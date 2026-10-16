"""Lease exhaustion preserves durable drafts and does not repeat provider work."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.errors import JobLeaseLostError, ResourceGoneError, ResourceNotFoundError
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_v3_journey import create, finish, refresh, repository_for


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("preview", [False, True])
async def test_final_interruption_retains_preview_or_fails_and_releases_creation_slot(kind, preview):
    updates = []

    async def progress(update):
        updates.append(update)

    output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT, progress_callback=progress, progressive_places=True)
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await create(repo, "exhausted-first", now)
        for attempt in range(3):
            claimed_at = now + timedelta(seconds=attempt * 3)
            job = await repo.claim_next(worker_id=f"interrupted-{attempt}", now=claimed_at, lease_seconds=2)
            assert job.attempt == attempt + 1
        if preview:
            assert await repo.record_progress(job, updates[-1].model_copy(update={"update_sequence": 1}), now=claimed_at)
        assert await repo.claim_next(worker_id="too-early", now=claimed_at, lease_seconds=2) is None
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=claimed_at)
        assert resource.state == "PROCESSING"
        expired_at = claimed_at + timedelta(seconds=3)
        assert await repo.claim_next(worker_id="recovery", now=expired_at, lease_seconds=2) is None
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=expired_at)
        assert resource.state == ("PARTIAL" if preview else "FAILED")
        stored = await repo.get_result(resource)
        if preview:
            assert stored.result.days[0].activities[0].activity_token == updates[-1].snapshot.days[0].activities[0].activity_token
            assert stored.result.days[0].activities[0].status == "READY"
            assert not stored.result.coverage.complete
        else:
            assert stored is None
        with pytest.raises(JobLeaseLostError):
            await repo.complete_job(job, output, now=expired_at)
        assert not await repo.record_progress(job, updates[-1].model_copy(update={"update_sequence": 2}), now=expired_at)
        await repo.fail_job(job, category="LATE_FAILURE", now=expired_at)
        assert (await repo.authorize(resource.public_resource_id, capability_hash="a" * 64, now=expired_at)).state == resource.state
        if kind == "postgres":
            row = await repo._pool.fetchrow("SELECT status,attempt,lease_owner FROM trip_understanding_jobs WHERE job_id=$1", job.job_id)
        else:
            row = repo.jobs[job.job_id]
        assert row["status"] == "FAILED" and row["attempt"] == 3 and row["lease_owner"] is None
        await create(repo, "exhausted-next", expired_at)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_delete_during_exhaustion_cleanup_cannot_recreate_trip(kind, monkeypatch):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await create(repo, "delete-exhausted", now)
        for attempt in range(3):
            await repo.claim_next(worker_id=f"stopped-{attempt}", now=now + timedelta(seconds=attempt * 3), lease_seconds=2)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        original = repo.cancel_understanding

        async def delete_then_finish(*args, **kwargs):
            await TripUnderstandingApplicationService(repo).delete_trip(resource, capability_hash="a" * 64,
                user_id=None, idempotency_key="delete", now=now + timedelta(seconds=9))
            return await original(*args, **kwargs)

        monkeypatch.setattr(repo, "cancel_understanding", delete_then_finish)
        assert await repo.claim_next(worker_id="cleanup", now=now + timedelta(seconds=9), lease_seconds=2) is None
        with pytest.raises((ResourceGoneError, ResourceNotFoundError)):
            await repo.authorize(resource.public_resource_id, capability_hash="a" * 64, now=now + timedelta(seconds=9))
        if kind == "postgres":
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_results") == 0
        else:
            assert not repo.results


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_exhausted_map_refresh_keeps_old_snapshot_and_rejects_late_completion(kind):
    from tests.test_map_render_generations import create_trip, refresh as refresh_map, records
    from app.trip_understanding.map_render import MapRenderer

    async with repository_for(kind) as repo:
        resource, stored = await create_trip(repo)
        old_snapshots = (await records(repo, kind))[1]
        await refresh_map(repo, resource, stored.opaque_etag)
        now = datetime.now(timezone.utc)
        for attempt in range(3):
            claimed_at = now + timedelta(seconds=attempt * 3)
            job = await repo.claim_next_map(worker_id=f"map-stopped-{attempt}", now=claimed_at, lease_seconds=2)
            assert job.attempt == attempt + 1
        output = await MapRenderer().render(await repo.load_map_plan(job), observed_at=claimed_at)
        assert await repo.claim_next_map(worker_id="too-early", now=claimed_at, lease_seconds=2) is None
        assert (await repo.get_map_view(resource, now=claimed_at)).status == "PREPARING"
        expired_at = claimed_at + timedelta(seconds=3)
        assert await repo.claim_next_map(worker_id="cleanup", now=expired_at, lease_seconds=2) is None
        view = await repo.get_map_view(resource, now=expired_at)
        assert view.status == "UNAVAILABLE" and not view.days
        assert (await records(repo, kind))[1] == old_snapshots
        with pytest.raises(JobLeaseLostError):
            await repo.complete_map_job(job, output, now=expired_at)
        await repo.fail_map_job(job, category="LATE_FAILURE", now=expired_at)
        assert (await repo.get_map_view(resource, now=expired_at)).status == "UNAVAILABLE"
        assert (await refresh_map(repo, resource, stored.opaque_etag, "after-exhaustion")).accepted.status == "PREPARING"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_exhausted_stay_does_not_wait_forever_or_publish_late_output(kind):
    from app.trip_understanding.stay import StayRecommendationEngine

    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "stay-exhausted", now), now)
        for attempt in range(3):
            claimed_at = now + timedelta(seconds=attempt * 3)
            job = await repo.claim_next_stay(worker_id=f"stay-stopped-{attempt}", now=claimed_at, lease_seconds=2)
            assert job.attempt == attempt + 1
        output = await StayRecommendationEngine().recommend(await repo.load_stay_plan(job), observed_at=claimed_at)
        assert await repo.claim_next_stay(worker_id="too-early", now=claimed_at, lease_seconds=2) is None
        assert (await repo.get_stay_view(resource)).status == "PREPARING"
        expired_at = claimed_at + timedelta(seconds=3)
        assert await repo.claim_next_stay(worker_id="cleanup", now=expired_at, lease_seconds=2) is None
        assert (await repo.get_stay_view(resource)).status == "UNAVAILABLE"
        with pytest.raises(JobLeaseLostError):
            await repo.complete_stay_job(job, output, now=expired_at)
        await repo.fail_stay_job(job, category="LATE_FAILURE", now=expired_at)
        if kind == "postgres":
            row = await repo._pool.fetchrow("SELECT status,attempt,lease_owner FROM trip_stay_recommendation_jobs WHERE stay_job_id=$1", job.stay_job_id)
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_stay_recommendation_snapshots") == 0
        else:
            row = repo.stay_jobs[job.stay_job_id]
            assert row["output"] is None
        assert row["status"] == "UNAVAILABLE" and row["attempt"] == 3 and row["lease_owner"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_exhausted_stay_refresh_preserves_prior_candidates_and_selected_hotel(kind):
    from tests.test_stay_manual_refresh import finish_stay

    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "stay-preserved", now), now)
        await finish_stay(repo, now)
        candidate = (await repo.get_stay_view(resource)).candidates[0]
        _, stored = await refresh(repo, resource, now)
        await TripUnderstandingApplicationService(repo).select_stay(resource, candidate_token=candidate.candidate_token,
            expected_etag=stored.opaque_etag, idempotency_key="choose-before-refresh", now=now)
        resource, stored = await refresh(repo, resource, now)
        if kind == "postgres":
            snapshots = await repo._pool.fetch("SELECT snapshot_id,snapshot_sha256 FROM trip_stay_recommendation_snapshots")
        else:
            snapshots = {key: item['output'].model_dump() for key, item in repo.stay_jobs.items() if item['output']}
        later = now + timedelta(minutes=16)
        await repo.refresh_stay_suggestions(resource, expected_etag=stored.opaque_etag,
            idempotency_key="refresh-selected", now=later)
        for attempt in range(3):
            job = await repo.claim_next_stay(worker_id=f"stopped-refresh-{attempt}",
                now=later + timedelta(seconds=attempt * 3), lease_seconds=2)
            assert job.attempt == attempt + 1
        await repo.claim_next_stay(worker_id="cleanup", now=later + timedelta(seconds=9), lease_seconds=2)
        view = await repo.get_stay_view(resource)
        assert view.status == "NEEDS_UPDATE" and not view.available_actions
        assert any(item.selected and item.name == candidate.name for segment in view.segments for item in segment.candidates)
        if kind == "postgres":
            assert await repo._pool.fetch("SELECT snapshot_id,snapshot_sha256 FROM trip_stay_recommendation_snapshots") == snapshots
        else:
            assert {key: item['output'].model_dump() for key, item in repo.stay_jobs.items() if item['output']} == snapshots


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_fair_polling_respects_active_map_lease_for_same_trip(kind):
    from app.trip_understanding.map_render import MapRenderer

    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        await finish(repo, await create(repo, "active-map", now), now)
        job = await repo.claim_next_map(worker_id="map-owner", now=now, lease_seconds=60)
        worker = MapRenderWorker(repo)
        worker._prefer_stay = True
        assert not await worker.run_once("fair-worker", now=now)
        output = await MapRenderer().render(await repo.load_map_plan(job), observed_at=now)
        await repo.complete_map_job(job, output, now=now)
        assert await worker.run_once("fair-worker", now=now)
        if kind == "postgres":
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_stay_recommendation_jobs WHERE attempt=1") == 1
        else:
            assert sum(item["attempt"] == 1 for item in repo.stay_jobs.values()) == 1


@pytest.mark.asyncio
async def test_concurrent_postgres_cleanup_publishes_one_terminal_revision():
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        created = await create(repo, "concurrent-cleanup", now)
        for attempt in range(3):
            await repo.claim_next(worker_id=f"stopped-{attempt}", now=now + timedelta(seconds=attempt * 3), lease_seconds=2)
        assert await asyncio.gather(*(repo.claim_next(worker_id=f"cleanup-{i}", now=now + timedelta(seconds=9),
            lease_seconds=2) for i in range(2))) == [None, None]
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64,
            now=now + timedelta(seconds=9))
        assert resource.state == "FAILED"
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_revisions WHERE status='FAILED'") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [None, "map", "stay"])
async def test_worker_rotates_priority_without_claiming_concurrent_work(empty):
    events = []

    class Queue:
        active = None

        async def claim(self, kind):
            assert self.active is None
            if kind == empty:
                return None
            self.active = kind
            return SimpleNamespace(attempt=1)

        async def claim_next_map(self, **kwargs):
            return await self.claim("map")

        async def claim_next_stay(self, **kwargs):
            return await self.claim("stay")

        async def load_map_plan(self, job):
            return None

        async def load_stay_plan(self, job):
            return None

        async def complete_map_job(self, *args, **kwargs):
            assert self.active == "map"
            events.append("map")
            self.active = None

        async def complete_stay_job(self, *args, **kwargs):
            assert self.active == "stay"
            events.append("stay")
            self.active = None

    class ControlledWork:
        async def render(self, *args, **kwargs):
            await asyncio.sleep(0)

        async def recommend(self, *args, **kwargs):
            await asyncio.sleep(0)

    repo = Queue()
    worker = MapRenderWorker(repo, renderer=ControlledWork(), stay_engine=ControlledWork())
    for _ in range(6):
        assert await worker.run_once("serial-worker")
    assert events == (["map", "stay"] * 3 if empty is None else ["stay" if empty == "map" else "map"] * 6)
