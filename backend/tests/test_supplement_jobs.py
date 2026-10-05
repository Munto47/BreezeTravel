"""Existing worker and real PostgreSQL publish source-bound additions."""
import hashlib
import os
import asyncio
from datetime import datetime, timezone

import pytest

from app.trip_understanding.bounded_supplement import build_supplement_patch
from app.trip_understanding.inference_allowance import reserve_model_call
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.worker import TripUnderstandingWorker
from tests.test_bounded_supplement import SOURCE, CountingPlaces, add, fixture
from tests.test_experience_v3_journey import repository_for

ROUNDS = range(int(os.environ.get("TRIPCHECK_RELIABILITY_ROUNDS", "1")))


async def prepared(repo, *, anonymous=False):
    now = datetime.now(timezone.utc)
    original, _ = await fixture()
    options = dict(source_text=SOURCE, idempotency_key="source", request_hash=hashlib.sha256(SOURCE.encode()).hexdigest(), now=now)
    created = (await repo.create_demo(capability_hash="a" * 64, ttl_hours=24, **options) if anonymous
        else await repo.create_full(owner_user_id="experience-owner", retention_days=7, **options))
    job = await repo.claim_next(worker_id="initial", now=now, lease_seconds=60)
    output = await TripUnderstandingPipeline(None, CountingPlaces()).run(SOURCE, prepared_plan=original)
    await repo.complete_job(job, output, now=now)
    resource = await repo.authorize(created.accepted.public_resource_id, user_id=None if anonymous else "experience-owner",
        capability_hash="a" * 64 if anonymous else None, now=now)
    stored = await repo.get_result(resource)
    return now, resource, stored


async def refresh(repo, resource):
    resource = await repo.authorize(resource.public_resource_id, user_id="experience-owner", capability_hash=None, now=datetime.now(timezone.utc))
    return resource, await repo.get_result(resource)


async def patch_for(repo, resource, stored, now):
    accepted = await repo.request_supplement(resource, expected_etag=stored.opaque_etag, idempotency_key="supplement",
        retry=False, now=now)
    job = await repo.claim_next(worker_id="supplement", now=datetime.now(timezone.utc), lease_seconds=60)
    assert job.job_type == "SUPPLEMENT" and job.job_id == accepted.job_id
    work = await repo.load_supplement_work(job, now=now)
    await repo.reserve_inference_call(job, now=now)
    row = add(after_visit_id=work.result.days[0].activities[0].visit_id)
    patch = await build_supplement_patch(work.source, work.plan, work.result, [row], TripUnderstandingPipeline(None, CountingPlaces()))
    return job, work, patch


@pytest.mark.asyncio
async def test_supplement_publication_preserves_original_cards_and_real_new_identity():
    async with repository_for("postgres") as repo:
        now, resource, stored = await prepared(repo)
        job, work, patch = await patch_for(repo, resource, stored, now)
        await repo.complete_supplement_job(job, work, patch, now=now, provider_binding={"external_calls": 1})
        resource, saved = await refresh(repo, resource)
        assert [card.name for day in saved.result.days for card in day.activities] == ["故宫博物院", "景山公园", "故宫博物院"]
        assert saved.result.days[0].activities[0].visit_id == stored.result.days[0].activities[0].visit_id
        assert saved.result.days[1].activities[0].visit_id == stored.result.days[1].activities[0].visit_id
        assert saved.result.can_undo
        assert saved.opaque_etag != stored.opaque_etag
        new = saved.result.days[0].activities[1]
        assert await repo._pool.fetchval("SELECT canonical_place_id FROM trip_understanding_activities WHERE public_activity_token=$1", new.activity_token) == "synthetic-replay:北京:景山公园"
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_source_claims WHERE understanding_id=$1 AND revision=2 AND quote LIKE 'enc:v1:%'", resource.understanding_id) > 0
        status = await repo.get_supplement_state(resource, now=now)
        assert status.status == "APPLIED" and status.result_etag == saved.opaque_etag and status.added_count == 1
        assert await repo._pool.fetchval("SELECT inference_calls_remaining FROM trip_understanding_sources WHERE understanding_id=$1", resource.understanding_id) == 30


