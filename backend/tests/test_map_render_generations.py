"""Explicit refresh over immutable snapshots; fixed providers and disposable PG."""
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import pytest

from app.itineraries.models import RevisionSource
from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.errors import (
    CommandTargetChangedError, JobLeaseLostError, ResourceAccessDeniedError,
    ResourceNotFoundError, RevisionConflictError,
)
from app.trip_understanding.g03 import build_itinerary_revision
from app.trip_understanding.map_render import InternalRouteModeFact, MapRenderer
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_v3_journey import repository_for
from tests.test_relative_route_flow import create_trip, preview, snapshot
from tests.test_relative_route_options import FixedAmap


class LegacyProvider(AmapRouteProvider):
    async def route(self, *args, **kwargs):
        value = (await super().route(*args, **kwargs)).model_dump()
        value['provider_binding'].pop('route_connection')
        return InternalRouteModeFact.model_validate(value)


async def render(repo, *, legacy=False):
    transport = FixedAmap()
    provider_type = LegacyProvider if legacy else AmapRouteProvider
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport.respond)) as client:
        worker = MapRenderWorker(repo, renderer=MapRenderer(provider_type(api_key='fixed', client=client)))
        assert await worker.run_once('generation-worker')
    assert len(transport.calls) == 8


async def records(repo, kind):
    if kind == 'postgres':
        async with repo._pool.acquire() as conn:
            jobs = [dict(r) for r in await conn.fetch('SELECT map_job_id,status,render_generation,logical_key_hash,attempt FROM trip_map_render_jobs ORDER BY render_generation')]
            snapshots = {r['map_job_id']: r['snapshot_sha256'].strip() for r in await conn.fetch('SELECT map_job_id,snapshot_sha256 FROM trip_map_render_snapshots')}
            effects = await conn.fetchval('SELECT count(*) FROM trip_map_provider_effect_receipts')
    else:
        jobs = sorted(repo.map_jobs.values(), key=lambda row: row.get('render_generation', 0))
        jobs = [{k: row[k] for k in ('map_job_id','status','render_generation','logical_key_hash','attempt')} for row in jobs]
        snapshots = {key: value.snapshot_sha256 for key, value in repo.map_snapshots.items()}
        effects = repo.map_provider_effect_count
    return jobs, snapshots, effects


async def refresh(repo, resource, tag, key='manual-refresh'):
    return await TripUnderstandingApplicationService(repo).request_map_render(
        resource, expected_etag=tag, idempotency_key=key, now=datetime.now(UTC))


async def initial(repo, resource, kind):
    revision = (await snapshot(repo,resource,kind)).revision
    if kind == 'postgres':
        async with repo._pool.acquire() as conn, conn.transaction():
            return await repo._ensure_map_job(conn, resource.understanding_id, revision,
                request_origin='INITIAL', now=datetime.now(UTC))
    return repo._ensure_memory_map_job(resource.understanding_id, revision,
        request_origin='INITIAL', now=datetime.now(UTC))


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['memory','postgres'])
async def test_explicit_refresh_preserves_legacy_snapshot_and_same_trip_version(kind):
    async with repository_for(kind) as repo:
        resource, stored = await create_trip(repo, render=False)
        await render(repo, legacy=True)
        old = await records(repo, kind)
        before = await snapshot(repo, resource, kind)
        view = await repo.get_map_view(resource)
        assert view.status == 'LIMITED' and 'RENDER_MAP' in view.available_actions
        assert all(r.walking.geometry_break_indices is None for d in view.days for r in d.routes)
        accepted = await asyncio.gather(refresh(repo,resource,stored.opaque_etag), refresh(repo,resource,stored.opaque_etag,'double-click'))
        assert all(out.accepted.status == 'PREPARING' for out in accepted)
        jobs, saved, _ = await records(repo, kind)
        assert [j['render_generation'] for j in jobs] == [0,1] and saved == old[1]
        assert (await initial(repo,resource,kind))['map_job_id'] == jobs[-1]['map_job_id']
        assert (await repo.get_map_view(resource)).status == 'PREPARING'
        await render(repo)
        jobs, saved, effects = await records(repo, kind)
        assert len(saved) == 2 and all(saved[k] == v for k,v in old[1].items())
        assert effects == 16 and len({j['logical_key_hash'] for j in jobs}) == 2
        reader = PostgresTripUnderstandingRepository(pool=repo._pool, geometry_cache=repo._geometry_cache) if kind=='postgres' else repo
        current = await reader.get_map_view(resource)
        assert current.status == 'AVAILABLE' and all(r.walking.connection_status == 'VERIFIED' for d in current.days for r in d.routes)
        after = await snapshot(repo,resource,kind)
        assert (after.etag, after.revision, after.identities, after.source) == (before.etag, before.revision, before.identities, before.source)
        replay = await refresh(repo,resource,stored.opaque_etag)
        assert replay.replayed and (await records(repo,kind))[0] == jobs
        assert (await initial(repo,resource,kind))['map_job_id'] == jobs[-1]['map_job_id']
        assert (await reader.get_map_view(resource)) == current


