from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trip_workspaces as workspace_api
from app.audit.repositories import InMemoryAuditRepository
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.utils.auth import get_current_user


def _client(monkeypatch) -> tuple[TestClient, InMemoryItineraryRepository]:
    date_range = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
    revision = with_content_hash(
        ItineraryRevisionContent(
            itinerary_id="mobile-itinerary",
            workspace_id="mobile-workspace",
            revision=1,
            source_type=RevisionSource.MANUAL,
            city="杭州",
            date_range=date_range,
            days=[
                ItineraryDay(
                    day_index=0,
                    date=date_range.start,
                    stops=[
                        ItineraryStop(
                            stop_id="mobile-stop",
                            place_id="mobile-place",
                            day_index=0,
                            order_index=0,
                        )
                    ],
                ),
                ItineraryDay(day_index=1, date=date_range.end, stops=[]),
            ],
            created_by="mobile-user",
        )
    )
    workspace = TripWorkspace(
        workspace_id="mobile-workspace",
        room_id="mobile-room",
        city="杭州",
        trip_date_range=date_range,
        current_itinerary_revision=1,
        created_by="mobile-user",
    )
    repository = InMemoryItineraryRepository()
    asyncio.run(repository.create_workspace(workspace, revision))

    app = FastAPI()
    app.include_router(workspace_api.router, prefix="/api")
    app.dependency_overrides[workspace_api.get_itinerary_repository] = lambda: repository
    app.dependency_overrides[workspace_api.get_audit_repository] = lambda: InMemoryAuditRepository(repository.workspaces)
    app.dependency_overrides[get_current_user] = lambda: "mobile-user"
    monkeypatch.setattr(workspace_api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app), repository


def test_workspace_snapshot_is_one_round_trip_resume_contract(monkeypatch):
    client, _ = _client(monkeypatch)

    response = client.get("/api/trip-workspaces/mobile-workspace/snapshot")

    assert response.status_code == 200
    assert response.headers["etag"] == '"1"'
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["workspace"]["current_itinerary_revision"] == 1
    assert payload["current_revision"]["revision"] == 1
    assert payload["current_revision"]["content_hash"]


def test_successful_mutation_returns_etag_for_next_mobile_write(monkeypatch):
    client, _ = _client(monkeypatch)

    response = client.post(
        "/api/trip-workspaces/mobile-workspace/edits",
        headers={"If-Match": 'W/"1"', "Idempotency-Key": "mobile-request-1"},
        json={
            "command_id": "mobile-command-1",
            "base_revision": 1,
            "operation": "LOCK_STOP",
            "payload": {"stop_id": "mobile-stop"},
        },
    )

    assert response.status_code == 200
    assert response.headers["etag"] == '"2"'
    assert response.json()["new_revision"] == 2


def test_missing_conditional_headers_have_machine_readable_codes(monkeypatch):
    client, _ = _client(monkeypatch)
    body = {
        "command_id": "mobile-command-missing-header",
        "base_revision": 1,
        "operation": "LOCK_STOP",
        "payload": {"stop_id": "mobile-stop"},
    }

    missing_match = client.post(
        "/api/trip-workspaces/mobile-workspace/edits",
        headers={"Idempotency-Key": "mobile-request-2"},
        json=body,
    )
    assert missing_match.status_code == 428
    assert missing_match.json()["detail"]["code"] == "IF_MATCH_REQUIRED"

    missing_idempotency = client.post(
        "/api/trip-workspaces/mobile-workspace/edits",
        headers={"If-Match": "1"},
        json=body,
    )
    assert missing_idempotency.status_code == 400
    assert missing_idempotency.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_edit_rejects_if_match_body_mismatch_with_stable_code(monkeypatch):
    client, _ = _client(monkeypatch)

    response = client.post(
        "/api/trip-workspaces/mobile-workspace/edits",
        headers={"If-Match": "2", "Idempotency-Key": "mobile-mismatch"},
        json={
            "command_id": "mobile-command-mismatch",
            "base_revision": 1,
            "operation": "LOCK_STOP",
            "payload": {"stop_id": "mobile-stop"},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IF_MATCH_BODY_MISMATCH"