class ControlledSupplement:
    def __init__(self):
        self.calls = 0

    async def propose(self, work):
        await reserve_model_call()
        self.calls += 1
        return [add(after_visit_id=work.result.days[0].activities[0].visit_id)], {"external_calls": 1, "input_tokens": 123, "output_tokens": 45}


@pytest.mark.asyncio
async def test_existing_worker_dispatches_one_supplement_without_requerying_existing_places():
    async with repository_for("postgres") as repo:
        now, resource, stored = await prepared(repo)
        await repo.request_supplement(resource, expected_etag=stored.opaque_etag, idempotency_key="worker-supplement", retry=False, now=now)
        places, provider = CountingPlaces(), ControlledSupplement()
        worker = TripUnderstandingWorker(repo, full_pipeline=TripUnderstandingPipeline(None, places), supplement_provider=provider)
        assert await worker.run_once("supplement-worker")
        resource, saved = await refresh(repo, resource)
        assert (await repo.get_supplement_state(resource, now=datetime.now(timezone.utc))).status == "APPLIED"
        assert provider.calls == 1 and places.queries == ["景山公园"]
        assert len(saved.result.days[0].activities) == 2


async def edit_note(repo, resource, stored):
    from app.trip_understanding.models import ActivityTextEditCommand
    return await repo.apply_command(resource, ActivityTextEditCommand(command_type="ACTIVITY_TEXT_EDIT",
        activity_token=stored.result.days[0].activities[0].activity_token, note="用户刚刚保存的备注"),
        expected_etag=stored.opaque_etag, idempotency_key="edit", request_hash="e" * 64, now=datetime.now(timezone.utc))


