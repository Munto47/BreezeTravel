from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import imports as imports_api
from app.agents.nodes import amap_search
from app.importing.repositories import InMemoryImportRepository
from app.itineraries.models import TripDateRange, TripWorkspace
from app.itineraries.repositories import InMemoryItineraryRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.trip_check.briefs import InMemoryTripBriefRepository
from app.utils.auth import get_current_user


class FakeProvider:
    async def search(self, *, query: str, city: str):
        def candidate(place_id: str, name: str, *, candidate_city: str | None = None):
            return {
                "place_id": place_id,
                "name": name,
                "city": candidate_city or city,
                "category": "attraction",
                "address": "受控测试地址",
                "coords": {"lng": 116.397, "lat": 39.918},
                "retrieval_provider": "controlled_test",
                "retrieval_request_hash": "2" * 64,
                "execution_mode": "fixture",
                "retrieval_response_hash": "b" * 64,
                "retrieval_observed_at": "2026-08-21T00:00:00+00:00",
            }
        mapping = {
            "故宫博物院": [
                candidate("gugong-a", "故宫博物院"),
                candidate("gugong-b", "故宫博物院"),
            ],
            "颐和园": [
                candidate("summer-palace", "颐和园"),
            ],
            "景山公园": [
                candidate("jingshan-a", "景山公园"),
                candidate("jingshan-b", "景山公园"),
            ],
            "东方明珠": [
                candidate("shanghai-tower", "东方明珠", candidate_city="上海市"),
            ],
        }
        return mapping.get(query, [])


def _client(monkeypatch):
    itinerary_repository = InMemoryItineraryRepository()
    workspace = TripWorkspace(
        workspace_id="workspace-import-api",
        room_id="room-import-api",
        city="北京",
        trip_date_range=TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
        created_by="import-user",
    )
    asyncio.run(itinerary_repository.create_workspace(workspace))
    import_repository = InMemoryImportRepository(itinerary_repository)
    command_repository = InMemoryCreationCommandRepository()
    trip_brief_repository = InMemoryTripBriefRepository()
    app = FastAPI()
    app.include_router(imports_api.router, prefix="/api")
    app.dependency_overrides[imports_api.get_itinerary_repository] = lambda: itinerary_repository
    app.dependency_overrides[imports_api.get_import_repository] = lambda: import_repository
    app.dependency_overrides[imports_api.get_entity_candidate_provider] = lambda: FakeProvider()
    app.dependency_overrides[imports_api.get_creation_command_repository] = lambda: command_repository
    app.dependency_overrides[imports_api.get_trip_brief_repository] = lambda: trip_brief_repository
    app.dependency_overrides[get_current_user] = lambda: "import-user"
    monkeypatch.setattr(imports_api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app), itinerary_repository


