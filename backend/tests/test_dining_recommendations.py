from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trip_understandings_v3 as api
from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, issue_candidate
from app.trip_understanding.dining import dining_binding, select_dining_rows
from app.trip_understanding.errors import CommandTargetChangedError, RevisionConflictError
from app.trip_understanding.map_render import MapStop
from app.trip_understanding.models import DiningInsertCommand, PlaceConfirmCommand, UndoCommand
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_v3_journey import repository_for, create, finish, refresh


def restaurant():
    return CandidatePlace(canonical_place_id="amap:meal-test", city="北京", name="合成餐厅",
        category="餐饮", area_or_address="合成测试地址", position=GCJ02Position(longitude=116.398, latitude=39.918))


def anchor():
    return MapStop(activity_token="a" * 24, day_index=1, day_label="Day 1", sequence_index=0,
        name="故宫博物院", canonical_place_id="amap:anchor", resolution_status="AUTO_MATCHED", city="北京",
        longitude=116.397, latitude=39.918)


def row():
    return {"id":"meal-test", "name":"合成餐厅", "cityname":"北京市", "pname":"北京市", "adname":"东城区",
        "adcode":"110101", "typecode":"050100", "type":"餐饮服务;中餐厅", "location":"116.398,39.918", "address":"合成测试地址"}


@pytest.mark.parametrize("change", [
    {"cityname":"上海市"}, {"typecode":"100100", "type":"住宿服务;宾馆酒店"},
    {"typecode":"050100", "type":"餐饮服务;酒店"}, {"location":"117.1,40.1"},
    {"name":"https://example.test"}, {"name":"记得提前预约。"}, {"name":"记得提前预约"},
    {"name":"01012345678"}, {"name":"预约说明"}, {"name":"这家餐厅需要确认"},
    {"location":"NaN,39.918"}, {"id":""},
])
def test_nearby_dining_rejects_wrong_identity_and_outside_radius(change):
    assert select_dining_rows([{**row(), **change}], anchor=anchor(), excluded_ids=set()) == []


def test_dining_deduplicates_and_excludes_existing_places_before_top_three():
    rows = [{**row(), "id":str(index), "name":f"合成餐厅{index}", "location":f"116.{398+index},39.918"} for index in range(6)]
    values = select_dining_rows(rows + [rows[0]], anchor=anchor(), excluded_ids={"amap:0"})
    assert [item.name for item in values] == ["合成餐厅1", "合成餐厅2", "合成餐厅3"]
    assert select_dining_rows(rows, anchor=anchor().model_copy(update={"resolution_status":"NEEDS_CONFIRMATION"}), excluded_ids=set()) == []


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_dining_adoption_is_atomic_replayable_undoable_and_preserves_real_coordinates(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "dining-flow", now), now)
        stored = await repo.get_result(resource)
        plan, etag = await repo.get_current_place_plan(resource)
        target = plan.stops[0]
        before_jobs = len(repo.map_jobs) if kind == "memory" else await repo._pool.fetchval("SELECT count(*) FROM trip_map_render_jobs")
        before = [[card.name for card in day.activities] for day in stored.result.days]
        issued = issue_candidate(restaurant(), public_resource_id=resource.public_resource_id,
            activity_token=dining_binding(target.activity_token), expected_etag=etag, now=now)
        command = DiningInsertCommand(command_type="DINING_INSERT", after_activity_token=target.activity_token,
            candidate_token=issued.candidate_token)
        service = TripUnderstandingApplicationService(repo)
        applied = await service.apply_command(resource, command, expected_etag=etag, idempotency_key="meal-insert", now=now)
        assert (await service.apply_command(resource, command, expected_etag=etag, idempotency_key="meal-insert", now=now)).replayed
        with pytest.raises(RevisionConflictError):
            await service.apply_command(resource, command, expected_etag=etag, idempotency_key="meal-duplicate", now=now)
        resource, stored = await refresh(repo, resource, now)
        assert stored.result.days[0].activities[1].name == "合成餐厅"
        assert stored.result.days[0].activities[1].status == "READY"
        assert stored.result.map.status == "NEEDS_UPDATE"
        new_plan, _ = await repo.get_current_place_plan(resource)
        meal = next(stop for stop in new_plan.stops if stop.name == "合成餐厅")
        assert meal.canonical_place_id == restaurant().canonical_place_id
        assert (meal.longitude, meal.latitude, meal.city) == (116.398, 39.918, "北京")
        assert stored.opaque_etag == applied.opaque_etag
        after_jobs = len(repo.map_jobs) if kind == "memory" else await repo._pool.fetchval("SELECT count(*) FROM trip_map_render_jobs")
        assert after_jobs == before_jobs
        # No rendering is requested by recommendation search or adoption.
        if kind == "memory":
            assert not repo.map_provider_effects
        else:
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_map_render_snapshots") == 0
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=stored.opaque_etag, idempotency_key="meal-undo", now=now)
        _, restored = await refresh(repo, resource, now)
        assert [[card.name for card in day.activities] for day in restored.result.days] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["purpose", "resource", "expired", "reverse-purpose", "category"])
