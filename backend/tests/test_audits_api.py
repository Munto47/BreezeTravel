from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import audits as audits_api
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
from app.members.repositories import InMemoryMemberConstraintRepository
from app.members.models import (
    ConstraintConfirmationStatus,
    ConstraintHardness,
    ConstraintSource,
    MemberConstraint,
)
from app.itineraries.tips_repositories import InMemoryFinalTipsRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.utils.auth import get_current_user


def _client(monkeypatch):
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    revision = with_content_hash(
        ItineraryRevisionContent(
            itinerary_id="itin-audit-api",
            workspace_id="workspace-audit-api",
            revision=1,
            source_type=RevisionSource.MANUAL,
            city="北京",
            date_range=date_range,
            days=[
                ItineraryDay(
                    day_index=0,
                    date=date_range.start,
                    stops=[
                        ItineraryStop(
                            stop_id="audit-stop",
                            place_id="audit-place",
                            day_index=0,
                            order_index=0,
                            start_time="09:00",
                            end_time="11:00",
                        ),
                    ],
                ),
                ItineraryDay(day_index=1, date=date_range.end, stops=[]),
            ],
            created_by="audit-user",
        )
    )
    workspace = TripWorkspace(
        workspace_id="workspace-audit-api",
        room_id="room-audit-api",
        city="北京",
        trip_date_range=date_range,
        current_itinerary_revision=1,
        created_by="audit-user",
    )
    itinerary_repository = InMemoryItineraryRepository()
    asyncio.run(itinerary_repository.create_workspace(workspace, revision))
    audit_repository = InMemoryAuditRepository(itinerary_repository.workspaces)
    member_repository = InMemoryMemberConstraintRepository(itinerary_repository.workspaces)
    audit_repository.current_revisions[workspace.workspace_id] = 1
    audit_repository.place_records[workspace.workspace_id] = {
        "audit-place": {
            "place_id": "audit-place",
            "name": "测试景点",
            "city": "北京",
            "category": "attraction",
            "opening_hours": "08:00-18:00",
        },
    }
    tips_repository = InMemoryFinalTipsRepository()
    command_repository = InMemoryCreationCommandRepository()

    app = FastAPI()
    app.include_router(audits_api.router, prefix="/api")
    app.dependency_overrides[audits_api.get_itinerary_repository] = lambda: itinerary_repository
    app.dependency_overrides[audits_api.get_audit_repository] = lambda: audit_repository
    app.dependency_overrides[audits_api.get_member_constraint_repository] = lambda: member_repository
    app.dependency_overrides[audits_api.get_tips_repository] = lambda: tips_repository
    app.dependency_overrides[audits_api.get_creation_command_repository] = lambda: command_repository
    app.dependency_overrides[get_current_user] = lambda: "audit-user"
    monkeypatch.setattr(audits_api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app), audit_repository, member_repository


def test_audit_api_runs_without_planner_and_evidence_is_readable(monkeypatch):
    client, _, _ = _client(monkeypatch)
    response = client.post(
        "/api/trip-workspaces/workspace-audit-api/audits",
        json={},
        headers={"Idempotency-Key": "audit-create"},
    )
    assert response.status_code == 200
    report = response.json()
    assert report["itinerary_revision"] == 1
    assert report["findings"]

    readback = client.get(f"/api/audits/{report['report_id']}")
    assert readback.status_code == 200
    assert readback.json()["report_input_hash"] == report["report_input_hash"]
    evidence = client.get(f"/api/audits/{report['report_id']}/evidence")
    assert evidence.status_code == 200
    assert evidence.json()["facts"]
    fact_ids = {item["fact_id"] for item in evidence.json()["facts"]}
    for finding in report["findings"]:
        assert set(finding["evidence_fact_ids"]).issubset(fact_ids)


def test_audit_api_injects_effective_confirmed_hard_member_constraint(monkeypatch):
    client, _, members = _client(monkeypatch)
    members.constraints.append(MemberConstraint(
        constraint_id="return-before-8",
        workspace_id="workspace-audit-api",
        owner_member_id="audit-user",
        type="latest_return_time",
        operator="eq",
        value="20:00",
        hardness=ConstraintHardness.HARD,
        source=ConstraintSource.MEMBER_EXPLICIT,
        confirmation_status=ConstraintConfirmationStatus.CONFIRMED,
        revision=1,
    ))
    members.workspaces["workspace-audit-api"] = members.workspaces["workspace-audit-api"].model_copy(
        update={"current_member_constraint_revision": 1},
    )

    report = client.post(
        "/api/trip-workspaces/workspace-audit-api/audits",
        json={},
        headers={"Idempotency-Key": "audit-member-constraint"},
    )

    assert report.status_code == 200
    payload = report.json()
    assert payload["member_constraint_revision_set"] == {"return-before-8": 1}
    finding = next(item for item in payload["findings"] if item["reason_code"] == "LATEST_RETURN_ROUTE_TO_HOTEL_UNKNOWN")
    assert finding["affected_member_ids"] == ["audit-user"]


