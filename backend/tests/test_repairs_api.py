from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import repairs as repairs_api
from app.audit.repositories import InMemoryAuditRepository
from app.audit.service import AuditApplicationService
from app.constraints.geo_routes import RouteResult
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
    WorkspaceStatus,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.repairs.repositories import InMemoryRepairRepository
from app.utils.auth import get_current_user


class ControlledRepairRouteProvider:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    async def fetch(self, *, origin, destination, mode, city):
        self.calls.append((origin, destination, mode, city))
        if self.fail:
            raise TimeoutError("controlled route provider failure")
        return RouteResult(
            status="ok",
            duration_minutes=15,
            distance_km=2.5,
            transfer_count=None,
            source="controlled_route_fixture",
            response_hash="a" * 64,
            observed_at=None,
        )


def _client(monkeypatch, *, route_provider=None):
    async def setup():
        date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
        revision = with_content_hash(
            ItineraryRevisionContent(
                itinerary_id="repair-api-itinerary",
                workspace_id="repair-api-workspace",
                revision=1,
                source_type=RevisionSource.IMPORT,
                city="北京",
                date_range=date_range,
                days=[
                    ItineraryDay(
                        day_index=0,
                        date=date_range.start,
                        stops=[
                            ItineraryStop(
                                stop_id="s1",
                                place_id="p1",
                                day_index=0,
                                order_index=0,
                                start_time="09:00",
                                end_time="12:00",
                                visit_duration_minutes=180,
                                raw_name="故宫",
                            ),
                            ItineraryStop(
                                stop_id="s2",
                                place_id="p2",
                                day_index=0,
                                order_index=1,
                                start_time="11:00",
                                end_time="13:00",
                                visit_duration_minutes=120,
                                raw_name="景山",
                            ),
                        ],
                    ),
                    ItineraryDay(day_index=1, date=date_range.end, stops=[]),
                ],
                change_summary={
                    "map_stop_projections": {
                        "s1": {
                            "place_id": "p1",
                            "coords": {"lng": 116.397, "lat": 39.918},
                            "coordinate_role": "CONTROLLED_CANONICAL_POI",
                            "provenance": "CONTROLLED_TEST_FIXTURE",
                        },
                        "s2": {
                            "place_id": "p2",
                            "coords": {"lng": 116.396, "lat": 39.925},
                            "coordinate_role": "CONTROLLED_CANONICAL_POI",
                            "provenance": "CONTROLLED_TEST_FIXTURE",
                        },
                    }
                },
                created_by="repair-api-user",
            )
        )
        workspace = TripWorkspace(
            workspace_id=revision.workspace_id,
            room_id="repair-api-room",
            city="北京",
            trip_date_range=date_range,
            current_itinerary_revision=1,
            created_by="repair-api-user",
        )
        itinerary_repository = InMemoryItineraryRepository()
        await itinerary_repository.create_workspace(workspace, revision)
        audit_repository = InMemoryAuditRepository()
        audit_repository.current_revisions[workspace.workspace_id] = 1
        audit_repository.place_records[workspace.workspace_id] = {
            place_id: {
                "place_id": place_id,
                "name": name,
                "city": "北京",
                "category": "attraction",
                "opening_hours": "08:00-20:00",
            }
            for place_id, name in (("p1", "故宫"), ("p2", "景山"))
        }
        report = await AuditApplicationService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
        ).run_current_audit(workspace.workspace_id, now=datetime(2026, 8, 20, tzinfo=timezone.utc))
        itinerary_repository.workspaces[workspace.workspace_id] = workspace.model_copy(
            update={
                "current_report_id": report.report_id,
                "status": WorkspaceStatus.NEEDS_CONFIRMATION,
            }
        )
        repair_repository = InMemoryRepairRepository(itinerary_repository, audit_repository)
        return itinerary_repository, audit_repository, repair_repository, report

    itinerary_repository, audit_repository, repair_repository, report = asyncio.run(setup())
    command_repository = InMemoryCreationCommandRepository()
    app = FastAPI()
    app.include_router(repairs_api.router, prefix="/api")
    app.dependency_overrides[repairs_api.get_itinerary_repository] = lambda: itinerary_repository
    app.dependency_overrides[repairs_api.get_audit_repository] = lambda: audit_repository
    app.dependency_overrides[repairs_api.get_repair_repository] = lambda: repair_repository
    app.dependency_overrides[repairs_api.get_creation_command_repository] = lambda: command_repository
    app.dependency_overrides[repairs_api.get_route_evidence_provider] = lambda: (
        route_provider or ControlledRepairRouteProvider()
    )
    app.dependency_overrides[get_current_user] = lambda: "repair-api-user"
    monkeypatch.setattr(repairs_api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app), itinerary_repository, repair_repository, report


