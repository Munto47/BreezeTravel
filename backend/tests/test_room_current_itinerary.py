"""Shared current route through the real runtime/router and random PostgreSQL.

All driving responses are fixed MockTransport data. No model/map service is used.
The database helper creates and drops its own randomly named database only.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import Request
from pydantic import ValidationError

from app.schemas.api import OptimizeRequest
from app.schemas.place import Place
from app.services.room_current_itinerary import selection_snapshot
from tests.test_experience_v3_journey import repository_for


def places():
    return [Place(place_id=f"place_{i}", name=name, city="北京", address="北京",
        category=category, coords={"lng": lng, "lat": lat}, estimated_duration=9999,
        opening_hours="仅09:00-09:01") for i, (name, category, lng, lat) in enumerate([
            ("故宫博物院", "attraction", 116.397, 39.918),
            ("景山公园", "attraction", 116.397, 39.925),
            ("全聚德前门店", "food", 116.397, 39.899),
            ("北京饭店", "hotel", 116.409, 39.908)])]


def body(**changes):
    return {"room_id": "shared-room", "thread_id": "shared-thread", "relative_only": True,
        "trip_days": 2, "places": [p.model_dump(mode="json") for p in places()],
        "base_room_route_version": 0, "room_route_request_id": "request-one", **changes}


@pytest.mark.parametrize("changes", [
    {"base_room_route_version": -1}, {"base_room_route_version": True},
    {"base_room_route_version": 0.0}, {"base_room_route_version": "0"},
    {"room_route_request_id": None}, {"base_room_route_version": None},
    {"relative_only": False}, {"room_id": None}, {"room_route_request_id": "bad request"},
])
def test_publication_contract_rejects_ambiguous_version_or_partial_opt_in(changes):
    with pytest.raises(ValidationError):
        OptimizeRequest.model_validate(body(**changes))


def test_selection_contains_original_selected_identity_and_no_provider_or_clock_fields():
    request = OptimizeRequest.model_validate(body())
    snapshot = selection_snapshot(request)
    assert snapshot["trip_days"] == 2
    assert snapshot["place_ids"] == [p.place_id for p in places()]
    assert [(p["name"], p["coords"]) for p in snapshot["places"]] == [
        (p.name, p.coords.model_dump()) for p in places()]
    assert not any(key in str(snapshot) for key in ("estimated_duration", "opening_hours", "evidence", "source_quote"))
    # Older clients keep their explicit calculation-only behavior.
    old = body()
    old.pop("base_room_route_version")
    old.pop("room_route_request_id")
    assert OptimizeRequest.model_validate(old).base_room_route_version is None


def test_import_idempotency_distinguishes_shared_versions_even_when_contents_are_identical():
    from app.trip_understanding.collaboration_import import prepare_collaboration_import
    route = {"days": [{"slots": [{"place": {"name": "景山公园", "category": "attraction"}}]}]}
    def prepared(version):
        return prepare_collaboration_import(user_id="member-b", room_id="shared-room", city="北京",
            saved_itinerary_id=f"room-route:{version}", itinerary_data=route,
            idempotency_key="same-click", room_route_version=version)
    assert prepared(1).source_text == prepared(2).source_text
    assert prepared(1).request_hash != prepared(2).request_hash
    assert prepared(1).internal_idempotency_key == prepared(2).internal_idempotency_key


@pytest_asyncio.fixture
async def api(monkeypatch):
    from app import experience_main as runtime
    from app.api import places_persist, trip_understandings_v3
    from app.config import Settings
    from app.services import room_access, room_current_itinerary
    from app.trip_understanding import collaboration_import
    from app.utils.auth import get_current_user, get_optional_user
    async with repository_for("postgres") as repository:
        pool = repository._pool
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO users(user_id,nickname) VALUES('member-a','A'),('member-b','B'),('outsider','X')")
            await conn.execute("INSERT INTO rooms(room_id,thread_id,trip_city,trip_days) VALUES('shared-room','shared-thread','北京',2)")
            await conn.execute("INSERT INTO room_members(room_id,user_id) VALUES('shared-room','member-a'),('shared-room','member-b')")
        cfg = Settings(_env_file=None, runtime_profile="test", experience_workers_enabled=False,
            require_schema_check=False, jwt_secret_key="fixed-test-secret", amap_api_key="fixed-test")
        for module in (runtime, runtime.optimize, trip_understandings_v3, collaboration_import):
            monkeypatch.setattr(module, "get_settings", lambda: cfg)
        for module in (room_access, room_current_itinerary, places_persist, collaboration_import):
            monkeypatch.setattr(module, "get_pool", AsyncMock(return_value=pool))
        app = runtime.create_app()
        app.state.cache = AsyncMock()
        app.state.cache.eval.return_value = 1

        async def user(request: Request):
            return request.headers.get("x-test-user", "member-a")

        app.dependency_overrides[get_current_user] = user
        app.dependency_overrides[get_optional_user] = user
        app.dependency_overrides[trip_understandings_v3.get_trip_understanding_repository] = lambda: repository
        state = SimpleNamespace(pool=pool, repository=repository, calls=[], on_route=None, payload={
            "status": "1", "route": {"paths": [{"duration": "660", "distance": "2300"}]}})

        async def fixed(request):
            assert request.url.host == "restapi.amap.com" and request.url.path == "/v3/direction/driving"
            state.calls.append((request.url.params["origin"], request.url.params["destination"]))
            if state.on_route is not None:
                await state.on_route()
            return httpx.Response(200, json=state.payload)

        actual_client = httpx.AsyncClient
        async with actual_client(transport=httpx.ASGITransport(app=app), base_url="http://controlled") as http:
            monkeypatch.setattr(runtime.optimize.httpx, "AsyncClient", lambda **kwargs:
                actual_client(transport=httpx.MockTransport(fixed), **kwargs))
            state.http = http
            yield state


async def publish(api, *, user="member-a", **changes):
    return await api.http.post("/api/optimize", headers={"x-test-user": user}, json=body(**changes))


async def read(api, *, user="member-b"):
    return await api.http.get("/api/room/shared-room/current-itinerary", headers={"x-test-user": user})


def ids(route):
    return [[slot["place_id"] for slot in day["slots"]] for day in route["days"]]


@pytest.mark.asyncio
async def test_a_publishes_b_reopens_same_server_route_and_imports_without_personal_save(api):
    empty = (await read(api)).json()
    assert empty == {"room_id": "shared-room", "version": 0, "itinerary_data": None,
        "selection_snapshot": None, "published_at": None}
    response = await publish(api)
    assert response.status_code == 200, response.text
    assert response.json()["room_route_version"] == 1 and len(api.calls) == 2
    current = (await read(api)).json()
    assert current == (await read(api, user="member-a")).json()
    assert current["version"] == 1 and current["published_at"]
    assert ids(current["itinerary_data"]) == ids(response.json()["itinerary"])
    assert current["selection_snapshot"] == selection_snapshot(OptimizeRequest.model_validate(body()))
    legs = [slot["transport"] for day in current["itinerary_data"]["days"] for slot in day["slots"] if slot["transport"]]
    assert legs == [{"mode": "driving", "status": "AVAILABLE", "duration_mins": 11, "distance_km": 2.3}] * 2
    assert await api.pool.fetchval("SELECT count(*) FROM saved_itineraries") == 0
    imported = await api.http.post("/api/v3/trip-understandings/from-collaboration",
        headers={"x-test-user": "member-b", "Idempotency-Key": "import-shared-one"},
        json={"room_id": "shared-room", "room_route_version": 1})
    assert imported.status_code == 202, imported.text
    from datetime import datetime, timezone
    job = await api.repository.claim_next(worker_id="fixed-import", now=datetime.now(timezone.utc), lease_seconds=60)
    source = await api.repository.load_source(job, now=datetime.now(timezone.utc))
    ordered = [(mention.atomic_place_name, mention.day_index) for mention in source.initial_plan.mentions]
    assert ordered == [(slot["place"]["name"], day["day_index"]+1)
        for day in current["itinerary_data"]["days"] for slot in day["slots"]]
    assert source.initial_plan.binding["external_calls"] == 0
    assert await api.pool.fetchval("SELECT count(*) FROM saved_itineraries") == 0
    assert (await read(api)).json() == current and len(api.calls) == 2


@pytest.mark.asyncio
async def test_replay_is_zero_http_stale_or_changed_request_cannot_publish(api):
    first = await publish(api)
    again = await publish(api)
    assert again.status_code == first.status_code == 200
    assert again.json() == first.json() and len(api.calls) == 2
    changed = await publish(api, trip_days=1)
    stale = await publish(api, room_route_request_id="different-request")
    assert changed.status_code == stale.status_code == 409
    assert changed.json()["detail"]["code"] == "ROOM_ROUTE_VERSION_CONFLICT"
    assert len(api.calls) == 2
    second = await publish(api, user="member-b", base_room_route_version=1, room_route_request_id="member-b-new")
    assert second.status_code == 200 and second.json()["room_route_version"] == 2
    old_replay = await publish(api)
    assert old_replay.status_code == 409 and len(api.calls) == 4
    stale_import = await api.http.post("/api/v3/trip-understandings/from-collaboration",
        headers={"x-test-user": "member-b", "Idempotency-Key": "old-version"},
        json={"room_id": "shared-room", "room_route_version": 1})
    assert stale_import.status_code == 409 and stale_import.json()["detail"]["code"] == "ROOM_ROUTE_VERSION_CONFLICT"
    assert await api.pool.fetchval("SELECT count(*) FROM room_itinerary_revisions") == 2


@pytest.mark.asyncio
async def test_nonmember_wrong_room_thread_and_revoked_member_never_publish(api):
    assert (await read(api, user="outsider")).status_code == 403
    assert (await publish(api, user="outsider")).status_code == 403
    assert (await publish(api, thread_id="different-room-thread")).status_code == 403
    assert api.calls == []
    async def revoke():
        api.on_route = None
        # This executes while the provider is awaited, on a second DB connection.
        async with api.pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchval("SELECT room_id FROM rooms WHERE room_id='shared-room' FOR UPDATE NOWAIT")
                await conn.execute("DELETE FROM room_members WHERE room_id='shared-room' AND user_id='member-a'")
    api.on_route = revoke
    rejected = await publish(api)
    assert rejected.status_code == 403 and len(api.calls) == 2
    assert (await read(api)).json()["version"] == 0
    assert await api.pool.fetchval("SELECT status FROM room_itinerary_requests") == "FAILED"


@pytest.mark.asyncio
async def test_same_click_in_flight_does_not_repeat_provider_or_clear_first_request(api):
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait_for_release():
        entered.set()
        await release.wait()
    api.on_route = wait_for_release
    task = asyncio.create_task(publish(api))
    await asyncio.wait_for(entered.wait(), 2)
    duplicate = await publish(api)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "ROOM_ROUTE_REQUEST_IN_PROGRESS"
    assert len(api.calls) == 1
    assert await api.pool.fetchval("SELECT status FROM room_itinerary_requests") == "RUNNING"
    release.set()
    assert (await task).status_code == 200 and len(api.calls) == 2
    assert await api.pool.fetchval("SELECT count(*) FROM room_itinerary_revisions") == 1


@pytest.mark.asyncio
async def test_late_result_cannot_overwrite_other_members_new_version(api):
    entered, release = asyncio.Event(), asyncio.Event()
    async def pause_first():
        api.on_route = None
        entered.set()
        await release.wait()
    api.on_route = pause_first
    late = asyncio.create_task(publish(api))
    await asyncio.wait_for(entered.wait(), 2)
    winner = await publish(api, user="member-b", trip_days=1, room_route_request_id="winner-request")
    assert winner.status_code == 200
    current = (await read(api)).json()
    release.set()
    rejected = await late
    assert rejected.status_code == 409
    assert (await read(api)).json() == current
    assert len(current["itinerary_data"]["days"]) == 1
    assert await api.pool.fetchval("SELECT count(*) FROM room_itinerary_revisions") == 1


@pytest.mark.asyncio
async def test_cancel_propagates_and_does_not_publish_partial_or_retry_same_id(api):
    entered = asyncio.Event()
    async def wait_for_cancel():
        entered.set()
        await asyncio.Event().wait()
    api.on_route = wait_for_cancel
    # Direct endpoint task avoids ASGI middleware cancellation semantics.
    from app.api.optimize import optimize
    task = asyncio.create_task(optimize(OptimizeRequest.model_validate(body()), "member-a"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await read(api)).json()["version"] == 0
    assert await api.pool.fetchval("SELECT status FROM room_itinerary_requests") == "FAILED"
    retry = await publish(api)
    assert retry.status_code == 409 and retry.json()["detail"]["code"] == "ROOM_ROUTE_REQUEST_FAILED"
    assert len(api.calls) == 1


@pytest.mark.asyncio
async def test_missing_transport_remains_unknown_and_legacy_compute_and_personal_save_stay_separate(api):
    api.payload = {"status": "0"}
    response = await publish(api, base_room_route_version=None, room_route_request_id=None)
    assert response.status_code == 200 and response.json()["room_route_version"] is None
    assert (await read(api)).json()["version"] == 0
    assert await api.pool.fetchval("SELECT count(*) FROM room_itinerary_requests") == 0
    published = await publish(api)
    assert published.status_code == 200
    current = (await read(api)).json()
    assert sum(len(day["slots"]) for day in current["itinerary_data"]["days"]) == 4
    assert all(slot["transport"] is None for day in current["itinerary_data"]["days"] for slot in day["slots"])
    assert await api.pool.fetchval("SELECT total_distance_km FROM room_itinerary_revisions") is None
    saved = await api.http.post("/api/room/shared-room/itinerary", json={
        "city": "北京", "trip_days": 2, "itinerary_data": current["itinerary_data"]})
    assert saved.status_code == 200
    assert (await api.http.get("/api/room/shared-room/itinerary", headers={"x-test-user": "member-b"})).status_code == 404
    personal_import = await api.http.post("/api/v3/trip-understandings/from-collaboration",
        headers={"x-test-user": "member-b", "Idempotency-Key": "personal-empty"}, json={"room_id": "shared-room"})
    assert personal_import.status_code == 409
    assert await api.pool.fetchval("SELECT count(*) FROM saved_itineraries WHERE user_id='member-a'") == 1
    assert (await read(api)).json() == current


@pytest.mark.asyncio
async def test_published_record_has_no_sensitive_place_payload_and_survives_publisher_account_deletion(api):
    response = await publish(api)
    assert response.status_code == 200
    current = (await read(api)).json()
    row = await api.pool.fetchrow("SELECT selection_snapshot,itinerary_data FROM room_itinerary_revisions")
    stored = json.dumps([json.loads(row["selection_snapshot"]), json.loads(row["itinerary_data"])])
    assert all(word not in stored for word in ("opening_hours", "estimated_duration", "evidence", "source_quote"))
    await api.pool.execute("DELETE FROM users WHERE user_id='member-a'")
    assert (await read(api)).json() == current
    assert (await read(api, user="member-a")).status_code == 403


@pytest.mark.asyncio
async def test_selected_places_change_during_http_preserves_original_snapshot_without_claiming_new_choices(api):
    async def change_choices():
        api.on_route = None
        async with api.pool.acquire() as conn:
            await conn.execute("""INSERT INTO room_places(room_id,place_id,place_data)
                VALUES('shared-room','new-selected-place','{"name":"北海公园"}'::jsonb)""")
    api.on_route = change_choices
    response = await publish(api)
    assert response.status_code == 200
    current = (await read(api)).json()
    assert current["selection_snapshot"]["place_ids"] == [p.place_id for p in places()]
    assert "new-selected-place" not in str(current)
    assert await api.pool.fetchval("SELECT place_id FROM room_places") == "new-selected-place"
    # The client compares this stored snapshot with live selections and marks it outdated.
    assert sum(len(day["slots"]) for day in current["itinerary_data"]["days"]) == 4


@pytest.mark.asyncio
async def test_clear_personal_travel_data_preserves_shared_route_but_old_in_flight_publish_cannot_recreate_personal_data(api):
    from datetime import datetime, timezone
    assert (await publish(api)).status_code == 200
    current = (await read(api)).json()
    entered, release = asyncio.Event(), asyncio.Event()
    async def pause():
        entered.set()
        await release.wait()
    api.on_route = pause
    late = asyncio.create_task(publish(api, base_room_route_version=1, room_route_request_id="in-flight-cleared"))
    await asyncio.wait_for(entered.wait(), 2)
    deleted = await api.repository.delete_account_travel_data(user_id="member-a", idempotency_key="delete-personal",
        request_hash="a" * 64, now=datetime.now(timezone.utc))
    assert deleted.view.status == "COMPLETED"
    assert await api.pool.fetchval("SELECT count(*) FROM room_itinerary_requests WHERE user_id='member-a'") == 0
    assert await api.pool.fetchval("SELECT published_by_user_id FROM room_itinerary_revisions") is None
    release.set()
    response = await late
    assert response.status_code == 409
    assert (await read(api)).json() == current
    assert await api.pool.fetchval("SELECT count(*) FROM room_itinerary_revisions") == 1
    assert await api.pool.fetchval("SELECT count(*) FROM room_itinerary_requests") == 0


@pytest.mark.asyncio
async def test_room_deletion_removes_shared_versions_and_request_copies(api):
    assert (await publish(api)).status_code == 200
    await api.pool.execute("DELETE FROM rooms WHERE room_id='shared-room'")
    assert (await read(api)).status_code == 403
    assert await api.pool.fetchval("SELECT count(*) FROM room_itinerary_revisions") == 0
    assert await api.pool.fetchval("SELECT count(*) FROM room_itinerary_requests") == 0


@pytest.mark.asyncio
async def test_lost_import_response_replays_only_successful_same_member_request_after_room_advances(api):
    assert (await publish(api)).status_code == 200
    async def import_old(*, user="member-b", key="lost-import-response"):
        return await api.http.post("/api/v3/trip-understandings/from-collaboration",
            headers={"x-test-user": user, "Idempotency-Key": key},
            json={"room_id": "shared-room", "room_route_version": 1})
    original = await import_old()
    assert original.status_code == 202
    assert (await publish(api, base_room_route_version=1, room_route_request_id="room-next-route")).status_code == 200
    replay = await import_old()
    assert replay.status_code == 202, replay.text
    assert replay.json() == original.json() and replay.headers["Idempotency-Replayed"] == "true"
    assert await api.pool.fetchval("SELECT count(*) FROM trip_understandings WHERE owner_user_id='member-b'") == 1
    assert (await import_old(key="new-request-for-old-route")).status_code == 409
    assert (await import_old(user="member-a")).status_code == 409
    assert (await import_old(user="outsider")).status_code == 403
    from datetime import datetime, timezone
    await api.repository.delete_account_travel_data(user_id="member-b", idempotency_key="clear-import",
        request_hash="b"*64, now=datetime.now(timezone.utc))
    assert (await import_old()).status_code == 409
    assert await api.pool.fetchval("SELECT count(*) FROM trip_understandings WHERE owner_user_id='member-b'") == 0
    assert (await read(api)).json()["version"] == 2 and len(api.calls) == 4