def test_audit_create_requires_key_replays_and_rejects_key_reuse(monkeypatch):
    client, repository, _ = _client(monkeypatch)
    path = "/api/trip-workspaces/workspace-audit-api/audits"
    missing = client.post(path, json={})
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    headers = {"Idempotency-Key": "audit-reliable-retry"}
    first = client.post(path, json={}, headers=headers)
    replay = client.post(path, json={}, headers=headers)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert len(repository.snapshots) == len(repository.reports) == 1

    reused = client.post(path, json={"task_id": "different-task"}, headers=headers)
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_audit_refresh_appends_snapshot_and_report(monkeypatch):
    client, repository, _ = _client(monkeypatch)
    first = client.post(
        "/api/trip-workspaces/workspace-audit-api/audits",
        json={},
        headers={"Idempotency-Key": "audit-create"},
    ).json()
    second_response = client.post(
        f"/api/audits/{first['report_id']}/refresh",
        headers={"Idempotency-Key": "audit-refresh"},
    )
    assert second_response.status_code == 200
    second = second_response.json()
    assert second["report_id"] != first["report_id"]
    assert second["supersedes_report_id"] == first["report_id"]
    assert len(repository.snapshots) == 2
    assert len(repository.reports) == 2


def test_pre_trip_recheck_appends_bundle_explains_evidence_change_and_replays(monkeypatch):
    client, repository, _ = _client(monkeypatch)
    first = client.post(
        "/api/trip-workspaces/workspace-audit-api/audits",
        json={},
        headers={"Idempotency-Key": "audit-create"},
    ).json()
    # The durable record is deliberately updated after the first audit.  The
    # local P8 command must show this as a fact change rather than masking it.
    repository.place_records["workspace-audit-api"]["audit-place"]["opening_hours"] = "12:00-18:00"
    headers = {"Idempotency-Key": "pre-trip-recheck"}
    path = f"/api/audits/{first['report_id']}/pre-trip-recheck"
    response = client.post(path, headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["source_report_id"] == first["report_id"]
    assert payload["report"]["report_id"] != first["report_id"]
    assert payload["evidence_snapshot"]["supersedes_snapshot_id"] == first["evidence_snapshot_id"]
    assert any(
        item["change_type"] == "VALUE_CHANGED" and item["fact_type"] == "OPENING_HOURS"
        for item in payload["evidence_changes"]
    )
    assert payload["degraded"] is False
    assert payload["provider_failures"] == []
    assert payload["recheck_window_state"] in {"EARLY", "RECOMMENDED_24_48H", "LATE"}
    assert payload["trip_start_reference_at"].endswith("+08:00")
    assert isinstance(payload["hours_until_trip_start"], float)
    assert "首日零点" in payload["recheck_window_reason"]
    replay = client.post(path, headers=headers)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == payload
    assert len(repository.snapshots) == len(repository.reports) == 2

    readback = client.get(f"/api/audits/{payload['report']['report_id']}/pre-trip-recheck-result")
    assert readback.status_code == 200
    assert readback.json() == payload

    # Ordinary report supersession is not itself evidence of a P8 recheck.
    ordinary = client.post(
        f"/api/audits/{payload['report']['report_id']}/refresh",
        headers={"Idempotency-Key": "ordinary-refresh"},
    )
    not_recheck = client.get(f"/api/audits/{ordinary.json()['report_id']}/pre-trip-recheck-result")
    assert not_recheck.status_code == 404


def test_audit_events_expose_completed_phases(monkeypatch):
    client, _, _ = _client(monkeypatch)
    report = client.post(
        "/api/trip-workspaces/workspace-audit-api/audits",
        json={},
        headers={"Idempotency-Key": "audit-create"},
    ).json()
    events = client.get(f"/api/audits/{report['report_id']}/events")
    assert events.status_code == 200
    assert "evidence_ready" in events.text
    assert "rules_complete" in events.text
    assert '"event": "done"' in events.text


def test_tips_api_fails_closed_while_current_report_has_high_findings(monkeypatch):
    client, _, _ = _client(monkeypatch)
    report = client.post(
        "/api/trip-workspaces/workspace-audit-api/audits",
        json={},
        headers={"Idempotency-Key": "audit-create"},
    ).json()
    missing = client.get(f"/api/audits/{report['report_id']}/tips")
    assert missing.status_code == 404

    generated = client.post(
        f"/api/audits/{report['report_id']}/tips",
        json={},
        headers={"Idempotency-Key": "tips-generate"},
    )
    assert generated.status_code == 409
    assert generated.json()["detail"]["code"] == "TIPS_NOT_ELIGIBLE"
