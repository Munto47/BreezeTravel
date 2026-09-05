from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from unittest.mock import AsyncMock


fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import trip_understandings_v3  # noqa: E402
from app.trip_understanding.demo import (  # noqa: E402
    DEMO_SOURCE_TEXT,
    FixedBeijingDemoInferenceProvider,
    FixedBeijingPlaceResolver,
)
from app.trip_understanding.collaboration_import import (  # noqa: E402
    CollaborationImportSource,
    CollaborationRouteUnavailableError,
)
from app.trip_understanding.map_worker import MapRenderWorker  # noqa: E402
from app.trip_understanding.pipeline import TripUnderstandingPipeline  # noqa: E402
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository  # noqa: E402
from app.trip_understanding.service import TripUnderstandingApplicationService  # noqa: E402
from app.trip_understanding.worker import TripUnderstandingWorker  # noqa: E402
from app.utils import auth as auth_utils  # noqa: E402
from app.utils.auth import get_current_user, get_optional_user, get_recent_user  # noqa: E402


def _client():
    repository = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(trip_understandings_v3.router, prefix="/api")
    app.include_router(trip_understandings_v3.account_router, prefix="/api")
    app.dependency_overrides[
        trip_understandings_v3.get_trip_understanding_repository
    ] = lambda: repository
    return TestClient(app), repository, app