@pytest.mark.asyncio
async def test_old_schema_rows_upgrade_without_rewriting_snapshot_and_old_writer_is_incompatible():
    async with repository_for('postgres') as repo:
        resource, stored = await create_trip(repo,render=False)
        await render(repo,legacy=True)
        old = await records(repo,'postgres')
        async with repo._pool.acquire() as conn:
            # Restore the exact 040 task shape on this disposable database only.
            # The source, old job, immutable snapshot and effect rows remain.
            await conn.execute('DROP INDEX idx_trip_map_render_jobs_active')
            await conn.execute('DROP INDEX idx_trip_map_render_jobs_generation')
            await conn.execute('ALTER TABLE trip_map_render_jobs DROP COLUMN render_generation')
            await conn.execute('ALTER TABLE trip_map_render_jobs ADD CONSTRAINT trip_map_render_jobs_plan_ref_id_route_config_hash_key UNIQUE(plan_ref_id,route_config_hash)')
            await conn.execute(Path('app/db/migrations/041_map_render_generations.sql').read_text(encoding='utf-8'))
            with pytest.raises(asyncpg.InvalidColumnReferenceError):
                await conn.execute("INSERT INTO trip_map_render_jobs(map_job_id) VALUES('old-writer') ON CONFLICT(plan_ref_id,route_config_hash) DO NOTHING")
        assert await records(repo,'postgres') == old
        assert (await repo.get_map_view(resource)).status == 'LIMITED'
        assert (await refresh(repo,resource,stored.opaque_etag)).accepted.status == 'PREPARING'
        await render(repo)
        assert len((await records(repo,'postgres'))[1]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['memory','postgres'])
