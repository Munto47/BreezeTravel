from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import imports as imports_api
from app.importing.repositories import InMemoryImportRepository
from app.itineraries.models import TripDateRange, TripWorkspace
from app.itineraries.repositories import InMemoryItineraryRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.trip_check.briefs import InMemoryTripBriefRepository
from app.utils.auth import get_current_user


class MobileImportProvider:
    async def search(self, *, query: str, city: str):
        def candidate(place_id: str):
            return {
                "place_id": place_id,
                "name": query,
                "city": city,
                "category": "attraction",
                "address": "受控测试地址",
                "coords": {"lng": 116.397, "lat": 39.918},
                "retrieval_provider": "controlled_test",
                "retrieval_request_hash": "3" * 64,
                "execution_mode": "fixture",
                "retrieval_response_hash": "c" * 64,
                "retrieval_observed_at": "2026-08-21T00:00:00+00:00",
            }
        if query == "故宫博物院":
            return [
                candidate("gugong-a"),
                candidate("gugong-b"),
            ]
        if query == "景山公园":
            return [candidate("jingshan")]
        return []


def _client(monkeypatch) -> tuple[TestClient, InMemoryImportRepository]:
    itinerary_repository = InMemoryItineraryRepository()
    asyncio.run(
        itinerary_repository.create_workspace(
            TripWorkspace(
                workspace_id="mobile-import-workspace",
                room_id="mobile-import-room",
                city="北京",
                trip_date_range=TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2)),
                created_by="mobile-import-user",
            )
        )
    )
    import_repository = InMemoryImportRepository(itinerary_repository)
    command_repository = InMemoryCreationCommandRepository()
    trip_brief_repository = InMemoryTripBriefRepository()
    app = FastAPI()
    app.include_router(imports_api.router, prefix="/api")
    app.dependency_overrides[imports_api.get_itinerary_repository] = lambda: itinerary_repository
    app.dependency_overrides[imports_api.get_import_repository] = lambda: import_repository
    app.dependency_overrides[imports_api.get_creation_command_repository] = lambda: command_repository
    app.dependency_overrides[imports_api.get_trip_brief_repository] = lambda: trip_brief_repository
    app.dependency_overrides[imports_api.get_entity_candidate_provider] = lambda: MobileImportProvider()
    app.dependency_overrides[get_current_user] = lambda: "mobile-import-user"
    monkeypatch.setattr(imports_api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app), import_repository


def _create_ambiguous(client: TestClient, *, idempotency_key: str = "mobile-create-ambiguous"):
    response = client.post(
        "/api/trip-workspaces/mobile-import-workspace/imports",
        json={
            "source_type": "AI_TEXT",
            "raw_text": "第1天：09:00-11:00 故宫博物院\n第2天：09:00-11:00 景山公园",
        },
        headers={"Idempotency-Key": idempotency_key},
    )
    assert response.status_code == 201
    assert response.headers["etag"] == '"2"'
    return response.json()


def test_same_import_state_can_only_be_confirmed_once(monkeypatch):
    client, _ = _client(monkeypatch)
    created = _create_ambiguous(client)
    ambiguous = next(item for item in created["resolutions"] if item["resolution_status"] == "AMBIGUOUS")
    url = f"/api/trip-workspaces/mobile-import-workspace/imports/{created['import_id']}/resolutions"

    first = client.patch(
        url,
        headers={"If-Match": 'W/"2"'},
        json={"confirmations": [{"raw_stop_id": ambiguous["raw_stop_id"], "place_id": "gugong-a"}]},
    )
    second = client.patch(
        url,
        headers={"If-Match": '"2"'},
        json={"confirmations": [{"raw_stop_id": ambiguous["raw_stop_id"], "place_id": "gugong-b"}]},
    )

    assert first.status_code == 200
    assert first.headers["etag"] == '"3"'
    assert second.status_code == 409
    assert second.json()["detail"] == {
        "code": "IMPORT_STATE_CONFLICT",
        "message": "import state version is stale",
        "expected_state_version": 2,
        "actual_state_version": 3,
    }