def test_import_api_requires_resolution_then_applies_revision(monkeypatch):
    client, itinerary_repository = _client(monkeypatch)
    created = client.post(
        "/api/trip-workspaces/workspace-import-api/imports",
        headers={"Idempotency-Key": "import-create"},
        json={
            "source_type": "AI_TEXT",
            "raw_text": "第1天：09:00-11:00 故宫博物院\n第2天：09:00-11:00 颐和园",
        },
    )
    assert created.status_code == 201
    itinerary_import = created.json()
    assert itinerary_import["status"] == "NEEDS_RESOLUTION"
    ambiguous = next(item for item in itinerary_import["resolutions"] if item["resolution_status"] == "AMBIGUOUS")

    rejected = client.post(
        f"/api/trip-workspaces/workspace-import-api/imports/{itinerary_import['import_id']}/apply",
        headers={"If-Match": '"2"'},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "DRAFT_AMBIGUOUS"

    confirmed = client.patch(
        f"/api/trip-workspaces/workspace-import-api/imports/{itinerary_import['import_id']}/resolutions",
        headers={"If-Match": '"2"'},
        json={
            "confirmations": [
                {
                    "raw_stop_id": ambiguous["raw_stop_id"],
                    "place_id": ambiguous["candidates"][0]["place_id"],
                }
            ],
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "READY"

    applied = client.post(
        f"/api/trip-workspaces/workspace-import-api/imports/{itinerary_import['import_id']}/apply",
        headers={"If-Match": '"3"'},
    )
    assert applied.status_code == 200
    assert applied.json()["itinerary_import"]["status"] == "APPLIED"
    assert applied.json()["revision"]["revision"] == 1
    assert len(applied.json()["resolved_place_receipts"]) == 2
    assert len(applied.json()["revision"]["change_summary"]["map_stop_projections"]) == 2
    workspace = asyncio.run(itinerary_repository.get_workspace("workspace-import-api"))
    assert workspace.current_itinerary_revision == 1


def test_local_fixture_profile_completes_text_import_with_explicit_fixture_provenance(monkeypatch):
    client, _ = _client(monkeypatch)
    client.app.dependency_overrides[imports_api.get_entity_candidate_provider] = (
        lambda: imports_api.AmapEntityCandidateProvider()
    )
    with (
        patch.object(amap_search.settings, "runtime_profile", "local_fixture"),
        patch.object(amap_search.settings, "demo_mode", False),
        patch.object(amap_search.settings, "amap_mock", True),
    ):
        response = client.post(
            "/api/trip-workspaces/workspace-import-api/imports",
            headers={"Idempotency-Key": "fixture-import"},
            json={
                "source_type": "AI_TEXT",
                "raw_text": (
                    "Day 1 北京\n09:00-11:00 故宫博物院\n12:00-14:00 景山公园"
                    "\nDay 2 北京\n09:00-11:00 颐和园"
                ),
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert [item["raw_name"] for item in body["raw_stops"]] == ["故宫博物院", "景山公园", "颐和园"]
    assert all(item["resolution_status"] == "AUTO_MATCHED" for item in body["resolutions"])
    assert all(item["candidates"][0]["execution_mode"] == "fixture" for item in body["resolutions"])
    assert all(item["candidates"][0]["retrieval_provider"] == "amap_fixture" for item in body["resolutions"])


def test_import_parse_failure_is_returned_as_editable_draft(monkeypatch):
    client, _ = _client(monkeypatch)
    response = client.post(
        "/api/trip-workspaces/workspace-import-api/imports",
        headers={"Idempotency-Key": "import-create"},
        json={"source_type": "MANUAL_TEXT", "raw_text": "说明：地点还没确定"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "FAILED"
    assert response.json()["raw_stops"] == []
    assert response.json()["parse_errors"] == ["IMPORT_PARSE_FAILED"]


def test_wrong_city_not_found_exposes_auditable_receipt_but_cannot_be_confirmed(monkeypatch):
    client, _ = _client(monkeypatch)
    created_response = client.post(
        "/api/trip-workspaces/workspace-import-api/imports",
        headers={"Idempotency-Key": "wrong-city-receipt"},
        json={"source_type": "MANUAL_TEXT", "raw_text": "第1天：09:00-11:00 东方明珠"},
    )
    assert created_response.status_code == 201
    created = created_response.json()
    resolution = created["resolutions"][0]
    assert resolution["resolution_status"] == "NOT_FOUND"
    assert resolution["candidates"] == []
    assert len(resolution["rejected_candidates"]) == 1
    rejected = resolution["rejected_candidates"][0]
    assert rejected["place_id"] == "shanghai-tower"
    assert rejected["reason"] == "WRONG_CITY"
    assert rejected["target_city"] == "北京"
    receipt = rejected["resolved_place_receipt"]
    assert receipt["city"] == "上海市"
    assert receipt["request_hash"] == "2" * 64
    assert receipt["response_hash"] == "b" * 64

    readback = client.get(
        f"/api/trip-workspaces/workspace-import-api/imports/{created['import_id']}"
    )
    assert readback.status_code == 200
    assert readback.json()["resolutions"][0]["rejected_candidates"] == [rejected]

    confirmation = client.patch(
        f"/api/trip-workspaces/workspace-import-api/imports/{created['import_id']}/resolutions",
        headers={"If-Match": f'"{created["state_version"]}"'},
        json={
            "confirmations": [
                {
                    "raw_stop_id": resolution["raw_stop_id"],
                    "place_id": rejected["place_id"],
                }
            ]
        },
    )
    assert confirmation.status_code == 422
    assert confirmation.json()["detail"]["code"] == "INVALID_ITINERARY_EDIT_COMMAND"


def test_import_create_requires_key_and_replays_same_resource(monkeypatch):
    client, itinerary_repository = _client(monkeypatch)
    path = "/api/trip-workspaces/workspace-import-api/imports"
    body = {"source_type": "AI_TEXT", "raw_text": "第1天：09:00-11:00 颐和园"}
    missing = client.post(path, json=body)
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    headers = {"Idempotency-Key": "import-reliable-retry"}
    first = client.post(path, json=body, headers=headers)
    replay = client.post(path, json=body, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    workspace = asyncio.run(itinerary_repository.get_workspace("workspace-import-api"))
    assert workspace.current_import_id == first.json()["import_id"]

    reused = client.post(
        path,
        json={**body, "raw_text": "第1天：天坛"},
        headers=headers,
    )
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_not_found_stop_can_search_new_candidates_without_mutating_other_versions(monkeypatch):
    client, _ = _client(monkeypatch)
    created = client.post(
        "/api/trip-workspaces/workspace-import-api/imports",
        headers={"Idempotency-Key": "import-create"},
        json={
            "source_type": "AI_TEXT",
            "raw_text": "第1天：09:00-11:00 不存在地点\n第2天：09:00-11:00 颐和园",
        },
    ).json()
    missing = next(item for item in created["resolutions"] if item["resolution_status"] == "NOT_FOUND")
    resolved = next(item for item in created["resolutions"] if item["resolution_status"] == "AUTO_MATCHED")
    assert missing["resolution_version"] == 1
    assert resolved["resolution_version"] == 1

    searched = client.post(
        (
            f"/api/trip-workspaces/workspace-import-api/imports/{created['import_id']}"
            f"/raw-stops/{missing['raw_stop_id']}/candidates:search"
        ),
        headers={"If-Match": '"2"'},
        json={"query": "景山公园"},
    )
    assert searched.status_code == 200
    search_body = searched.json()
    retried = next(item for item in search_body["resolutions"] if item["raw_stop_id"] == missing["raw_stop_id"])
    untouched = next(item for item in search_body["resolutions"] if item["raw_stop_id"] == resolved["raw_stop_id"])
    assert retried["resolution_status"] == "AMBIGUOUS"
    assert len(retried["candidates"]) == 2
    assert retried["resolution_version"] == 2
    assert untouched["resolution_version"] == 1

    confirmed = client.patch(
        f"/api/trip-workspaces/workspace-import-api/imports/{created['import_id']}/resolutions",
        headers={"If-Match": '"3"'},
        json={
            "confirmations": [
                {
                    "raw_stop_id": retried["raw_stop_id"],
                    "place_id": retried["candidates"][0]["place_id"],
                }
            ],
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "READY"


def test_candidate_search_rejects_raw_stop_outside_import(monkeypatch):
    client, _ = _client(monkeypatch)
    created = client.post(
        "/api/trip-workspaces/workspace-import-api/imports",
        headers={"Idempotency-Key": "import-create"},
        json={"source_type": "AI_TEXT", "raw_text": "第1天：不存在地点"},
    ).json()
    response = client.post(
        (
            f"/api/trip-workspaces/workspace-import-api/imports/{created['import_id']}"
            "/raw-stops/not-in-import/candidates:search"
        ),
        headers={"If-Match": '"2"'},
        json={"query": "颐和园"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RESOURCE_NOT_FOUND"


def test_batch_resolution_confirmation_is_atomic(monkeypatch):
    client, _ = _client(monkeypatch)
    created = client.post(
        "/api/trip-workspaces/workspace-import-api/imports",
        headers={"Idempotency-Key": "import-create"},
        json={
            "source_type": "AI_TEXT",
            "raw_text": "第1天：09:00-11:00 故宫博物院\n第2天：09:00-11:00 景山公园",
        },
    ).json()
    ambiguous = [item for item in created["resolutions"] if item["resolution_status"] == "AMBIGUOUS"]
    assert len(ambiguous) == 2

    rejected = client.patch(
        f"/api/trip-workspaces/workspace-import-api/imports/{created['import_id']}/resolutions",
        headers={"If-Match": '"2"'},
        json={
            "confirmations": [
                {
                    "raw_stop_id": ambiguous[0]["raw_stop_id"],
                    "place_id": ambiguous[0]["candidates"][0]["place_id"],
                },
                {"raw_stop_id": ambiguous[1]["raw_stop_id"], "place_id": "not-offered"},
            ],
        },
    )
    assert rejected.status_code == 422

    readback = client.get(f"/api/trip-workspaces/workspace-import-api/imports/{created['import_id']}").json()
    assert all(item["resolution_status"] == "AMBIGUOUS" for item in readback["resolutions"])
    assert all(item["resolution_version"] == 1 for item in readback["resolutions"])


def test_batch_resolution_rejects_duplicate_raw_stop_ids(monkeypatch):
    client, _ = _client(monkeypatch)
    created = client.post(
        "/api/trip-workspaces/workspace-import-api/imports",
        headers={"Idempotency-Key": "import-create"},
        json={"source_type": "AI_TEXT", "raw_text": "第1天：09:00-11:00 故宫博物院"},
    ).json()
    ambiguous = created["resolutions"][0]
    confirmation = {
        "raw_stop_id": ambiguous["raw_stop_id"],
        "place_id": ambiguous["candidates"][0]["place_id"],
    }
    response = client.patch(
        f"/api/trip-workspaces/workspace-import-api/imports/{created['import_id']}/resolutions",
        json={"confirmations": [confirmation, confirmation]},
    )
    assert response.status_code == 422