def test_collaboration_route_creates_private_text_job_and_replays(monkeypatch) -> None:
    client, repository, app = _client()
    source = CollaborationImportSource(
        source_text="北京1日行程。\nDay 1\n09:00-11:00 去故宫博物院（景点）。",
        request_hash="a" * 64,
        internal_idempotency_key="collaboration_" + "b" * 64,
        internal_binding={
            "status": "NOT_RUN",
            "source_origin": "COLLABORATION",
            "room_ref_hash": "c" * 64,
            "saved_itinerary_ref_hash": "d" * 64,
            "saved_content_hash": "e" * 64,
            "normalized_text_hash": "f" * 64,
        },
    )
    loader = AsyncMock(return_value=source)
    monkeypatch.setattr(trip_understandings_v3, "load_collaboration_import", loader)

    unauthenticated = client.post(
        "/api/v3/trip-understandings/from-collaboration",
        headers={"Idempotency-Key": "transfer-once"},
        json={"room_id": "room-secret"},
    )
    assert unauthenticated.status_code == 401
    loader.assert_not_awaited()

    app.dependency_overrides[get_current_user] = lambda: "account-owner"
    created = client.post(
        "/api/v3/trip-understandings/from-collaboration",
        headers={"Idempotency-Key": "transfer-once"},
        json={"room_id": "room-secret"},
    )
    assert created.status_code == 202
    assert created.headers["Location"] == created.json()["result_url"]
    assert "Idempotency-Replayed" not in created.headers
    job = next(iter(repository.jobs.values()))
    assert repository.sources[job["job_id"]].text == source.source_text
    assert repository.progress_internal_bindings[(job["understanding_id"], 1)] == source.internal_binding
    assert "room-secret" not in repr(repository.progress_internal_bindings)

    replay = client.post(
        "/api/v3/trip-understandings/from-collaboration",
        headers={"Idempotency-Key": "transfer-once"},
        json={"room_id": "room-secret"},
    )
    assert replay.status_code == 202
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["public_resource_id"] == created.json()["public_resource_id"]
    assert len(repository.jobs) == 1

    loader.return_value = CollaborationImportSource(
        **{**source.__dict__, "request_hash": "9" * 64},
    )
    changed = client.post(
        "/api/v3/trip-understandings/from-collaboration",
        headers={"Idempotency-Key": "transfer-once"},
        json={"room_id": "room-secret"},
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_collaboration_route_requires_usable_saved_result(monkeypatch) -> None:
    client, _repository, app = _client()
    app.dependency_overrides[get_current_user] = lambda: "account-owner"
    monkeypatch.setattr(
        trip_understandings_v3,
        "load_collaboration_import",
        AsyncMock(side_effect=CollaborationRouteUnavailableError("private detail")),
    )
    response = client.post(
        "/api/v3/trip-understandings/from-collaboration",
        headers={"Idempotency-Key": "transfer-empty"},
        json={"room_id": "room-secret"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "COLLABORATION_ROUTE_UNAVAILABLE",
        "message": "请先在协同规划中保存一条可用路线",
    }
    assert "private detail" not in response.text


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
    assert set(progress.json()) == {
        "status",
        "message",
        "retry_after_ms",
        "phase",
        "event_cursor",
        "progress",
        "snapshot",
    }
    assert progress.json()["phase"] == "RECEIVED"
    assert progress.json()["event_cursor"] == 1
    assert progress.json()["snapshot"] is None
    asyncio.run(TripUnderstandingWorker(repository).run_once("api-test-worker"))

    result = client.get(payload["result_url"])
    assert result.status_code == 200
    assert result.headers["etag"].startswith('"tu3_')
    assert [len(day["activities"]) for day in result.json()["days"]] == [2, 2, 2]
    assert result.json()["map"]["status"] == "PREPARING"
    refreshed = client.get(payload["result_url"])
    assert refreshed.json() == result.json()
    assert refreshed.headers["etag"] == result.headers["etag"]

    event_stream = client.get(payload["events_url"])
    assert event_stream.status_code == 200
    assert "event: progress" in event_stream.text
    assert "event: result_available" in event_stream.text
    event_ids = [
        int(line.removeprefix("id: "))
        for line in event_stream.text.splitlines()
        if line.startswith("id: ")
    ]
    assert event_ids == sorted(set(event_ids))
    resumed = client.get(
        payload["events_url"],
        headers={"Last-Event-ID": str(event_ids[-2])},
    )
    assert "event: progress" not in resumed.text
    assert "event: result_available" in resumed.text
    assert f"id: {event_ids[-1]}" in resumed.text
    for invalid_cursor in ("not-a-number", "-1", str(2**63)):
        invalid_resume = client.get(
            payload["events_url"],
            headers={"Last-Event-ID": invalid_cursor},
        )
        assert invalid_resume.status_code == 400
        assert invalid_resume.json()["detail"]["code"] == "INVALID_EVENT_CURSOR"

    map_preparing = client.get(
        f"/api/v3/trip-understandings/{public_resource_id}/map-renders/latest"
    )
    assert map_preparing.status_code == 200
    assert map_preparing.json()["status"] == "PREPARING"
    asyncio.run(MapRenderWorker(repository).run_once("api-map-worker"))
    map_available = client.get(
        f"/api/v3/trip-understandings/{public_resource_id}/map-renders/latest"
    )
    assert map_available.status_code == 200
    assert map_available.json()["status"] == "AVAILABLE"
    assert [
        route["selected_mode"]
        for day in map_available.json()["days"]
        for route in day["routes"]
    ] == ["walking", "transit", "transit"]
    assert client.get(payload["result_url"]).json()["map"]["status"] == "AVAILABLE"

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
    assert repository.map_job_count == 1

    missing_map_precondition = client.post(
        f"/api/v3/trip-understandings/{public_resource_id}/map-renders",
        headers={"Idempotency-Key": "manual-map-1"},
    )
    assert missing_map_precondition.status_code == 428
    manual_map = client.post(
        f"/api/v3/trip-understandings/{public_resource_id}/map-renders",
        headers={
            "Idempotency-Key": "manual-map-1",
            "If-Match": applied.headers["etag"],
        },
    )
    assert manual_map.status_code == 202
    assert manual_map.json()["status"] == "PREPARING"
    manual_replay = client.post(
        f"/api/v3/trip-understandings/{public_resource_id}/map-renders",
        headers={
            "Idempotency-Key": "manual-map-1",
            "If-Match": applied.headers["etag"],
        },
    )
    assert manual_replay.headers["Idempotency-Replayed"] == "true"
    assert repository.map_job_count == 2
    asyncio.run(MapRenderWorker(repository).run_once("api-map-worker"))
    assert client.get(
        f"/api/v3/trip-understandings/{public_resource_id}/map-renders/latest"
    ).json()["status"] == "LIMITED"

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


def test_cancel_api_handles_empty_draft_progress_and_replay() -> None:
    client, repository, _app = _client()
    empty = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "cancel-api-empty-create"},
        json={"mode": "DEMO"},
    ).json()
    missing_key = client.post(
        f"/api/v3/trip-understandings/{empty['public_resource_id']}/cancel"
    )
    assert missing_key.status_code == 400
    stopped_empty = client.post(
        f"/api/v3/trip-understandings/{empty['public_resource_id']}/cancel",
        headers={"Idempotency-Key": "cancel-api-empty"},
    )
    assert stopped_empty.status_code == 200
    assert stopped_empty.json()["status"] == "STOPPED_EMPTY"
    assert "etag" not in stopped_empty.headers
    cancelled_result = client.get(empty["result_url"])
    assert cancelled_result.status_code == 409
    assert cancelled_result.json()["detail"]["code"] == "UNDERSTANDING_CANCELLED"
    empty_replay = client.post(
        f"/api/v3/trip-understandings/{empty['public_resource_id']}/cancel",
        headers={"Idempotency-Key": "cancel-api-empty"},
    )
    assert empty_replay.headers["Idempotency-Replayed"] == "true"
    assert empty_replay.json() == stopped_empty.json()

    draft = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "cancel-api-draft-create"},
        json={"mode": "DEMO"},
    ).json()

    async def prepare_progress() -> None:
        now = datetime.now(timezone.utc)
        job = await repository.claim_next(
            worker_id="cancel-api-draft-worker",
            now=now,
            lease_seconds=30,
        )
        assert job is not None

        async def persist(update) -> None:
            assert await repository.record_progress(job, update, now=now)

        await TripUnderstandingPipeline(
            FixedBeijingDemoInferenceProvider(),
            FixedBeijingPlaceResolver(),
        ).run(DEMO_SOURCE_TEXT, progress_callback=persist)

    asyncio.run(prepare_progress())
    progress = client.get(draft["result_url"])
    assert progress.status_code == 202
    assert progress.json()["snapshot"] is not None
    assert progress.json()["phase"] in {"CARDS_AVAILABLE", "CHECKING_PLACES"}
    stopped_draft = client.post(
        f"/api/v3/trip-understandings/{draft['public_resource_id']}/cancel",
        headers={"Idempotency-Key": "cancel-api-draft"},
    )
    assert stopped_draft.status_code == 200
    assert stopped_draft.json()["status"] == "STOPPED_WITH_DRAFT"
    assert stopped_draft.headers["etag"].startswith('"tu3_')
    editable = client.get(draft["result_url"])
    assert editable.status_code == 200
    assert editable.headers["etag"] == stopped_draft.headers["etag"]
    assert editable.json()["available_actions"] == [
        "EDIT_ASSUMPTIONS",
        "EDIT_CARDS",
    ]
    assert all(
        card["status"] == "NEEDS_CONFIRMATION"
        for day in editable.json()["days"]
        for card in day["activities"]
    )


