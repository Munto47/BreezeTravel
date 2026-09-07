"""Explicit city correction through the authenticated search/confirmation path."""
import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import trip_understandings_v3 as api
from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, issue_candidate
from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.models import PlaceConfirmCommand
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_v3_journey import repository_for, create, finish, refresh


def shanghai_place():
    return CandidatePlace(canonical_place_id="synthetic-shanghai-poi", city="上海", name="上海博物馆(东馆)",
        category="景点", area_or_address="浦东新区", position=GCJ02Position(longitude=121.542, latitude=31.219))


def test_http_explicit_city_overrides_card_without_changing_other_cards_or_auto_routes():
    repository = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.get_trip_understanding_repository] = lambda: repository
    calls = []
    async def search(**kwargs):
        calls.append(kwargs)
        return [shanghai_place()]
    app.dependency_overrides[api.get_place_candidate_search] = lambda: search
    with TestClient(app) as client:
        created = client.post("/api/v3/trip-understandings", json={"mode": "FULL", "source": {"type": "TEXT", "text": DEMO_SOURCE_TEXT}},
            headers={"Idempotency-Key": "city-selection-create"})
        assert created.status_code == 202
        base = "/api/v3/trip-understandings/" + created.json()["public_resource_id"]
        async def complete():
            now = datetime.now(timezone.utc)
            job = await repository.claim_next(worker_id="city-test", now=now, lease_seconds=30)
            await repository.complete_job(job, await build_demo_pipeline().run(DEMO_SOURCE_TEXT), now=now)
        asyncio.run(complete())
        before = client.get(base + "/result")
        cards = before.json()["days"][0]["activities"]
        token = cards[0]["activity_token"]
        body = {"activity_token": token, "query": "上海博物馆东馆", "city": "上海"}
        assert client.post(base + "/place-candidates", json={**body, "city": "成都"}).status_code == 422
        assert calls == []
        # Old clients can omit the optional field and retain the current city.
        assert client.post(base + "/place-candidates", json={"activity_token": token, "query": "故宫"}).status_code == 200
        assert calls[-1]["city"] == "北京"
        rows = client.post(base + "/place-candidates", json=body)
        assert rows.status_code == 200 and calls[-1]["city"] == "上海"
        assert "synthetic-shanghai-poi" not in rows.text
        before_jobs = len(repository.map_jobs)
        command = {"command_type": "PLACE_CONFIRM", "activity_token": token, "candidate_token": rows.json()["candidates"][0]["candidate_token"]}
        headers = {"If-Match": before.headers["etag"], "Idempotency-Key": "city-confirm"}
        saved = client.post(base + "/commands", json=command, headers=headers)
        assert saved.status_code == 200
        assert client.post(base + "/commands", json=command, headers=headers).status_code == 200
        after = client.get(base + "/result")
        changed = after.json()["days"][0]["activities"]
        assert changed[0]["city"] == "上海" and changed[0]["name"] == "上海博物馆(东馆)"
        # Commands rotate all opaque activity tokens; compare every business field.
        assert [{k: v for k, v in card.items() if k != "activity_token"} for card in changed[1:]] == [
            {k: v for k, v in card.items() if k != "activity_token"} for card in cards[1:]]
        assert after.json()["map"]["status"] == "NEEDS_UPDATE"
        assert len(repository.map_jobs) == before_jobs
        assert client.post(base + "/commands", json=command,
            headers={"If-Match": after.headers["etag"], "Idempotency-Key": "stale-city-token"}).status_code == 409
        with TestClient(app) as stranger:
            assert stranger.post(base + "/place-candidates", json=body).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_selected_city_persists_and_undo_restores_map_coordinates(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "city-confirm", now), now)
        stored = await repo.get_result(resource)
        token = stored.result.days[0].activities[0].activity_token
        original_point = (await repo.get_map_view(resource, now=now)).points[0]
        candidate = issue_candidate(shanghai_place(), public_resource_id=resource.public_resource_id,
            activity_token=token, expected_etag=stored.opaque_etag, now=now)
        before_jobs = len(repo.map_jobs) if kind == "memory" else await repo._pool.fetchval("SELECT COUNT(*) FROM trip_map_render_jobs")
        service = TripUnderstandingApplicationService(repo)
        await service.apply_command(resource, PlaceConfirmCommand(command_type="PLACE_CONFIRM", activity_token=token,
            candidate_token=candidate.candidate_token), expected_etag=stored.opaque_etag, idempotency_key="city", now=now)
        resource, confirmed = await refresh(repo, resource, now)
        assert confirmed.result.days[0].activities[0].city == "上海"
        assert [card.model_dump(exclude={"activity_token"}) for card in confirmed.result.days[0].activities[1:]] == [
            card.model_dump(exclude={"activity_token"}) for card in stored.result.days[0].activities[1:]]
        assert (await repo.get_map_view(resource, now=now)).points[0].position.longitude == 121.542
        assert confirmed.result.map.status == "NEEDS_UPDATE"
        after_jobs = len(repo.map_jobs) if kind == "memory" else await repo._pool.fetchval("SELECT COUNT(*) FROM trip_map_render_jobs")
        assert after_jobs == before_jobs
        from app.trip_understanding.models import UndoCommand
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=confirmed.opaque_etag,
            idempotency_key="undo-city", now=now)
        resource, undone = await refresh(repo, resource, now)
        assert undone.result.days[0].activities[0].city == stored.result.days[0].activities[0].city
        assert (await repo.get_map_view(resource, now=now)).points[0].position == original_point.position
