"""Explicit lodging updates preserve revisions, old snapshots and provider boundaries."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.trip_understandings_v3 import get_trip_understanding_repository
from app.experience_main import app
from app.trip_understanding.errors import IdempotencyConflictError, RevisionConflictError
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.models import ActivityDeleteCommand
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.stay import ControlledStayRouteProvider, StayRecommendationEngine
from app.trip_understanding.worker import TripUnderstandingWorker
from tests.test_experience_v3_journey import repository_for, create, finish, refresh
from tests.test_stay_overnight_segments import CityHotels


async def job_count(repo, kind):
    return len(repo.stay_jobs) if kind == "memory" else await repo._pool.fetchval("SELECT count(*) FROM trip_stay_recommendation_jobs")


async def finish_stay(repo, now, provider=None):
    job = await repo.claim_next_stay(worker_id="manual-stay", now=now, lease_seconds=60)
    assert job is not None
    output = await StayRecommendationEngine(provider or CityHotels(), ControlledStayRouteProvider()).recommend(
        await repo.load_stay_plan(job), observed_at=now)
    await repo.complete_stay_job(job, output, now=now)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_edit_keeps_hotel_but_requires_explicit_idempotent_update_and_never_reuses_old_minutes(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "stay-refresh", now), now)
        await finish_stay(repo, now)
        resource, stored = await refresh(repo, resource, now)
        chosen = (await repo.get_stay_view(resource)).candidates[0]
        service = TripUnderstandingApplicationService(repo)
        await service.select_stay(resource, candidate_token=chosen.candidate_token, expected_etag=stored.opaque_etag,
            idempotency_key="choose", now=now)
        resource, stored = await refresh(repo, resource, now)
        await service.apply_command(resource, ActivityDeleteCommand(command_type="ACTIVITY_DELETE",
            activity_token=stored.result.days[0].activities[0].activity_token), expected_etag=stored.opaque_etag,
            idempotency_key="edit", now=now)
        resource, edited = await refresh(repo, resource, now)
        assert await job_count(repo, kind) == 1
        stale = await repo.get_stay_view(resource)
        assert stale.status == "NEEDS_UPDATE" and stale.candidates[0].selected
        assert stale.candidates[0].max_single_leg_minutes is None
        with pytest.raises(RevisionConflictError):
            await repo.refresh_stay_suggestions(resource, expected_etag=stored.opaque_etag, idempotency_key="stale", now=now)
        responses = await asyncio.gather(*(repo.refresh_stay_suggestions(resource, expected_etag=edited.opaque_etag,
            idempotency_key="manual", now=now) for _ in range(2)))
        assert {r[2] for r in responses} == {False, True}
        assert responses[0][0].status == "PREPARING" and responses[0][1] == edited.opaque_etag
        assert await job_count(repo, kind) == 2
        with pytest.raises(IdempotencyConflictError):
            await repo.refresh_stay_suggestions(resource, expected_etag=stored.opaque_etag, idempotency_key="manual", now=now)
        await finish_stay(repo, now)
        current = await repo.get_stay_view(resource)
        assert current.candidates[0].selected and current.candidates[0].name == chosen.name
        assert current.candidates[0].max_single_leg_minutes is None
        _, after = await refresh(repo, resource, now)
        assert after.opaque_etag == edited.opaque_etag


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_failed_snapshot_manual_retries_are_cooled_down_bounded_and_immutable(kind):
    class EmptyHotels:
        async def search(self, **kwargs):
            return []
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "stay-retry", now), now)
        await finish_stay(repo, now, EmptyHotels())
        _, stored = await refresh(repo, resource, now)
        immediate = await repo.refresh_stay_suggestions(resource, expected_etag=stored.opaque_etag,
            idempotency_key="too-soon", now=now)
        assert immediate[0].status == "UNAVAILABLE" and await job_count(repo, kind) == 1
        for attempt in range(1, 5):
            later = now + timedelta(seconds=31 * attempt)
            view, _, _ = await repo.refresh_stay_suggestions(resource, expected_etag=stored.opaque_etag,
                idempotency_key=f"retry-{attempt}", now=later)
            if attempt <= 3:
                assert view.status == "PREPARING"
                await finish_stay(repo, later, EmptyHotels())
            else:
                assert view.status == "UNAVAILABLE"
        assert await job_count(repo, kind) == 4
        if kind == "postgres":
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_stay_recommendation_snapshots") == 4
        await TripUnderstandingApplicationService(repo).delete_trip(resource, capability_hash="a" * 64,
            user_id=None, idempotency_key="delete-retries", now=now + timedelta(minutes=3))
        if kind == "postgres":
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_stay_recommendation_snapshots") == 0
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_idempotency_records WHERE scope=$1",
                f"understanding:{resource.understanding_id}:stay-refresh") == 0
        else:
            assert not repo.stay_refresh_idempotency and not repo.stay_jobs_by_key


@pytest.mark.parametrize("scope_kind", ["trip", "account"])
@pytest.mark.asyncio
async def test_delete_cleans_all_owned_recommendation_replays_and_preserves_other_trip_and_delete_replay(scope_kind):
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "delete-all-stay", now), now)
        service = TripUnderstandingApplicationService(repo)
        if scope_kind == "account":
            await service.claim_demo(resource.public_resource_id, capability_hash="a" * 64, user_id="experience-owner",
                idempotency_key="claim-before-delete", now=now)
        scopes = [f"understanding:{resource.understanding_id}:stay-selection",
            f"understanding:{resource.understanding_id}:stay-refresh", "understanding:other-trip:stay-refresh"]
        for scope in scopes:
            await repo._pool.execute("""INSERT INTO trip_understanding_idempotency_records
                (scope,key_hash,request_hash,state,response_status,response_json,response_headers_json,created_at,completed_at)
                VALUES($1,$2,$3,'COMPLETED',200,'{}','{}',$4,$4)""", scope, "a" * 64, "b" * 64, now)
        if scope_kind == "trip":
            await service.delete_trip(resource, capability_hash="a" * 64, user_id=None, idempotency_key="delete-owned", now=now)
        else:
            await service.delete_account_travel_data(user_id="experience-owner", idempotency_key="delete-owned", now=now)
        remaining = await repo._pool.fetch("SELECT scope FROM trip_understanding_idempotency_records WHERE scope=ANY($1::text[])", scopes)
        assert [row["scope"] for row in remaining] == [scopes[-1]]
        if scope_kind == "trip":
            assert await service.replay_trip_deletion(resource.public_resource_id, capability_hash="a" * 64,
                user_id=None, idempotency_key="delete-owned")
        else:
            assert (await service.delete_account_travel_data(user_id="experience-owner", idempotency_key="delete-owned", now=now)).replayed


def test_refresh_api_requires_version_replays_and_fixed_demo_cannot_use_live_engine():
    repo = InMemoryTripUnderstandingRepository()
    app.dependency_overrides[get_trip_understanding_repository] = lambda: repo
    class ForbiddenEngine:
        async def recommend(self, *args, **kwargs):
            raise AssertionError("fixed demo must not call live lodging engine")
    try:
        with TestClient(app) as client:
            created = client.post("/api/v3/trip-understandings", json={"mode": "DEMO"}, headers={"Idempotency-Key": "stay-api"})
            identifier = created.json()["public_resource_id"]
            asyncio.run(TripUnderstandingWorker(repo).run_once("stay-api"))
            result = client.get(f"/api/v3/trip-understandings/{identifier}/result")
            url = f"/api/v3/trip-understandings/{identifier}/stay-suggestions"
            assert client.post(url).status_code == 428
            headers = {"If-Match": result.headers["etag"], "Idempotency-Key": "manual-api"}
            first, replay = client.post(url, headers=headers), client.post(url, headers=headers)
            assert first.status_code == replay.status_code == 200 and first.json() == replay.json()
            assert replay.headers["Idempotency-Replayed"] == "true" and first.headers["etag"] == result.headers["etag"]
            worker = MapRenderWorker(repo, stay_engine=ForbiddenEngine())
            asyncio.run(worker.run_once("map-first"))
            asyncio.run(worker.run_once("stay-next"))
            assert client.get(url).json()["status"] in {"AVAILABLE", "LIMITED"}
    finally:
        app.dependency_overrides.pop(get_trip_understanding_repository, None)