def test_public_full_contract_accepts_text_only() -> None:
    client, _repository, app = _client()
    app.dependency_overrides[get_optional_user] = lambda: "text-only-user"

    rejected = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "screenshot-source-rejected"},
        json={
            "mode": "FULL",
            "source": {"type": "SCREENSHOT_BATCH", "batch_ref": "legacy-batch"},
        },
    )

    assert rejected.status_code == 422
    openapi_text = json.dumps(app.openapi(), ensure_ascii=False)
    assert "ScreenshotBatchSourceRequest" not in openapi_text
    assert "screenshot-batches" not in openapi_text

    checked_in = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "packages/trip-check-client/openapi.current.json"
        ).read_text(encoding="utf-8")
    )
    assert all(
        path == "/health"
        or path.startswith(("/api/v3/", "/api/auth/", "/api/user/"))
        for path in checked_in["paths"]
    )
    public_contract = json.dumps(checked_in, ensure_ascii=False)
    for internal_name in (
        "Screenshot",
        "TripWorkspace",
        "AuditReport",
        "EvidenceSnapshot",
        "RepairOption",
        "RunSpec",
    ):
        assert internal_name not in public_contract


def test_full_api_accepts_anonymous_text_and_supports_account_owned_chain() -> None:
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
    assert anonymous.status_code == 202
    assert "HttpOnly" in anonymous.headers["set-cookie"]
    asyncio.run(TripUnderstandingWorker(repository).run_once("anonymous-full-worker"))
    assert client.get(anonymous.json()["result_url"]).json()["ownership"] == "ANONYMOUS"

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


