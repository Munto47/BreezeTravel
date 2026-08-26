from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trip_workspaces as workspace_api
from app.audit.repositories import InMemoryAuditRepository
from app.audit.service import AuditApplicationService
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    RevisionTransport,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.constraints.geo_routes import RouteResult
from app.operations.repositories import InMemoryCreationCommandRepository
from app.suggestions.repositories import InMemorySuggestionRepository
from app.utils.auth import get_current_user


def _seed_repository() -> InMemoryItineraryRepository:
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="itin-api",
        workspace_id="workspace-api",
        revision=1,
        source_type=RevisionSource.MANUAL,
        city="北京",
        date_range=date_range,
        days=[
            ItineraryDay(day_index=0, date=date_range.start, stops=[
                ItineraryStop(
                    stop_id="stop-api-1",
                    place_id="place-api-1",
                    day_index=0,
                    order_index=0,
                    start_time="09:00",
                    end_time="11:00",
                    transport_to_next=RevisionTransport(duration_minutes=10, distance_meters=1000),
                ),
                ItineraryStop(
                    stop_id="stop-api-2", place_id="place-api-2", day_index=0, order_index=1,
                    start_time="11:30", end_time="12:30",
                    transport_to_next=RevisionTransport(duration_minutes=20, distance_meters=3000),
                ),
                ItineraryStop(
                    stop_id="stop-api-3", place_id="place-api-3", day_index=0, order_index=2,
                    start_time="13:00", end_time="14:00",
                ),
            ]),
            ItineraryDay(day_index=1, date=date_range.end, stops=[]),
        ],
        change_summary={
            "map_stop_projections": {
                f"stop-api-{index}": {
                    "place": {"place_id": f"place-api-{index}", "coords": {"lng": 116.3 + index / 100, "lat": 39.9 + index / 100}},
                    "coordinate_role": "CONTROLLED_TEST", "provenance": "CONTROLLED_TEST",
                }
                for index in (1, 2, 3)
            },
        },
        created_by="user-api",
    ))
    workspace = TripWorkspace(
        workspace_id="workspace-api",
        room_id="room-api",
        city="北京",
        trip_date_range=date_range,
        current_itinerary_revision=1,
        current_report_id="old-report",
        created_by="user-api",
    )
    repository = InMemoryItineraryRepository()
    asyncio.run(repository.create_workspace(workspace, revision))
    return repository


def _client(monkeypatch):
    repository = _seed_repository()
    audit_repository = InMemoryAuditRepository(repository.workspaces)
    suggestion_repository = InMemorySuggestionRepository(repository)
    app = FastAPI()
    app.include_router(workspace_api.router, prefix="/api")
    app.dependency_overrides[workspace_api.get_itinerary_repository] = lambda: repository
    app.dependency_overrides[workspace_api.get_audit_repository] = lambda: audit_repository
    app.dependency_overrides[workspace_api.get_suggestion_repository] = lambda: suggestion_repository
    app.dependency_overrides[get_current_user] = lambda: "user-api"
    monkeypatch.setattr(workspace_api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app), repository, audit_repository


def test_revision_api_edit_and_cross_client_readback(monkeypatch):
    client, _, _ = _client(monkeypatch)
    response = client.post(
        "/api/trip-workspaces/workspace-api/edits",
        headers={"If-Match": '"1"', "Idempotency-Key": "browser-a-command-1"},
        json={
            "command_id": "api-command-1",
            "base_revision": 1,
            "operation": "LOCK_STOP",
            "payload": {"stop_id": "stop-api-1"},
        },
    )
    assert response.status_code == 200
    assert response.json()["new_revision"] == 2
    assert response.json()["audit_mode"] == "INCREMENTAL_REVISION_ONLY"
    assert response.json()["llm_calls"] == 0
    assert response.json()["route_delta"]["status"] == "AVAILABLE"

    # A second browser reads the same server revision; no local cache participates.
    readback = client.get("/api/trip-workspaces/workspace-api/revisions/2")
    assert readback.status_code == 200
    assert readback.json()["days"][0]["stops"][0]["locked"] is True
    workspace = client.get("/api/trip-workspaces/workspace-api").json()
    assert workspace["current_itinerary_revision"] == 2
    assert workspace["current_report_id"] is None


def test_revision_api_idempotent_replay(monkeypatch):
    client, repository, _ = _client(monkeypatch)
    request = {
        "command_id": "api-command-idempotent",
        "base_revision": 1,
        "operation": "REMOVE_STOP",
        "payload": {"stop_id": "stop-api-1"},
    }
    headers = {"If-Match": "1", "Idempotency-Key": "same-browser-request"}
    assert client.post("/api/trip-workspaces/workspace-api/edits", headers=headers, json=request).status_code == 200
    replay = client.post("/api/trip-workspaces/workspace-api/edits", headers=headers, json=request)
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert len(asyncio.run(repository.list_revisions("workspace-api"))) == 2