async def counts(repo, resource):
    tables = ["revisions", "results", "activities", "source_claims"]
    return [await repo._pool.fetchval(f"SELECT count(*) FROM trip_understanding_{table} WHERE understanding_id=$1",
        resource.understanding_id) for table in tables]


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
@pytest.mark.parametrize("change", ["edit", "cancel", "source_delete", "expired_lease", "deadline"])
async def test_late_supplement_cannot_publish_after_state_changes(round_number, change):
    from app.trip_understanding.errors import JobLeaseLostError, RevisionConflictError, SourceUnavailableError
    from app.trip_understanding.inference_allowance import InferenceAllowanceExceeded
    async with repository_for("postgres") as repo:
        now, resource, stored = await prepared(repo)
        job, work, patch = await patch_for(repo, resource, stored, now)
        expected = JobLeaseLostError
        if change == "edit":
            await edit_note(repo, resource, stored)
            expected = RevisionConflictError
        elif change == "cancel":
            await repo.cancel_supplement(resource, job.job_id, now=now)
        elif change == "source_delete":
            await repo.delete_source(resource, user_id="experience-owner", request_hash="d" * 64,
                idempotency_key="delete-source", now=now)
            expected = SourceUnavailableError
        elif change == "expired_lease":
            await repo._pool.execute("UPDATE trip_understanding_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=$1", job.job_id)
        else:
            await repo._pool.execute("UPDATE trip_understanding_sources SET inference_deadline_at=clock_timestamp()-interval '1 second' WHERE understanding_id=$1", resource.understanding_id)
            expected = InferenceAllowanceExceeded
        resource, before = await refresh(repo, resource)
        before_counts = await counts(repo, resource)
        with pytest.raises(expected):
            await repo.complete_supplement_job(job, work, patch, now=now, provider_binding={"external_calls": 1})
        resource, after = await refresh(repo, resource)
        assert after.opaque_etag == before.opaque_etag
        assert after.result == before.result
        assert await counts(repo, resource) == before_counts


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
async def test_duplicate_request_lost_response_and_explicit_retry_share_budget(round_number):
    from app.trip_understanding.supplement_jobs import SupplementRejectedError
    async with repository_for("postgres") as repo:
        now, resource, stored = await prepared(repo)
        job, work, patch = await patch_for(repo, resource, stored, now)
        duplicate = await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
            idempotency_key="supplement", retry=False, now=now)
        assert duplicate.job_id == job.job_id
        first = await repo.complete_supplement_job(job, work, patch, now=now, provider_binding={"external_calls": 1})
        before_counts = await counts(repo, resource)
        replay = await repo.complete_supplement_job(job, work, patch, now=now, provider_binding={"external_calls": 1})
        assert replay.replayed and replay.opaque_etag == first.opaque_etag
        assert await counts(repo, resource) == before_counts
        resource, current = await refresh(repo, resource)
        with pytest.raises(SupplementRejectedError, match="RETRY_REQUIRED"):
            await repo.request_supplement(resource, expected_etag=current.opaque_etag,
                idempotency_key="implicit-retry", retry=False, now=now)
        deadline = await repo._pool.fetchval("SELECT inference_deadline_at FROM trip_understanding_sources WHERE understanding_id=$1", resource.understanding_id)
        await repo.request_supplement(resource, expected_etag=current.opaque_etag,
            idempotency_key="explicit-retry", retry=True, now=now)
        retry = await repo.claim_next(worker_id="second", now=datetime.now(timezone.utc), lease_seconds=60)
        retry_work = await repo.load_supplement_work(retry, now=now)
        await repo.reserve_inference_call(retry, now=now)
        empty = await build_supplement_patch(retry_work.source, retry_work.plan, retry_work.result, [], TripUnderstandingPipeline(None, CountingPlaces()))
        await repo.complete_supplement_job(retry, retry_work, empty, now=now, provider_binding={"external_calls": 1})
        resource, after = await refresh(repo, resource)
        assert after.opaque_etag == current.opaque_etag
        assert await counts(repo, resource) == before_counts
        source = await repo._pool.fetchrow("SELECT * FROM trip_understanding_sources WHERE understanding_id=$1", resource.understanding_id)
        assert source["inference_calls_remaining"] == 29 and source["inference_deadline_at"] == deadline
        assert (await repo.get_supplement_state(resource, now=now)).status == "NO_CHANGES"
        with pytest.raises(SupplementRejectedError, match="SUPPLEMENT_LIMIT"):
            await repo.request_supplement(resource, expected_etag=after.opaque_etag, idempotency_key="third", retry=True, now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
async def test_supplement_publication_failure_rolls_back_all_version_writes(round_number):
    import asyncpg
    async with repository_for("postgres") as repo:
        now, resource, stored = await prepared(repo)
        job, work, patch = await patch_for(repo, resource, stored, now)
        before = await counts(repo, resource)
        # Fail at the final job-state write, after versions, pointer and operation were written.
        await repo._pool.execute("""CREATE FUNCTION fail_supplement_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.job_type='SUPPLEMENT' AND NEW.status='SUCCEEDED' THEN RAISE EXCEPTION 'controlled commit failure'; END IF;
            RETURN NEW; END $$;
            CREATE TRIGGER fail_supplement_commit BEFORE UPDATE ON trip_understanding_jobs FOR EACH ROW EXECUTE FUNCTION fail_supplement_commit();""")
        with pytest.raises(asyncpg.RaiseError, match="controlled commit failure"):
            await repo.complete_supplement_job(job, work, patch, now=now, provider_binding={"external_calls": 1})
        assert await counts(repo, resource) == before
        resource, saved = await refresh(repo, resource)
        assert saved.opaque_etag == stored.opaque_etag
        assert await repo._pool.fetchval("SELECT status FROM trip_understanding_jobs WHERE job_id=$1", job.job_id) == "RUNNING"
        assert not await repo._pool.fetchval("SELECT EXISTS(SELECT 1 FROM trip_understanding_idempotency_records WHERE scope=$1)", f"understanding:{resource.understanding_id}:command")


@pytest.mark.asyncio
async def test_supplement_api_uses_existing_auth_etag_and_cancel_path():
    from fastapi import FastAPI
    import httpx
    from app.api import trip_understandings_v3 as api
    from app.utils.auth import get_optional_user
    async with repository_for("postgres") as repo:
        _, resource, stored = await prepared(repo)
        app = FastAPI()
        app.include_router(api.router, prefix="/api")
        app.dependency_overrides[api.get_trip_understanding_repository] = lambda: repo
        app.dependency_overrides[get_optional_user] = lambda: "experience-owner"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            url = f"/api/v3/trip-understandings/{resource.public_resource_id}/supplements"
            assert (await client.get(url)).json()["available_actions"] == ["REQUEST"]
            assert (await client.post(url, json={})).status_code == 428
            headers = {"If-Match": stored.opaque_etag, "Idempotency-Key": "api"}
            accepted = await client.post(url, json={}, headers=headers)
            assert accepted.status_code == 202 and accepted.headers["Cache-Control"] == "no-store"
            again = await client.post(url, json={}, headers=headers)
            assert again.json()["job_id"] == accepted.json()["job_id"]
            stopped = await client.post(f"{url}/{accepted.json()['job_id']}/cancel")
            assert stopped.json()["status"] == "CANCELLED"
            resource, saved = await refresh(repo, resource)
            assert saved.opaque_etag == stored.opaque_etag
            app.dependency_overrides[get_optional_user] = lambda: "someone-else"
            assert (await client.get(url)).status_code == 404


@pytest.mark.asyncio
async def test_detail_and_alternative_save_readback_and_undo_without_mainline_promotion():
    from app.trip_understanding.models import UndoCommand, RedoCommand
    async with repository_for("postgres") as repo:
        now, resource, stored = await prepared(repo)
        job, work, _ = await patch_for(repo, resource, stored, now)
        first = work.result.days[0].activities[0]
        rows = [dict(operation_id="detail", operation="ADD_DETAIL", parent_visit_id=first.visit_id,
            details=[dict(kind="VISIT", source_quote="太和殿", optional=False, evidence="故宫博物院，馆内重点参观太和殿")]),
            dict(operation_id="optional", operation="ADD_ALTERNATIVE", activity=dict(source_quote="颐和园",
                place_name="颐和园", day_index=1, role="OPTIONAL", role_evidence="若有余力去颐和园", category="景点"))]
        places = CountingPlaces()
        patch = await build_supplement_patch(work.source, work.plan, work.result, rows, TripUnderstandingPipeline(None, places))
        assert len(patch.validation.accepted) == 2 and not patch.validation.rejected
        assert not places.queries
        await repo.complete_supplement_job(job, work, patch, now=now, provider_binding={"external_calls": 1})
        resource, saved = await refresh(repo, resource)
        assert len(saved.result.days[0].activities) == 1
        assert [item.name for item in saved.result.days[0].activities[0].source_details] == ["太和殿"]
        assert [item.name for item in saved.result.days[0].alternatives] == ["颐和园"]
        supplementary = await repo.get_supplementary_view(resource, now=now)
        assert [(item.name, item.role) for day in supplementary.days for item in day.items] == [("颐和园", "OPTIONAL")]
        source_view = await repo.get_source_view(resource, now=now)
        assert len(source_view.activities) == 2
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_source_claims WHERE understanding_id=$1 AND revision=2", resource.understanding_id) >= 2
        await repo.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=saved.opaque_etag,
            idempotency_key="undo", request_hash="a" * 64, now=now)
        resource, undone = await refresh(repo, resource)
        assert not undone.result.days[0].alternatives and not undone.result.days[0].activities[0].source_details
        assert not (await repo.get_supplementary_view(resource, now=now)).days
        await repo.apply_command(resource, RedoCommand(command_type="REDO"), expected_etag=undone.opaque_etag,
            idempotency_key="redo", request_hash="b" * 64, now=now)
        resource, redone = await refresh(repo, resource)
        assert redone.result.days[0].activities[0].source_details == saved.result.days[0].activities[0].source_details
        assert [item.name for item in redone.result.days[0].alternatives] == ["颐和园"]
        assert (await repo.get_supplementary_view(resource, now=now)).days[0].items[0].name == "颐和园"


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
@pytest.mark.parametrize("dispatched", [False, True])
async def test_supplement_takeover_never_redispatches_unknown_paid_request(round_number, dispatched):
    from app.trip_understanding.errors import JobLeaseLostError, RevisionConflictError
    async with repository_for("postgres") as repo:
        now, resource, stored = await prepared(repo)
        accepted = await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
            idempotency_key="takeover", retry=False, now=now)
        old = await repo.claim_next(worker_id="same-worker", now=datetime.now(timezone.utc), lease_seconds=60)
        work = await repo.load_supplement_work(old, now=now)
        patch = await build_supplement_patch(work.source, work.plan, work.result,
            [add(after_visit_id=work.result.days[0].activities[0].visit_id)], TripUnderstandingPipeline(None, CountingPlaces()))
        if dispatched:
            await repo.reserve_inference_call(old, now=now)
        await repo._pool.execute("UPDATE trip_understanding_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=$1", old.job_id)
        provider = ControlledSupplement()
        worker = TripUnderstandingWorker(repo, full_pipeline=TripUnderstandingPipeline(None, CountingPlaces()), supplement_provider=provider)
        assert await worker.run_once("same-worker")
        state = await repo.get_supplement_state(resource, now=now)
        assert state.job_id == accepted.job_id
        assert state.status == ("FAILED" if dispatched else "APPLIED")
        assert provider.calls == (0 if dispatched else 1)
        if dispatched:
            assert state.reason == "LEASE_TAKEOVER_UNKNOWN_OUTCOME"
        before = await counts(repo, resource)
        with pytest.raises((JobLeaseLostError, RevisionConflictError)):
            await repo.complete_supplement_job(old, work, patch, now=now, provider_binding={"external_calls": 1})
        assert await counts(repo, resource) == before
        assert await repo._pool.fetchval("SELECT inference_calls_remaining FROM trip_understanding_sources WHERE understanding_id=$1", resource.understanding_id) == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