def test_import_mutations_require_if_match(monkeypatch):
    client, _ = _client(monkeypatch)
    created = _create_ambiguous(client)
    ambiguous = next(item for item in created["resolutions"] if item["resolution_status"] == "AMBIGUOUS")
    base = f"/api/trip-workspaces/mobile-import-workspace/imports/{created['import_id']}"
    requests = [
        client.patch(
            f"{base}/resolutions",
            json={
                "confirmations": [
                    {
                        "raw_stop_id": ambiguous["raw_stop_id"],
                        "place_id": "gugong-a",
                    }
                ],
            },
        ),
        client.post(
            f"{base}/raw-stops/{ambiguous['raw_stop_id']}/candidates:search",
            json={"query": "景山公园"},
        ),
        client.post(f"{base}/apply"),
    ]

    for response in requests:
        assert response.status_code == 428
        assert response.json()["detail"] == {
            "code": "IF_MATCH_REQUIRED",
            "message": "If-Match header is required",
        }
    readback = client.get(base).json()
    assert readback["state_version"] == created["state_version"]


def test_apply_replays_after_lost_response_and_terminal_draft_mutations_fail_closed(monkeypatch):
    client, _ = _client(monkeypatch)
    created = _create_ambiguous(client)
    ambiguous = next(item for item in created["resolutions"] if item["resolution_status"] == "AMBIGUOUS")
    base = f"/api/trip-workspaces/mobile-import-workspace/imports/{created['import_id']}"
    confirmed = client.patch(
        f"{base}/resolutions",
        headers={"If-Match": '"2"'},
        json={"confirmations": [{"raw_stop_id": ambiguous["raw_stop_id"], "place_id": "gugong-a"}]},
    )
    assert confirmed.status_code == 200

    headers = {"If-Match": '"3"', "Idempotency-Key": "mobile-apply-1"}
    first = client.post(f"{base}/apply", headers=headers)
    replay = client.post(f"{base}/apply", headers=headers)
    assert first.status_code == replay.status_code == 200
    assert first.json()["revision"]["content_hash"] == replay.json()["revision"]["content_hash"]
    assert replay.json()["idempotent_replay"] is True
    assert replay.headers["etag"] == '"4"'

    reused_for_different_precondition = client.post(
        f"{base}/apply",
        headers={"If-Match": '"4"', "Idempotency-Key": "mobile-apply-1"},
    )
    assert reused_for_different_precondition.status_code == 409
    assert reused_for_different_precondition.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    reconfirm = client.patch(
        f"{base}/resolutions",
        headers={"If-Match": '"4"'},
        json={"confirmations": [{"raw_stop_id": ambiguous["raw_stop_id"], "place_id": "gugong-b"}]},
    )
    retry = client.post(
        f"{base}/raw-stops/{ambiguous['raw_stop_id']}/candidates:search",
        headers={"If-Match": '"4"'},
        json={"query": "景山公园"},
    )
    assert reconfirm.status_code == retry.status_code == 409
    assert reconfirm.json()["detail"]["code"] == "INVALID_IMPORT_STATE"
    assert retry.json()["detail"]["code"] == "INVALID_IMPORT_STATE"


def test_workspace_can_discover_latest_unfinished_import_with_bounded_list(monkeypatch):
    client, _ = _client(monkeypatch)
    first = client.post(
        "/api/trip-workspaces/mobile-import-workspace/imports",
        json={"source_type": "MANUAL_TEXT", "raw_text": "说明：地点还没确定"},
        headers={"Idempotency-Key": "mobile-create-unfinished-first"},
    ).json()
    second = _create_ambiguous(client, idempotency_key="mobile-create-unfinished-second")

    listing = client.get(
        "/api/trip-workspaces/mobile-import-workspace/imports",
        params={"limit": 1, "unfinished_only": True},
    )
    latest = client.get("/api/trip-workspaces/mobile-import-workspace/imports/latest")
    assert listing.status_code == latest.status_code == 200
    assert listing.json()["limit"] == 1
    assert len(listing.json()["items"]) == 1
    assert latest.json()["import_id"] in {first["import_id"], second["import_id"]}
    assert latest.headers["etag"]


def test_failed_import_mutation_uses_stable_machine_code(monkeypatch):
    client, _ = _client(monkeypatch)
    failed = client.post(
        "/api/trip-workspaces/mobile-import-workspace/imports",
        json={"source_type": "MANUAL_TEXT", "raw_text": "说明：地点还没确定"},
        headers={"Idempotency-Key": "mobile-create-failed"},
    ).json()
    response = client.post(
        (
            f"/api/trip-workspaces/mobile-import-workspace/imports/{failed['import_id']}"
            "/raw-stops/unknown/candidates:search"
        ),
        headers={"If-Match": '"1"'},
        json={"query": "景山公园"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "INVALID_IMPORT_STATE"