def test_repair_api_preview_apply_and_idempotent_replay(monkeypatch):
    client, itinerary_repository, repair_repository, report = _client(monkeypatch)

    proposed = client.post(
        f"/api/audits/{report.report_id}/repairs",
        headers={"Idempotency-Key": "repair-propose"},
    )
    assert proposed.status_code == 201
    options = proposed.json()
    assert len(options) == 2
    assert all(item["postcheck_report_id"] for item in options)
    assert all(item["route_cost_delta"] is None for item in options)

    selected = options[0]
    readback = client.get(f"/api/audits/{report.report_id}/repairs/{selected['repair_id']}")
    assert readback.status_code == 200
    assert readback.json()["route_cost_delta"] is None
    apply = client.post(
        f"/api/audits/{report.report_id}/repairs/{selected['repair_id']}/apply",
        json={"base_revision": 1},
        headers={"If-Match": '"1"', "Idempotency-Key": "repair-api-key"},
    )
    assert apply.status_code == 200
    assert apply.json()["new_revision"] == 2
    replay = client.post(
        f"/api/audits/{report.report_id}/repairs/{selected['repair_id']}/apply",
        json={"base_revision": 1},
        headers={"If-Match": '"1"', "Idempotency-Key": "repair-api-key"},
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    workspace = asyncio.run(itinerary_repository.get_workspace("repair-api-workspace"))
    assert workspace.current_itinerary_revision == 2
    sibling = next(item for item in repair_repository.options.values() if item.repair_id != selected["repair_id"])
    assert sibling.status.value == "STALE"


def test_repair_propose_requires_key_and_replays_same_option_set(monkeypatch):
    client, _, repair_repository, report = _client(monkeypatch)
    path = f"/api/audits/{report.report_id}/repairs"
    missing = client.post(path)
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    headers = {"Idempotency-Key": "repair-propose-retry"}
    first = client.post(path, headers=headers)
    replay = client.post(path, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert len(repair_repository.options) == len(first.json())


def test_repair_propose_fails_closed_when_route_provider_is_unavailable(monkeypatch):
    route_provider = ControlledRepairRouteProvider(fail=True)
    client, _, repair_repository, report = _client(
        monkeypatch,
        route_provider=route_provider,
    )

    response = client.post(
        f"/api/audits/{report.report_id}/repairs",
        headers={"Idempotency-Key": "repair-provider-unavailable"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "REPAIR_NO_FEASIBLE_OPTION"
    assert detail["message"] == (
        "no candidate passed locked-item, high-risk and UNKNOWN postcheck gates"
    )
    assert detail["source_report_id"] == report.report_id
    assert detail["unresolved_finding_ids"]
    assert len(route_provider.calls) == 2
    assert repair_repository.options == {}


def test_repair_api_reject_records_reason_and_checks_report_scope(monkeypatch):
    client, _, _, report = _client(monkeypatch)
    option = client.post(
        f"/api/audits/{report.report_id}/repairs",
        headers={"Idempotency-Key": "repair-propose"},
    ).json()[0]

    wrong_scope = client.get(f"/api/audits/not-the-source/repairs/{option['repair_id']}")
    assert wrong_scope.status_code == 404
    rejected = client.post(
        f"/api/audits/{report.report_id}/repairs/{option['repair_id']}/reject",
        json={"reason": "时间偏移太大"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["decision_reason"] == "时间偏移太大"
    replay = client.post(
        f"/api/audits/{report.report_id}/repairs/{option['repair_id']}/reject",
        json={"reason": "  时间偏移太大  "},
    )
    assert replay.status_code == 200
    assert replay.json() == rejected.json()
    conflicting_decision = client.post(
        f"/api/audits/{report.report_id}/repairs/{option['repair_id']}/reject",
        json={"reason": "改成另一个原因"},
    )
    assert conflicting_decision.status_code == 409
    assert conflicting_decision.json()["detail"]["code"] == "AUDIT_INPUT_STALE"


def test_repair_api_rejects_blank_reason_with_stable_machine_code(monkeypatch):
    client, _, repair_repository, report = _client(monkeypatch)
    option = client.post(
        f"/api/audits/{report.report_id}/repairs",
        headers={"Idempotency-Key": "repair-propose"},
    ).json()[0]

    response = client.post(
        f"/api/audits/{report.report_id}/repairs/{option['repair_id']}/reject",
        json={"reason": "   "},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "INVALID_REPAIR_REJECTION_REASON",
        "message": "repair rejection reason is required",
    }
    assert repair_repository.options[option["repair_id"]].status.value == "PROPOSED"


def test_repair_api_requires_matching_if_match(monkeypatch):
    client, _, _, report = _client(monkeypatch)
    option = client.post(
        f"/api/audits/{report.report_id}/repairs",
        headers={"Idempotency-Key": "repair-propose"},
    ).json()[0]

    response = client.post(
        f"/api/audits/{report.report_id}/repairs/{option['repair_id']}/apply",
        json={"base_revision": 1},
        headers={"If-Match": '"2"', "Idempotency-Key": "mismatch-key"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IF_MATCH_BODY_MISMATCH"
