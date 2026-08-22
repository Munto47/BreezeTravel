from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import members as members_api
from app.audit.models import AuditFinding, AuditReport, AuditSeverity, AuditStatus
from app.audit.repositories import InMemoryAuditRepository
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import ItineraryDay, ItineraryRevisionContent, ItineraryStop, RevisionSource, TripDateRange, TripWorkspace
from app.itineraries.repositories import InMemoryItineraryRepository
from app.members.repositories import InMemoryMemberConstraintRepository
from app.members.sharing import InMemoryShareLinkRepository
from app.services.room_access import RoomAccess
from app.utils.auth import get_current_user


def _client(monkeypatch, *, actor: str = "owner"):
    itineraries = InMemoryItineraryRepository()
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="share-itinerary", workspace_id="share-workspace", revision=1,
        source_type=RevisionSource.IMPORT, city="北京", date_range=date_range,
        days=[
            ItineraryDay(day_index=0, date=date_range.start, stops=[ItineraryStop(stop_id="s1", place_id="p1", day_index=0, order_index=0, raw_name="故宫")]),
            ItineraryDay(day_index=1, date=date_range.end, stops=[]),
        ],
        created_by="owner",
    ))
    workspace = TripWorkspace(workspace_id="share-workspace", room_id="share-room", city="北京", trip_date_range=date_range, created_by="owner")
    asyncio.run(itineraries.create_workspace(workspace, revision))
    member_repository = InMemoryMemberConstraintRepository(itineraries.workspaces)
    share_repository = InMemoryShareLinkRepository()
    audit_repository = InMemoryAuditRepository(itineraries.workspaces)
    app = FastAPI()
    app.include_router(members_api.router, prefix="/api")
    current = {"user": actor}
    app.dependency_overrides[members_api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[members_api.get_member_constraint_repository] = lambda: member_repository
    app.dependency_overrides[members_api.get_share_link_repository] = lambda: share_repository
    app.dependency_overrides[members_api.get_audit_repository] = lambda: audit_repository
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.dependency_overrides[members_api.get_optional_user] = lambda: current["user"]

    async def room_access(room_id: str, user_id: str, **_):
        if room_id != "share-room" or user_id not in {"owner", "member-1", "outsider"}:
            raise AssertionError("unexpected room lookup")
        return RoomAccess(room_id=room_id, thread_id="thread", user_id=user_id, role="owner" if user_id == "owner" else "member")

    monkeypatch.setattr(members_api, "require_room_member", room_access)
    monkeypatch.setattr(members_api, "get_room_member_ids", lambda _room_id: _room_member_ids())
    return TestClient(app), current, member_repository, share_repository, audit_repository


async def _room_member_ids():
    return ["member-1", "owner"]


def _constraint(*, owner: str = "member-1", hardness: str = "HARD", source: str = "MEMBER_EXPLICIT"):
    return {
        "constraint_id": "walking-limit",
        "owner_member_id": owner,
        "type": "walking_limit_minutes",
        "operator": "LTE",
        "value": 90,
        "hardness": hardness,
        "priority": 80,
        "source": source,
        "confirmation_status": "CONFIRMED",
        "waivable_by": [owner],
    }


def test_member_cannot_write_another_members_hard_constraint(monkeypatch):
    client, _, _, _, _ = _client(monkeypatch, actor="owner")
    response = client.put(
        "/api/trip-workspaces/share-workspace/members/member-1/constraints",
        json={"expected_base_revision": 0, "constraint": _constraint()},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "RESOURCE_SCOPE_DENIED"


def test_member_explicit_hard_constraint_creates_new_constraint_revision(monkeypatch):
    client, current, _, _, _ = _client(monkeypatch, actor="member-1")
    response = client.put(
        "/api/trip-workspaces/share-workspace/members/member-1/constraints",
        json={"expected_base_revision": 0, "constraint": _constraint()},
    )
    assert response.status_code == 200
    assert response.json()["current_workspace_revision"] == 1
    # A claimed organizer/memory source cannot cross the member endpoint.
    response = client.put(
        "/api/trip-workspaces/share-workspace/members/member-1/constraints",
        json={"expected_base_revision": 1, "constraint": _constraint(source="ORGANIZER")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MEMBER_CONSTRAINT_SOURCE_REQUIRED"
    current["user"] = "owner"


def test_share_links_bind_scope_recipient_expiry_and_revocation(monkeypatch):
    client, current, member_repository, _, _ = _client(monkeypatch)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    created = client.post(
        "/api/trip-workspaces/share-workspace/share-links",
        json={"scopes": ["REPORT_READ", "CONSTRAINT_WRITE", "ACKNOWLEDGE"], "recipient_member_id": "member-1", "expires_at": expires_at},
    )
    assert created.status_code == 201
    issued = created.json()
    assert len(issued["token"]) >= 32
    assert "token" not in issued["link"]

    # A bearer link with input scope is additionally bound to its recipient's
    # authenticated identity.  Its raw value cannot become a generic editor.
    readable_as_owner = client.get(f"/api/share/{issued['token']}")
    assert readable_as_owner.status_code == 403
    assert readable_as_owner.json()["detail"]["code"] == "RESOURCE_SCOPE_DENIED"
    forwarded_write = client.post(f"/api/share/{issued['token']}/responses", json={"action": "ACKNOWLEDGE"})
    assert forwarded_write.status_code == 403
    assert forwarded_write.json()["detail"]["code"] == "RESOURCE_SCOPE_DENIED"
    current["user"] = "member-1"
    readable = client.get(f"/api/share/{issued['token']}")
    assert readable.status_code == 200
    body = readable.json()
    assert body["itinerary"]["revision"] == 1
    assert body["recipient_bound"] is True
    assert body["acknowledgement"] == {"required": True, "acknowledged": False, "acknowledged_at": None}
    assert "workspace_id" not in body
    assert "share_link_id" not in body

    constraint_response = client.post(
        f"/api/share/{issued['token']}/responses",
        json={"action": "CONSTRAINT", "expected_base_revision": 0, "constraint": _constraint()},
    )
    assert constraint_response.status_code == 201
    assert constraint_response.json()["member_constraint_revision"] == 1
    assert asyncio.run(member_repository.list_effective_constraints("share-workspace", 1))[0].hardness.value == "HARD"

    acknowledgement = client.post(f"/api/share/{issued['token']}/responses", json={"action": "ACKNOWLEDGE"})
    assert acknowledgement.status_code == 201
    current["user"] = "owner"
    members = client.get("/api/trip-workspaces/share-workspace/members")
    assert members.status_code == 200
    member_view = next(item for item in members.json() if item["member_id"] == "member-1")
    assert member_view["confirmed_itinerary_revision"] == 1
    owner_view = next(item for item in members.json() if item["member_id"] == "owner")
    assert owner_view["confirmed_itinerary_revision"] is None

    revoked = client.delete(f"/api/trip-workspaces/share-workspace/share-links/{issued['link']['share_link_id']}")
    assert revoked.status_code == 200
    inaccessible = client.get(f"/api/share/{issued['token']}")
    assert inaccessible.status_code == 404
    assert inaccessible.json()["detail"]["code"] == "SHARE_LINK_UNAVAILABLE"


def test_share_scope_and_unbound_input_are_rejected(monkeypatch):
    client, _, _, _, _ = _client(monkeypatch)
    readonly = client.post("/api/trip-workspaces/share-workspace/share-links", json={})
    assert readonly.status_code == 201
    scope_denied = client.post(f"/api/share/{readonly.json()['token']}/responses", json={"action": "ACKNOWLEDGE"})
    assert scope_denied.status_code == 403
    assert scope_denied.json()["detail"]["code"] == "RESOURCE_SCOPE_DENIED"
    invalid = client.post(
        "/api/trip-workspaces/share-workspace/share-links",
        json={"scopes": ["ACKNOWLEDGE"]},
    )
    assert invalid.status_code == 422

    input_without_read = client.post(
        "/api/trip-workspaces/share-workspace/share-links",
        json={"scopes": ["ACKNOWLEDGE"], "recipient_member_id": "member-1"},
    )
    assert input_without_read.status_code == 422

    unsupported = client.post(
        "/api/trip-workspaces/share-workspace/share-links",
        json={"scopes": ["WORKSPACE_EDIT"]},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["detail"]["code"] == "UNSUPPORTED_SHARE_SCOPE"


def test_expired_link_has_same_non_disclosing_result(monkeypatch):
    client, _, _, _, _ = _client(monkeypatch)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    # Creation correctly refuses an already expired link, avoiding a misleading issued capability.
    result = client.post("/api/trip-workspaces/share-workspace/share-links", json={"expires_at": expired})
    assert result.status_code == 422


def test_shared_read_projection_captures_only_the_locked_revision_and_report(monkeypatch):
    client, current, _, _, audit_repository = _client(monkeypatch)
    report = AuditReport(
        report_id="captured-report",
        workspace_id="share-workspace",
        itinerary_id="share-itinerary",
        itinerary_revision=1,
        task_id="captured-task",
        task_revision=1,
        evidence_snapshot_id="captured-snapshot",
        audit_rule_set_version="rules-v1",
        report_input_hash="d" * 64,
        overall_status=AuditStatus.VIOLATED,
        findings=[AuditFinding(
            finding_id="shared-finding",
            rule_id="route-time-window",
            rule_version="1",
            status=AuditStatus.VIOLATED,
            severity=AuditSeverity.HIGH,
            reason_code="TIME_OVERLAP",
            message="两个地点的时段重叠",
            input_values={"private": "must-not-leak"},
            affected_days=[0],
            affected_stop_ids=["s1"],
            affected_member_ids=["member-1"],
            evidence_fact_ids=["secret-evidence"],
            repairable=True,
            confirmation_action="调整时间",
        )],
    )
    asyncio.run(audit_repository.save_report(report))
    issued = client.post(
        "/api/trip-workspaces/share-workspace/share-links",
        json={"scopes": ["REPORT_READ", "ACKNOWLEDGE"], "recipient_member_id": "member-1"},
    ).json()
    current["user"] = "member-1"

    response = client.get(f"/api/share/{issued['token']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["itinerary"]["revision"] == 1
    assert payload["report"]["report_id"] == "captured-report"
    assert payload["report"]["findings"] == [{
        "finding_id": "shared-finding", "rule_id": "route-time-window",
        "status": "VIOLATED", "severity": "HIGH", "reason_code": "TIME_OVERLAP",
        "message": "两个地点的时段重叠", "affected_days": [0],
        "affected_stop_ids": ["s1"], "repairable": True, "confirmation_action": "调整时间",
    }]
    serialized = response.text
    assert "share-workspace" not in serialized
    assert "share-itinerary" not in serialized
    assert "must-not-leak" not in serialized
    assert "secret-evidence" not in serialized
    assert "member-1" not in serialized