async def test_new_generation_invalidates_old_preview_without_rewriting_successful_command_replay(kind):
    async with repository_for(kind) as repo:
        resource, stored = await create_trip(repo)
        fixed = FixedAmap()
        async with httpx.AsyncClient(transport=httpx.MockTransport(fixed.respond)) as client:
            provider = AmapRouteProvider(api_key='fixed',client=client)
            old, _ = await preview(repo,resource,stored.opaque_etag,provider)
            assert old.options and len(fixed.calls) == 6
            await refresh(repo,resource,stored.opaque_etag)
            cached, replayed = await preview(repo,resource,stored.opaque_etag,provider)
            assert replayed and cached.status == 'NEEDS_UPDATE' and not cached.options and len(fixed.calls) == 6
            pending, _ = await preview(repo,resource,stored.opaque_etag,provider,key='during-update')
            assert not pending.options and len(fixed.calls) == 6
        service = TripUnderstandingApplicationService(repo)
        with pytest.raises(CommandTargetChangedError):
            await service.adopt_trip_change(resource,change_token=old.options[0].change_token,expected_etag=stored.opaque_etag,idempotency_key='reject-old')
        await render(repo)
        with pytest.raises(CommandTargetChangedError):
            await service.adopt_trip_change(resource,change_token=old.options[0].change_token,expected_etag=stored.opaque_etag,idempotency_key='reject-old-after-ready')
        assert (await snapshot(repo,resource,kind)).etag == stored.opaque_etag

        # Another trip supplies a successful operation. Its same-key recovery
        # remains valid after the new business version has a new map task.
        other, original = await create_trip(repo,key='success-replay')
        async with httpx.AsyncClient(transport=httpx.MockTransport(FixedAmap().respond)) as client:
            options,_ = await preview(repo,other,original.opaque_etag,AmapRouteProvider(api_key='fixed',client=client),key='success-preview')
        token = options.options[0].change_token
        done = await service.adopt_trip_change(other,change_token=token,expected_etag=original.opaque_etag,idempotency_key='successful-adopt')
        current = await snapshot(repo,other,kind)
        await refresh(repo,other,current.etag,'new-version-map')
        duplicate = await service.adopt_trip_change(other,change_token=token,expected_etag=original.opaque_etag,idempotency_key='successful-adopt')
        assert duplicate.replayed and duplicate.opaque_etag == done.opaque_etag


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['memory','postgres'])
async def test_comparison_finishing_after_manual_refresh_is_rejected(kind):
    async with repository_for(kind) as repo:
        resource,stored = await create_trip(repo)
        fixed = FixedAmap()
        changed = False
        async def respond(request):
            nonlocal changed
            if not changed:
                changed = True
                await refresh(repo,resource,stored.opaque_etag)
            return await fixed.respond(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(CommandTargetChangedError):
                await preview(repo,resource,stored.opaque_etag,AmapRouteProvider(api_key='fixed',client=client))
        assert (await snapshot(repo,resource,kind)).etag == stored.opaque_etag


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['memory','postgres'])
async def test_failed_new_job_does_not_publish_old_snapshot_and_initial_does_not_retry(kind):
    async with repository_for(kind) as repo:
        resource,stored = await create_trip(repo)
        old = await records(repo,kind)
        await refresh(repo,resource,stored.opaque_etag)
        now = datetime.now(UTC)
        for attempt in range(3):
            job = await repo.claim_next_map(worker_id='failure',now=now,lease_seconds=60)
            assert job.attempt == attempt+1
            await repo.fail_map_job(job,category='FIXED_FAILURE',now=now)
            now += timedelta(seconds=3)
        view = await repo.get_map_view(resource,now=now)
        assert view.status == 'UNAVAILABLE' and not view.days
        jobs,saved,_ = await records(repo,kind)
        assert len(saved) == 2 and all(saved[k] == v for k,v in old[1].items())
        assert (await initial(repo,resource,kind))['map_job_id'] == jobs[-1]['map_job_id']
        assert (await refresh(repo,resource,stored.opaque_etag,'explicit-retry')).accepted.status == 'PREPARING'
        assert [j['render_generation'] for j in (await records(repo,kind))[0]] == [0,1,2]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['memory','postgres'])
async def test_worker_cancellation_and_late_lease_owner_cannot_replace_snapshot(kind):
    async with repository_for(kind) as repo:
        resource,stored = await create_trip(repo)
        old = await records(repo,kind)
        await refresh(repo,resource,stored.opaque_etag)
        class Cancelled:
            async def render(self,*args,**kwargs):
                raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await MapRenderWorker(repo,renderer=Cancelled()).run_once('cancelled-worker')
        assert (await records(repo,kind))[1] == old[1]
        now = datetime.now(UTC) + timedelta(minutes=2)
        takeover = await repo.claim_next_map(worker_id='new-owner',now=now,lease_seconds=60)
        assert takeover.attempt == 2
        stale = takeover.model_copy(update={'attempt':1,'lease_owner':'cancelled-worker'})
        plan = await repo.load_map_plan(takeover)
        async with httpx.AsyncClient(transport=httpx.MockTransport(FixedAmap().respond)) as client:
            output = await MapRenderer(AmapRouteProvider(api_key='fixed',client=client)).render(plan,observed_at=now)
        with pytest.raises(JobLeaseLostError):
            await repo.complete_map_job(stale,output,now=now)
        await repo.complete_map_job(takeover,output,now=now)
        assert len((await records(repo,kind))[1]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['memory','postgres'])
