from __future__ import annotations

import asyncio
import json

import pytest


fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import trip_understandings_v3  # noqa: E402
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository  # noqa: E402
from app.trip_understanding.service import TripUnderstandingApplicationService  # noqa: E402
from app.trip_understanding.worker import TripUnderstandingWorker  # noqa: E402
from app.utils.auth import get_optional_user  # noqa: E402


def _client():
    repository = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(trip_understandings_v3.router, prefix="/api")
    app.dependency_overrides[
        trip_understandings_v3.get_trip_understanding_repository
    ] = lambda: repository
    return TestClient(app), repository, app


def test_demo_api_create_events_result_refresh_and_session_isolation() -> None:
    client, repository, app = _client()
    missing_key = client.post("/api/v3/trip-understandings", json={"mode": "DEMO"})
    assert missing_key.status_code == 400
    assert client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "full"},
        json={"mode": "FULL"},
    ).status_code == 422

    created = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "browser-demo"},
        json={"mode": "DEMO"},
    )
    assert created.status_code == 202
    payload = created.json()
    public_resource_id = payload["public_resource_id"]
    assert set(payload) == {"public_resource_id", "status", "message", "result_url", "events_url"}
    cookie = created.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api/v3/trip-understandings" in cookie
    assert public_resource_id not in cookie

    replay = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "browser-demo"},
        json={"mode": "DEMO"},
    )
    assert replay.status_code == 202
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["public_resource_id"] == public_resource_id

    progress = client.get(payload["result_url"])
    assert progress.status_code == 202
    assert set(progress.json()) == {"status", "message", "retry_after_ms"}
    asyncio.run(TripUnderstandingWorker(repository).run_once("api-test-worker"))

    result = client.get(payload["result_url"])
    assert result.status_code == 200
    assert result.headers["etag"].startswith('"tu3_')
    assert [len(day["activities"]) for day in result.json()["days"]] == [2, 2, 2]
    refreshed = client.get(payload["result_url"])
    assert refreshed.json() == result.json()
    assert refreshed.headers["etag"] == result.headers["etag"]

    event_stream = client.get(payload["events_url"])
    assert event_stream.status_code == 200
    assert "event: progress" in event_stream.text
    assert "event: result_available" in event_stream.text
    resumed = client.get(payload["events_url"], headers={"Last-Event-ID": "2"})
    assert "event: progress" not in resumed.text
    assert "event: result_available" in resumed.text
    assert "id: 3" in resumed.text

    first_token = result.json()["days"][0]["activities"][0]["activity_token"]
    command_body = {
        "command_type": "ACTIVITY_MOVE",
        "activity_token": first_token,
        "target_day_index": 3,
        "target_position": 1,
    }
    missing_precondition = client.post(
        f"/api/v3/trip-understandings/{public_resource_id}/commands",
        headers={"Idempotency-Key": "move-1"},
        json=command_body,
    )
    assert missing_precondition.status_code == 428
    applied = client.post(
        f"/api/v3/trip-understandings/{public_resource_id}/commands",
        headers={
            "Idempotency-Key": "move-1",
            "If-Match": result.headers["etag"],
        },
        json=command_body,
    )
    assert applied.status_code == 200
    assert applied.json() == {
        "status": "APPLIED",
        "changed_days": ["Day 1", "Day 3"],
        "map_readiness": "NEEDS_UPDATE",
    }
    assert applied.headers["etag"] != result.headers["etag"]
    replayed_command = client.post(
        f"/api/v3/trip-understandings/{public_resource_id}/commands",
        headers={
            "Idempotency-Key": "move-1",
            "If-Match": result.headers["etag"],
        },
        json=command_body,
    )
    assert replayed_command.status_code == 200
    assert replayed_command.headers["Idempotency-Replayed"] == "true"
    assert replayed_command.headers["etag"] == applied.headers["etag"]
    stale = client.post(
        f"/api/v3/trip-understandings/{public_resource_id}/commands",
        headers={
            "Idempotency-Key": "move-stale",
            "If-Match": result.headers["etag"],
        },
        json=command_body,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "REVISION_CONFLICT"
    updated = client.get(payload["result_url"])
    assert updated.headers["etag"] == applied.headers["etag"]
    assert updated.json()["map"]["status"] == "NEEDS_UPDATE"
    assert [item["name"] for item in updated.json()["days"][2]["activities"]] == [
        "颐和园",
        "故宫博物院",
        "圆明园",
    ]
    assert repository.side_effect_count == 1

    other_browser = TestClient(app)
    denied = other_browser.get(payload["result_url"])
    assert denied.status_code == 404

    openapi_text = json.dumps(app.openapi(), ensure_ascii=False)
    assert '"DEMO"' in openapi_text
    assert '"FULL"' in openapi_text
    assert '"discriminator"' in openapi_text
    for command_type in (
        "ACTIVITY_INSERT",
        "ACTIVITY_DELETE",
        "ACTIVITY_MOVE",
        "ACTIVITY_TEXT_EDIT",
        "PLACE_REPLACE",
        "ASSUMPTION_SET",
    ):
        assert f'"{command_type}"' in openapi_text


def test_full_api_requires_login_and_uses_user_owned_persistent_chain() -> None:
    client, repository, app = _client()
    text = """北京三日行程
Day 1：故宫博物院、景山公园。
Day 2：天坛公园、前门大街。
Day 3：颐和园、圆明园。
有空可以考虑南锣鼓巷，不去上海迪士尼乐园。
"""
    request_body = {"mode": "FULL", "source": {"type": "TEXT", "text": text}}

    anonymous = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "full-anonymous"},
        json=request_body,
    )
    assert anonymous.status_code == 401
    assert anonymous.json()["detail"]["code"] == "LOGIN_REQUIRED"

    app.dependency_overrides[get_optional_user] = lambda: "user-a"
    created = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "full-user-a"},
        json=request_body,
    )
    assert created.status_code == 202
    assert "set-cookie" not in created.headers
    payload = created.json()
    assert client.get(payload["result_url"]).status_code == 202

    asyncio.run(TripUnderstandingWorker(repository).run_once("full-api-worker"))
    result = client.get(payload["result_url"])
    assert result.status_code == 200
    assert result.json()["status"] == "READY"
    assert [len(day["activities"]) for day in result.json()["days"]] == [2, 2, 2]
    serialized = json.dumps(result.json(), ensure_ascii=False)
    assert text not in serialized
    assert "南锣鼓巷" not in serialized
    assert "上海迪士尼乐园" not in serialized

    app.dependency_overrides[get_optional_user] = lambda: "user-b"
    assert client.get(payload["result_url"]).status_code == 404


