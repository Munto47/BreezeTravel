from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import imports as imports_api
from app.api import trip_briefs as trip_briefs_api
from app.importing.repositories import InMemoryImportRepository
from app.itineraries.models import TripDateRange, TripWorkspace
from app.itineraries.repositories import InMemoryItineraryRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.trip_check.briefs import InMemoryTripBriefRepository
from app.utils.auth import get_current_user


class ControlledProvider:
    async def search(self, *, query: str, city: str):
        return [
            {
                "place_id": f"fixture-{query}",
                "name": query,
                "city": city,
                "category": "attraction",
                "address": "受控地址",
                "coords": {"lng": 116.397, "lat": 39.918},
                "retrieval_provider": "controlled_test",
                "retrieval_request_hash": "1" * 64,
                "execution_mode": "fixture",
                "retrieval_response_hash": "2" * 64,
                "retrieval_observed_at": "2026-08-22T00:00:00+00:00",
            }
        ]


def _client(monkeypatch):
    itinerary_repository = InMemoryItineraryRepository()
    asyncio.run(
        itinerary_repository.create_workspace(
            TripWorkspace(
                workspace_id="brief-workspace",
                room_id="brief-room",
                city="北京",
                trip_date_range=TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2)),
                created_by="brief-user",
            )
        )
    )
    import_repository = InMemoryImportRepository(itinerary_repository)
    command_repository = InMemoryCreationCommandRepository()
    brief_repository = InMemoryTripBriefRepository()
    app = FastAPI()
    app.include_router(imports_api.router, prefix="/api")
    app.include_router(trip_briefs_api.router, prefix="/api")
    app.dependency_overrides[imports_api.get_itinerary_repository] = lambda: itinerary_repository
    app.dependency_overrides[imports_api.get_import_repository] = lambda: import_repository
    app.dependency_overrides[imports_api.get_creation_command_repository] = lambda: command_repository
    app.dependency_overrides[imports_api.get_trip_brief_repository] = lambda: brief_repository
    app.dependency_overrides[imports_api.get_entity_candidate_provider] = lambda: ControlledProvider()
    app.dependency_overrides[trip_briefs_api.get_itinerary_repository] = lambda: itinerary_repository
    app.dependency_overrides[trip_briefs_api.get_trip_brief_repository] = lambda: brief_repository
    app.dependency_overrides[get_current_user] = lambda: "brief-user"
    monkeypatch.setattr(imports_api, "require_room_member", AsyncMock(return_value=None))
    monkeypatch.setattr(trip_briefs_api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app), brief_repository


def test_text_import_creates_versioned_brief_then_patch_confirm_and_replay(monkeypatch):
    client, repository = _client(monkeypatch)
    imported = client.post(
        "/api/trip-workspaces/brief-workspace/imports",
        headers={"Idempotency-Key": "create-import-and-brief"},
        json={
            "source_type": "MANUAL_TEXT",
            "raw_text": "北京3人，第1天步行去故宫博物院，第2天地铁去颐和园",
        },
    )
    assert imported.status_code == 201
    brief = asyncio.run(repository.get_latest_brief("brief-workspace"))
    assert brief is not None
    assert brief.revision == 1
    assert brief.traveler_count == 3
    assert brief.status.value == "NEEDS_CONFIRMATION"

    read = client.get("/api/trip-workspaces/brief-workspace/trip-briefs/1")
    assert read.status_code == 200
    assert read.headers["etag"] == '"1"'
    assert read.json()["field_provenance"]["traveler_count"]["source_spans"]

    missing_precondition = client.patch(
        "/api/trip-workspaces/brief-workspace/trip-briefs/1",
        headers={"Idempotency-Key": "patch-brief"},
        json={"updates": {"budget": {"currency": "CNY", "total": 3000}}},
    )
    assert missing_precondition.status_code == 428
    assert missing_precondition.json()["detail"]["code"] == "IF_MATCH_REQUIRED"

    headers = {"If-Match": '"1"', "Idempotency-Key": "patch-brief"}
    patched = client.patch(
        "/api/trip-workspaces/brief-workspace/trip-briefs/1",
        headers=headers,
        json={"updates": {"budget": {"currency": "CNY", "total": 3000}}},
    )
    replay = client.patch(
        "/api/trip-workspaces/brief-workspace/trip-briefs/1",
        headers=headers,
        json={"updates": {"budget": {"currency": "CNY", "total": 3000}}},
    )
    assert patched.status_code == replay.status_code == 200
    assert patched.json()["revision"] == 2
    assert replay.json() == patched.json()
    assert replay.headers["Idempotency-Replayed"] == "true"

    reused = client.patch(
        "/api/trip-workspaces/brief-workspace/trip-briefs/1",
        headers=headers,
        json={"updates": {"budget": {"currency": "CNY", "total": 5000}}},
    )
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    stale = client.patch(
        "/api/trip-workspaces/brief-workspace/trip-briefs/1",
        headers={"If-Match": '"1"', "Idempotency-Key": "stale-patch"},
        json={"updates": {"daily_pace": "RELAXED"}},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "TRIP_BRIEF_REVISION_CONFLICT"

    confirmed = client.post(
        "/api/trip-workspaces/brief-workspace/trip-briefs/2/confirm",
        headers={"If-Match": '"2"', "Idempotency-Key": "confirm-brief"},
    )
    assert confirmed.status_code == 200
    assert confirmed.headers["etag"] == '"3"'
    assert confirmed.json()["status"] == "CONFIRMED"
    assert all(
        field["confirmation"] == "CONFIRMED"
        for field in confirmed.json()["field_provenance"].values()
    )

    old = client.get("/api/trip-workspaces/brief-workspace/trip-briefs/1")
    assert old.status_code == 200
    assert old.json()["status"] == "NEEDS_CONFIRMATION"


def test_trip_brief_patch_rejects_unknown_or_confirmed_fields(monkeypatch):
    client, _ = _client(monkeypatch)
    client.post(
        "/api/trip-workspaces/brief-workspace/imports",
        headers={"Idempotency-Key": "create-import-and-brief"},
        json={"source_type": "MANUAL_TEXT", "raw_text": "北京2人，第1天故宫，第2天颐和园"},
    )
    unsupported = client.patch(
        "/api/trip-workspaces/brief-workspace/trip-briefs/1",
        headers={"If-Match": '"1"', "Idempotency-Key": "unsupported"},
        json={"updates": {"canonical_place_id": "client-forged"}},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["detail"]["code"] == "INVALID_TRIP_BRIEF_PATCH"

    confirmed = client.post(
        "/api/trip-workspaces/brief-workspace/trip-briefs/1/confirm",
        headers={"If-Match": '"1"', "Idempotency-Key": "confirm"},
    )
    assert confirmed.status_code == 200
    rejected = client.patch(
        "/api/trip-workspaces/brief-workspace/trip-briefs/2",
        headers={"If-Match": '"2"', "Idempotency-Key": "edit-confirmed"},
        json={"updates": {"traveler_count": 4}},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "TRIP_BRIEF_ALREADY_CONFIRMED"