def test_demo_claim_rotates_id_keeps_session_cookie_and_replays_without_cookie() -> None:
    client, repository, app = _client()
    created = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "claim-demo-create"},
        json={"mode": "DEMO"},
    )
    old_id = created.json()["public_resource_id"]
    asyncio.run(TripUnderstandingWorker(repository).run_once("claim-worker"))
    old_result = client.get(created.json()["result_url"])
    assert old_result.status_code == 200

    app.dependency_overrides[get_current_user] = lambda: "user-a"
    app.dependency_overrides[get_optional_user] = lambda: "user-a"
    claimed = client.post(
        f"/api/v3/trip-understandings/{old_id}/claim",
        headers={"Idempotency-Key": "claim-demo"},
    )
    assert claimed.status_code == 200
    new_id = claimed.json()["public_resource_id"]
    assert claimed.json()["status"] == "CLAIMED"
    assert new_id != old_id
    assert claimed.headers["location"].endswith(f"/{new_id}/result")
    assert claimed.headers["etag"] == old_result.headers["etag"]
    assert "set-cookie" not in claimed.headers  # Other anonymous drafts retain access.
    assert client.get(f"/api/v3/trip-understandings/{old_id}/result").status_code == 410
    assert client.get(f"/api/v3/trip-understandings/{new_id}/result").status_code == 200

    client.cookies.clear()
    replay = client.post(
        f"/api/v3/trip-understandings/{old_id}/claim",
        headers={"Idempotency-Key": "claim-demo"},
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["public_resource_id"] == new_id

    app.dependency_overrides[get_current_user] = lambda: "user-b"
    app.dependency_overrides[get_optional_user] = lambda: "user-b"
    assert client.post(
        f"/api/v3/trip-understandings/{old_id}/claim",
        headers={"Idempotency-Key": "claim-demo"},
    ).status_code == 410
    assert client.get(f"/api/v3/trip-understandings/{new_id}/result").status_code == 404


def test_source_delete_keeps_cards_and_is_owner_only() -> None:
    client, repository, app = _client()
    app.dependency_overrides[get_optional_user] = lambda: "user-a"
    app.dependency_overrides[get_current_user] = lambda: "user-a"
    source_text = "Day 1 去故宫博物院和景山公园，Day 2 去天坛公园。"
    created = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "source-delete-create"},
        json={"mode": "FULL", "source": {"type": "TEXT", "text": source_text}},
    )
    resource_id = created.json()["public_resource_id"]
    asyncio.run(TripUnderstandingWorker(repository).run_once("source-delete-worker"))
    before = client.get(created.json()["result_url"])
    assert before.status_code == 200
    assert repository.sources

    deleted = client.delete(
        f"/api/v3/trip-understandings/{resource_id}/source",
        headers={"Idempotency-Key": "source-delete"},
    )
    assert deleted.status_code == 204
    assert not repository.sources
    after = client.get(created.json()["result_url"])
    assert after.status_code == 200
    assert after.json() == before.json()

    replay = client.delete(
        f"/api/v3/trip-understandings/{resource_id}/source",
        headers={"Idempotency-Key": "source-delete"},
    )
    assert replay.status_code == 204
    assert replay.headers["Idempotency-Replayed"] == "true"

    app.dependency_overrides[get_current_user] = lambda: "user-b"
    app.dependency_overrides[get_optional_user] = lambda: "user-b"
    assert client.delete(
        f"/api/v3/trip-understandings/{resource_id}/source",
        headers={"Idempotency-Key": "source-delete-other"},
    ).status_code == 404


def test_trip_delete_scrubs_aggregate_and_requires_authorized_replay() -> None:
    client, repository, app = _client()
    created = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "trip-delete-create"},
        json={"mode": "DEMO"},
    )
    resource_id = created.json()["public_resource_id"]
    asyncio.run(TripUnderstandingWorker(repository).run_once("trip-delete-worker"))
    before = client.get(created.json()["result_url"])
    token = before.json()["days"][0]["activities"][0]["activity_token"]

    deleted = client.delete(
        f"/api/v3/trip-understandings/{resource_id}",
        headers={"Idempotency-Key": "trip-delete"},
    )
    assert deleted.status_code == 204
    assert resource_id not in repository.resources
    assert not repository.results
    assert not repository.jobs
    assert not repository.events
    assert not repository.sources
    assert repository.side_effect_count == 0
    assert client.get(created.json()["result_url"]).status_code == 410
    assert client.get(created.json()["events_url"]).status_code == 410
    assert client.post(
        f"/api/v3/trip-understandings/{resource_id}/commands",
        headers={
            "Idempotency-Key": "after-delete-command",
            "If-Match": before.headers["etag"],
        },
        json={"command_type": "ACTIVITY_DELETE", "activity_token": token},
    ).status_code == 410

    replay = client.delete(
        f"/api/v3/trip-understandings/{resource_id}",
        headers={"Idempotency-Key": "trip-delete"},
    )
    assert replay.status_code == 204
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert client.delete(
        f"/api/v3/trip-understandings/{resource_id}",
        headers={"Idempotency-Key": "unknown-delete"},
    ).status_code == 410

    other_browser = TestClient(app)
    assert other_browser.delete(
        f"/api/v3/trip-understandings/{resource_id}",
        headers={"Idempotency-Key": "trip-delete"},
    ).status_code == 410