@pytest.mark.asyncio
async def test_full_service_replay_conflict_and_two_active_job_limit() -> None:
    repository = InMemoryTripUnderstandingRepository()
    service = TripUnderstandingApplicationService(repository)
    from app.trip_understanding.models import CreateFullRequest
    from app.trip_understanding.errors import ConcurrentJobLimitError, IdempotencyConflictError

    first_body = CreateFullRequest.model_validate(
        {"mode": "FULL", "source": {"type": "TEXT", "text": "Day 1 去故宫博物院"}}
    )
    first = await service.create_full(
        first_body,
        owner_user_id="user-a",
        idempotency_key="full-1",
    )
    replay = await service.create_full(
        first_body,
        owner_user_id="user-a",
        idempotency_key="full-1",
    )
    assert replay.replayed is True
    assert replay.accepted == first.accepted
    conflicting_body = CreateFullRequest.model_validate(
        {"mode": "FULL", "source": {"type": "TEXT", "text": "Day 1 去天坛公园"}}
    )
    with pytest.raises(IdempotencyConflictError):
        await service.create_full(
            conflicting_body,
            owner_user_id="user-a",
            idempotency_key="full-1",
        )
    await service.create_full(
        conflicting_body,
        owner_user_id="user-a",
        idempotency_key="full-2",
    )
    with pytest.raises(ConcurrentJobLimitError):
        await service.create_full(
            conflicting_body,
            owner_user_id="user-a",
            idempotency_key="full-3",
        )