async def test_dining_tokens_cannot_be_repurposed_or_used_after_expiry(bad):
    repo = InMemoryTripUnderstandingRepository()
    now = datetime.now(timezone.utc)
    resource = await finish(repo, await create(repo, "dining-binding", now), now)
    stored = await repo.get_result(resource)
    token = stored.result.days[0].activities[0].activity_token
    place = restaurant().model_copy(update={"category":"住宿"}) if bad == "category" else restaurant()
    issued = issue_candidate(place, public_resource_id="wrong-resource" if bad == "resource" else resource.public_resource_id,
        activity_token=token if bad == "purpose" else dining_binding(token), expected_etag=stored.opaque_etag,
        now=now - timedelta(minutes=11) if bad == "expired" else now)
    command = (PlaceConfirmCommand(command_type="PLACE_CONFIRM", activity_token=token, candidate_token=issued.candidate_token)
        if bad == "reverse-purpose" else DiningInsertCommand(command_type="DINING_INSERT", after_activity_token=token, candidate_token=issued.candidate_token))
    with pytest.raises(CommandTargetChangedError):
        await TripUnderstandingApplicationService(repo).apply_command(resource, command, expected_etag=stored.opaque_etag,
            idempotency_key="invalid-meal", now=now)
    assert (await repo.get_result(resource)).opaque_etag == stored.opaque_etag


def test_dining_api_requires_owner_and_is_read_only():
    import asyncio
    from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
    repo = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.get_trip_understanding_repository] = lambda: repo
    calls = []
    async def search(**kwargs):
        calls.append(kwargs)
        return [restaurant()]
    app.dependency_overrides[api.get_dining_candidate_search] = lambda: search
    client = TestClient(app)
    created = client.post("/api/v3/trip-understandings", json={"mode":"FULL", "source":{"type":"TEXT", "text":DEMO_SOURCE_TEXT}}, headers={"Idempotency-Key":"dining-api"})
    job = asyncio.run(repo.claim_next(worker_id="dining-api", now=datetime.now(timezone.utc), lease_seconds=30))
    asyncio.run(repo.complete_job(job, asyncio.run(build_demo_pipeline().run(DEMO_SOURCE_TEXT)), now=datetime.now(timezone.utc)))
    path = "/api/v3/trip-understandings/" + created.json()["public_resource_id"]
    initial = client.get(path + "/result")
    token = initial.json()["days"][0]["activities"][0]["activity_token"]
    assert TestClient(app).post(path + "/dining-candidates", json={"activity_token":token}).status_code == 404
    assert not calls
    response = client.post(path + "/dining-candidates", json={"activity_token":token})
    assert response.status_code == 200 and response.json()["status"] == "AVAILABLE"
    assert len(calls) == 1 and response.headers["etag"] == initial.headers["etag"]
    assert client.get(path + "/result").headers["etag"] == initial.headers["etag"]
    assert "canonical_place_id" not in response.text and "amap:meal-test" not in response.text