@pytest.mark.parametrize("operation", ["reserve", "publish"])
@pytest.mark.parametrize("loss", ["lease", "source", "session"])
async def test_supplement_checks_authority_after_waiting_for_lock(round_number, operation, loss):
    from app.trip_understanding.errors import JobLeaseLostError, SourceUnavailableError, ResourceAccessDeniedError
    async with repository_for("postgres") as repo:
        now, resource, stored = await prepared(repo, anonymous=True)
        await repo.request_supplement(resource, expected_etag=stored.opaque_etag, idempotency_key="wait", retry=False, now=now)
        job = await repo.claim_next(worker_id="wait-worker", now=datetime.now(timezone.utc), lease_seconds=60)
        work = await repo.load_supplement_work(job, now=now)
        patch = await build_supplement_patch(work.source, work.plan, work.result,
            [add(after_visit_id=work.result.days[0].activities[0].visit_id)], TripUnderstandingPipeline(None, CountingPlaces()))
        if operation == "publish":
            await repo.reserve_inference_call(job, now=now)
        before = await counts(repo, resource)
        credits = await repo._pool.fetchval("SELECT inference_calls_remaining FROM trip_understanding_sources WHERE understanding_id=$1", resource.understanding_id)
        task = None
        try:
            async with repo._pool.acquire() as blocker, blocker.transaction():
                await blocker.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"trip-understanding-resource:{resource.understanding_id}")
                task = asyncio.create_task(repo.reserve_inference_call(job, now=now) if operation=="reserve"
                    else repo.complete_supplement_job(job, work, patch, now=now, provider_binding={"external_calls": 1}))
                async with asyncio.timeout(5):
                    while not await blocker.fetchval("""SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory'
                        AND NOT granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database()))"""):
                        await asyncio.sleep(0.01)
                if loss=="lease":
                    await blocker.execute("UPDATE trip_understanding_jobs SET lease_until=clock_timestamp()-interval '1 millisecond' WHERE job_id=$1", job.job_id)
                elif loss=="source":
                    await blocker.execute("UPDATE trip_understanding_sources SET retention_until=clock_timestamp()-interval '1 millisecond' WHERE understanding_id=$1", resource.understanding_id)
                else:
                    await blocker.execute("""UPDATE trip_understanding_anonymous_sessions SET revoked_at=clock_timestamp()
                        WHERE session_id=(SELECT anonymous_session_id FROM trip_understandings WHERE understanding_id=$1)""", resource.understanding_id)
            expected = {"lease": JobLeaseLostError, "source": SourceUnavailableError, "session": ResourceAccessDeniedError}[loss]
            with pytest.raises(expected):
                await task
            assert await counts(repo, resource) == before
            assert await repo._pool.fetchval("SELECT current_result_id FROM trip_understandings WHERE understanding_id=$1", resource.understanding_id) == resource.current_result_id
            assert await repo._pool.fetchval("SELECT inference_calls_remaining FROM trip_understanding_sources WHERE understanding_id=$1", resource.understanding_id) == credits
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
async def test_exhausted_supplement_takeovers_leave_saved_trip_and_allow_explicit_retry(round_number):
    async with repository_for("postgres") as repo:
        now, resource, stored = await prepared(repo)
        job, _, _ = await patch_for(repo, resource, stored, now)
        await repo._pool.execute("""UPDATE trip_understanding_jobs SET attempt=max_attempts,
            lease_until=clock_timestamp()-interval '1 second' WHERE job_id=$1""", job.job_id)
        provider = ControlledSupplement()
        worker = TripUnderstandingWorker(repo, full_pipeline=TripUnderstandingPipeline(None, CountingPlaces()), supplement_provider=provider)
        await worker.run_once("next-worker")
        state = await repo.get_supplement_state(resource, now=now)
        assert state.status == "FAILED" and state.available_actions == ["RETRY"]
        assert state.reason == "SUPPLEMENT_TAKEOVERS_EXHAUSTED"
        assert provider.calls == 0
        resource, saved = await refresh(repo, resource)
        assert saved.opaque_etag == stored.opaque_etag
        assert await repo._pool.fetchval("SELECT inference_calls_remaining FROM trip_understanding_sources WHERE understanding_id=$1", resource.understanding_id) == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