async def test_refresh_permissions_and_deleted_trip_reject_late_completion(kind):
    async with repository_for(kind) as repo:
        resource,stored = await create_trip(repo)
        original = await records(repo,kind)
        with pytest.raises(RevisionConflictError):
            await refresh(repo,resource,'old-etag')
        with pytest.raises(ResourceAccessDeniedError):
            await refresh(repo,resource.model_copy(update={'public_resource_id':'another-resource'}),stored.opaque_etag)
        assert await records(repo,kind) == original
        await refresh(repo,resource,stored.opaque_etag,'before-delete')
        now = datetime.now(UTC)
        job = await repo.claim_next_map(worker_id='delayed',now=now,lease_seconds=60)
        async with httpx.AsyncClient(transport=httpx.MockTransport(FixedAmap().respond)) as client:
            output = await MapRenderer(AmapRouteProvider(api_key='fixed',client=client)).render(await repo.load_map_plan(job),observed_at=now)
        await TripUnderstandingApplicationService(repo).delete_trip(resource,capability_hash=None,user_id='experience-owner',idempotency_key='delete',now=now)
        with pytest.raises(ResourceNotFoundError):
            await repo.complete_map_job(job,output,now=now)
        assert not (await records(repo,kind))[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['memory','postgres'])
async def test_g03_does_not_skip_latest_preparing_job_to_find_an_old_completed_snapshot(kind):
    async with repository_for(kind) as repo:
        resource,stored = await create_trip(repo)
        await refresh(repo,resource,stored.opaque_etag)
        current = await snapshot(repo,resource,kind)
        itinerary,_ = build_itinerary_revision(result=current.result,bindings={},assumptions=[],city='北京',
            workspace_id='fixed',itinerary_id='fixed',revision=1,parent_revision=None,source_type=RevisionSource.IMPORT)
        if kind=='postgres':
            async with repo._pool.acquire() as conn:
                evidence,*_ = await repo._collect_g03_evidence(conn,understanding_id=resource.understanding_id,understanding_revision=current.revision,
                    itinerary=itinerary,result=current.result,bindings={},now=datetime.now(UTC),supersedes_snapshot_id=None)
        else:
            evidence = repo._memory_g03_evidence(understanding_id=resource.understanding_id,understanding_revision=current.revision,
                itinerary=itinerary,bindings={},now=datetime.now(UTC),supersedes_snapshot_id=None)
        assert not [fact for fact in evidence.facts if fact.subject_type=='ROUTE_EDGE']
        assert any(f.error_category=='CURRENT_ROUTE_NOT_RENDERED' for f in evidence.provider_failures)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['memory','postgres'])
async def test_saved_checks_require_explicit_recheck_after_map_generation_changes(kind):
    async with repository_for(kind) as repo:
        resource,stored = await create_trip(repo)
        service = TripUnderstandingApplicationService(repo)
        await service.materialize_trip(resource,expected_etag=stored.opaque_etag,idempotency_key='check-before')
        await refresh(repo,resource,stored.opaque_etag)
        await render(repo)
        checks = await service.get_trip_checks(resource)
        assert checks.status == 'STILL_NEEDS_CONFIRMATION' and '路线已更新' in checks.message
        assert not any(item.depends_on_routes and item.can_preview for item in checks.items)
        await service.materialize_trip(resource,expected_etag=stored.opaque_etag,idempotency_key='check-after')
        current = await service.get_trip_checks(resource)
        assert '路线已更新' not in current.message