def test_account_travel_delete_cascades_owned_resources_and_has_safe_status() -> None:
    client, repository, app = _client()
    app.dependency_overrides[get_optional_user] = lambda: "user-account-delete"
    app.dependency_overrides[get_current_user] = lambda: "user-account-delete"
    app.dependency_overrides[get_recent_user] = lambda: "user-account-delete"
    resource_ids: list[str] = []
    for index, place in enumerate(("故宫博物院", "天坛公园"), start=1):
        created = client.post(
            "/api/v3/trip-understandings",
            headers={"Idempotency-Key": f"account-delete-create-{index}"},
            json={
                "mode": "FULL",
                "source": {"type": "TEXT", "text": f"Day 1 去{place}"},
            },
        )
        resource_ids.append(created.json()["public_resource_id"])
        asyncio.run(
            TripUnderstandingWorker(repository).run_once(f"account-delete-worker-{index}")
        )

    assert client.request(
        "DELETE",
        "/api/v3/me/travel-data",
        headers={"Idempotency-Key": "account-delete-invalid"},
        json={"confirmation": "DELETE"},
    ).status_code == 422
    deleted = client.request(
        "DELETE",
        "/api/v3/me/travel-data",
        headers={"Idempotency-Key": "account-delete"},
        json={"confirmation": "DELETE_ALL_TRAVEL_DATA"},
    )
    assert deleted.status_code == 202
    assert deleted.json() == {
        "status": "COMPLETED",
        "message": "旅行数据已清空",
        "next_action": "NONE",
    }
    assert deleted.headers["location"] == "/api/v3/me/travel-data-deletion"
    assert not repository.resources
    assert not repository.results
    assert not repository.jobs
    assert not repository.sources
    for resource_id in resource_ids:
        assert client.get(
            f"/api/v3/trip-understandings/{resource_id}/result"
        ).status_code == 410

    status_view = client.get("/api/v3/me/travel-data-deletion")
    assert status_view.status_code == 200
    assert status_view.json() == deleted.json()
    replay = client.request(
        "DELETE",
        "/api/v3/me/travel-data",
        headers={"Idempotency-Key": "account-delete"},
        json={"confirmation": "DELETE_ALL_TRAVEL_DATA"},
    )
    assert replay.status_code == 202
    assert replay.headers["Idempotency-Replayed"] == "true"
    retained_internal_state = repr(
        (
            repository.privacy_idempotency,
            repository.account_deletion_status,
            repository.tombstones,
        )
    )
    assert "user-account-delete" not in retained_internal_state


def test_account_travel_delete_requires_login_minted_within_ten_minutes() -> None:
    client, _, _ = _client()
    body = {"confirmation": "DELETE_ALL_TRAVEL_DATA"}
    assert client.request(
        "DELETE",
        "/api/v3/me/travel-data",
        headers={"Idempotency-Key": "fresh-login-missing"},
        json=body,
    ).status_code == 401

    now = datetime.now(timezone.utc)
    stale_token = jwt.encode(
        {
            "sub": "fresh-login-user",
            "iat": now - timedelta(minutes=11),
            "exp": now + timedelta(days=1),
        },
        auth_utils.settings.jwt_secret_key,
        algorithm="HS256",
    )
    stale = client.request(
        "DELETE",
        "/api/v3/me/travel-data",
        headers={
            "Authorization": f"Bearer {stale_token}",
            "Idempotency-Key": "fresh-login-stale",
        },
        json=body,
    )
    assert stale.status_code == 401
    assert stale.json()["detail"] == "请重新登录后再清空旅行数据"

    fresh = client.request(
        "DELETE",
        "/api/v3/me/travel-data",
        headers={
            "Authorization": f"Bearer {auth_utils.create_token('fresh-login-user')}",
            "Idempotency-Key": "fresh-login-valid",
        },
        json=body,
    )
    assert fresh.status_code == 202
    assert fresh.json()["status"] == "COMPLETED"