async def test_empty_supplement_lost_request_response_replays_without_new_version_or_dispatch(round_number):
    class EmptySupplement(ControlledSupplement):
        async def propose(self, work):
            await reserve_model_call()
            self.calls += 1
            return [], {"external_calls": 1, "input_tokens": None, "output_tokens": None}
    async with repository_for("postgres") as repo:
        now, resource, stored = await prepared(repo)
        before = await counts(repo, resource)
        accepted = await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
            idempotency_key="empty-lost", retry=False, now=now)
        provider = EmptySupplement()
        worker = TripUnderstandingWorker(repo, full_pipeline=TripUnderstandingPipeline(None, CountingPlaces()), supplement_provider=provider)
        assert await worker.run_once("empty-worker")
        replay = await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
            idempotency_key="empty-lost", retry=False, now=now)
        assert replay.job_id == accepted.job_id
        assert not await worker.run_once("after-lost-response")
        state = await repo.get_supplement_state(resource, now=now)
        assert state.status == "NO_CHANGES" and state.result_etag == stored.opaque_etag
        assert provider.calls == 1 and await counts(repo, resource) == before
        source = await repo._pool.fetchrow("SELECT supplement_requests,inference_calls_remaining FROM trip_understanding_sources WHERE understanding_id=$1", resource.understanding_id)
        assert dict(source) == {"supplement_requests": 1, "inference_calls_remaining": 30}