def test_public_undo_keeps_generic_non_suggestion_revision_path(monkeypatch):
    client, repository, _ = _client(monkeypatch)
    edited = client.post(
        "/api/trip-workspaces/workspace-api/edits",
        headers={"If-Match": '"1"', "Idempotency-Key": "generic-before-undo"},
        json={
            "command_id": "generic-before-undo",
            "base_revision": 1,
            "operation": "LOCK_STOP",
            "payload": {"stop_id": "stop-api-1"},
        },
    )
    assert edited.status_code == 200
    undone = client.post(
        "/api/trip-workspaces/workspace-api/undo",
        headers={"If-Match": '"2"', "Idempotency-Key": "generic-undo"},
        json={"command_id": "generic-undo", "base_revision": 2, "target_revision": 1},
    )
    assert undone.status_code == 200
    assert undone.json()["new_revision"] == 3
    restored = asyncio.run(repository.get_revision("workspace-api", 3))
    assert restored.days[0].stops[0].locked is False


def test_revision_api_returns_stable_conflict_code(monkeypatch):
    client, _, _ = _client(monkeypatch)
    first = client.post(
        "/api/trip-workspaces/workspace-api/edits",
        headers={"If-Match": "1", "Idempotency-Key": "first"},
        json={
            "command_id": "first-command",
            "base_revision": 1,
            "operation": "LOCK_STOP",
            "payload": {"stop_id": "stop-api-1"},
        },
    )
    assert first.status_code == 200
    conflict = client.post(
        "/api/trip-workspaces/workspace-api/edits",
        headers={"If-Match": "1", "Idempotency-Key": "stale"},
        json={
            "command_id": "stale-command",
            "base_revision": 1,
            "operation": "LOCK_STOP",
            "payload": {"stop_id": "stop-api-1"},
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "ITINERARY_REVISION_CONFLICT"
    assert conflict.json()["detail"]["actual_revision"] == 2


def test_confirm_requires_a_current_full_audit_then_creates_revision_and_etag(monkeypatch):
    client, repository, audits = _client(monkeypatch)
    rejected = client.post(
        "/api/trip-workspaces/workspace-api/confirm",
        headers={"If-Match": "1", "Idempotency-Key": "confirm-without-audit"},
        json={"command_id": "confirm-without-audit", "base_revision": 1},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "CURRENT_AUDIT_REQUIRED"
    asyncio.run(AuditApplicationService(
        itinerary_repository=repository,
        audit_repository=audits,
    ).run_current_audit("workspace-api"))
    response = client.post(
        "/api/trip-workspaces/workspace-api/confirm",
        headers={"If-Match": "1", "Idempotency-Key": "confirm-api"},
        json={"command_id": "confirm-command", "base_revision": 1},
    )
    assert response.status_code == 200
    assert response.headers["etag"] == '"2"'
    assert client.get("/api/trip-workspaces/workspace-api").json()["status"] == "CONFIRMED"


def test_changed_route_edge_refresh_api_uses_only_new_edges_and_replays_immutable_receipt(monkeypatch):
    client, _, _ = _client(monkeypatch)

    class Provider:
        def __init__(self):
            self.calls = 0

        async def fetch(self, *, origin, destination, mode, city):
            self.calls += 1
            return RouteResult(
                status="ok", duration_minutes=31, distance_km=6.2, transfer_count=None,
                source="api-controlled-route", response_hash="b" * 64,
            )

    provider = Provider()
    commands = InMemoryCreationCommandRepository()
    client.app.dependency_overrides[workspace_api.get_creation_command_repository] = lambda: commands
    client.app.dependency_overrides[workspace_api.get_route_evidence_provider] = lambda: provider
    edit = client.post(
        "/api/trip-workspaces/workspace-api/edits",
        headers={"If-Match": "1", "Idempotency-Key": "api-reorder"},
        json={
            "command_id": "api-reorder", "base_revision": 1, "operation": "REORDER_STOP",
            "payload": {"stop_id": "stop-api-2", "target_day_index": 0, "target_order_index": 3},
        },
    )
    assert edit.status_code == 200
    assert edit.json()["route_delta"]["async_route_refresh_required"] is True

    path = "/api/trip-workspaces/workspace-api/revisions/2/changed-route-edges/refresh"
    first = client.post(path, headers={"Idempotency-Key": "api-refresh-new-edges"})
    replay = client.post(path, headers={"Idempotency-Key": "api-refresh-new-edges"})

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json()["idempotent_replay"] is True
    assert provider.calls == 2
    assert first.json()["route_delta"]["scope"] == "CURRENT_REVISION_CHANGED_EDGES_ONLY"
    assert first.json()["route_delta"]["status"] == "AVAILABLE"
    assert replay.json()["evidence_snapshot"]["snapshot_id"] == first.json()["evidence_snapshot"]["snapshot_id"]
