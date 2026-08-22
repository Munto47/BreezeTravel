from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.audit.evidence_service import EvidenceObservation, EvidenceService
from app.audit.engine import AuditEngine
from app.audit.models import AuditRunInput, AuditStatus
from app.audit.recheck import PreTripRecheckService
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
from app.members.models import (
    ConstraintConfirmationStatus,
    ConstraintHardness,
    ConstraintSource,
    MemberConstraint,
)
from app.members.repositories import InMemoryMemberConstraintRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.schemas.task_spec import DateRange, HardConstraint, TripTaskSpec


NOW = datetime(2026, 8, 20, 9, tzinfo=timezone.utc)


def _constraint(constraint_id: str, kind: str, value: object) -> MemberConstraint:
    return MemberConstraint(
        constraint_id=constraint_id,
        workspace_id="member-audit-workspace",
        owner_member_id="member-senior",
        type=kind,
        operator="eq",
        value=value,
        hardness=ConstraintHardness.HARD,
        source=ConstraintSource.MEMBER_EXPLICIT,
        confirmation_status=ConstraintConfirmationStatus.CONFIRMED,
        revision=1,
    )


def _revision():
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    return with_content_hash(ItineraryRevisionContent(
        itinerary_id="member-audit-itinerary",
        workspace_id="member-audit-workspace",
        revision=1,
        source_type=RevisionSource.MANUAL,
        city="北京",
        date_range=date_range,
        days=[
            ItineraryDay(day_index=0, date=date_range.start, stops=[
                ItineraryStop(
                    stop_id="walk-stop", place_id="walk-place", day_index=0, order_index=0,
                    start_time="11:00", end_time="12:00", category="attraction",
                    transport_to_next=RevisionTransport(mode="walking", duration_minutes=90),
                ),
                ItineraryStop(
                    stop_id="food-stop", place_id="food-place", day_index=0, order_index=1,
                    start_time="12:00", end_time="15:00", category="food",
                    transport_to_next=RevisionTransport(mode="driving", duration_minutes=20),
                ),
                ItineraryStop(
                    stop_id="hotel-stop", place_id="hotel-place", day_index=0, order_index=2,
                    start_time="20:30", end_time="21:00", category="hotel",
                ),
            ]),
            ItineraryDay(day_index=1, date=date_range.end, stops=[]),
        ],
        created_by="member-senior",
        created_at=NOW,
    ))


def _task() -> TripTaskSpec:
    # A positive legacy-vote finding cannot waive the member's hard constraint.
    return TripTaskSpec(
        task_id="member-audit-task",
        room_id="member-audit-room",
        task_revision=1,
        city="北京",
        date_range=DateRange(start=date(2026, 9, 1), days=2),
        hard_constraints=[
            HardConstraint(
                id="votes",
                type="preserve_majority_voted",
                value=True,
            ),
        ],
    )


def _snapshot(revision):
    return EvidenceService().create_snapshot(
        workspace_id=revision.workspace_id,
        itinerary_revision=revision.revision,
        observations=[
            EvidenceObservation(
                subject_type="PLACE",
                subject_id="food-place",
                fact_type="DIETARY_SUPPORT",
                value={"supported_restrictions": ["低盐"]},
                provider="restaurant_official",
                observed_at=NOW,
            ),
        ],
        now=NOW,
    )


def test_confirmed_hard_member_rules_are_three_state_and_votes_do_not_override():
    revision = _revision()
    constraints = [
        _constraint("walk", "walking_limit_minutes", 60),
        _constraint("nap", "requires_nap", True),
        _constraint("diet", "dietary_restrictions", ["低盐"]),
        _constraint("return", "latest_return_time", "20:00"),
    ]
    report = AuditEngine().run(
        run_input=AuditRunInput(
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            task_id="member-audit-task",
            task_revision=1,
            member_constraint_revision_set={item.constraint_id: item.revision for item in constraints},
            member_constraints=constraints,
            place_resolution_versions={stop.place_id: 1 for day in revision.days for stop in day.stops},
        ),
        revision=revision,
        task_spec=_task(),
        evidence_snapshot=_snapshot(revision),
        now=NOW,
    )
    by_reason = {
        item.reason_code: item
        for item in report.findings
        if item.rule_id == "member.confirmed_hard_constraints" and item.affected_days == [0]
    }

    assert by_reason["WALKING_LIMIT_EXCEEDED"].status == AuditStatus.VIOLATED
    assert by_reason["NAP_WINDOW_MISSING"].status == AuditStatus.VIOLATED
    assert by_reason["DIETARY_RESTRICTIONS_SUPPORTED"].status == AuditStatus.SATISFIED
    assert by_reason["LATEST_RETURN_EXCEEDED"].status == AuditStatus.VIOLATED
    assert all(item.affected_member_ids == ["member-senior"] for item in by_reason.values())
    assert report.overall_status == AuditStatus.VIOLATED


def test_member_dietary_and_return_are_unknown_without_verifiable_evidence():
    revision = _revision()
    constraints = [
        _constraint("diet", "dietary_restrictions", ["清真"]),
        _constraint("return", "latest_return_time", "22:00"),
    ]
    report = AuditEngine().run(
        run_input=AuditRunInput(
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            task_id="member-audit-task",
            task_revision=1,
            member_constraint_revision_set={item.constraint_id: item.revision for item in constraints},
            member_constraints=constraints,
        ),
        revision=revision,
        task_spec=_task(),
        evidence_snapshot=_snapshot(revision),
        now=NOW,
    )
    day_zero = {item.reason_code: item for item in report.findings if item.affected_days == [0]}

    assert day_zero["DIETARY_RESTRICTIONS_INCOMPLETE"].status == AuditStatus.UNKNOWN
    assert day_zero["LATEST_RETURN_MET"].status == AuditStatus.SATISFIED
    # Empty itinerary days never fabricate a return-home success.
    assert any(item.reason_code == "LATEST_RETURN_ITINERARY_EMPTY" and item.status == AuditStatus.UNKNOWN for item in report.findings)


def test_hotel_arrival_can_prove_latest_return_without_a_checkout_time():
    original = _revision()
    content = ItineraryRevisionContent.model_validate(original.model_dump(exclude={"content_hash"}))
    final_stop = content.days[0].stops[-1].model_copy(update={"end_time": None})
    revision = with_content_hash(content.model_copy(update={
        "days": [content.days[0].model_copy(update={"stops": [*content.days[0].stops[:-1], final_stop]}), content.days[1]],
    }))
    constraint = _constraint("return", "latest_return_time", "21:00")
    report = AuditEngine().run(
        run_input=AuditRunInput(
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            task_id="member-audit-task",
            task_revision=1,
            member_constraint_revision_set={"return": 1},
            member_constraints=[constraint],
        ),
        revision=revision,
        task_spec=_task(),
        evidence_snapshot=_snapshot(revision),
        now=NOW,
    )

    assert any(item.reason_code == "LATEST_RETURN_MET" for item in report.findings)


def test_stale_dietary_support_never_becomes_a_pass():
    revision = _revision()
    constraint = _constraint("diet", "dietary_restrictions", ["低盐"])
    stale_snapshot = EvidenceService().create_snapshot(
        workspace_id=revision.workspace_id,
        itinerary_revision=revision.revision,
        observations=[EvidenceObservation(
            subject_type="PLACE",
            subject_id="food-place",
            fact_type="DIETARY_SUPPORT",
            value={"supported_restrictions": ["低盐"]},
            provider="restaurant_official",
            observed_at=datetime(2026, 8, 18, 9, tzinfo=timezone.utc),
        )],
        now=NOW,
    )
    report = AuditEngine().run(
        run_input=AuditRunInput(
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            task_id="member-audit-task",
            task_revision=1,
            member_constraint_revision_set={"diet": 1},
            member_constraints=[constraint],
        ),
        revision=revision,
        task_spec=_task(),
        evidence_snapshot=stale_snapshot,
        now=NOW,
    )

    finding = next(item for item in report.findings if item.reason_code == "DIETARY_EVIDENCE_MISSING")
    assert finding.status == AuditStatus.UNKNOWN


@pytest.mark.asyncio
async def test_application_audit_and_pretrip_recheck_bind_effective_member_constraints():
    revision = _revision()
    workspace = TripWorkspace(
        workspace_id=revision.workspace_id,
        room_id="member-audit-room",
        city="北京",
        trip_date_range=revision.date_range,
        current_itinerary_revision=1,
        created_by="member-senior",
    )
    itineraries = InMemoryItineraryRepository()
    await itineraries.create_workspace(workspace, revision)
    audits = InMemoryAuditRepository(itineraries.workspaces)
    members = InMemoryMemberConstraintRepository(itineraries.workspaces)
    members.constraints.append(_constraint("walk", "walking_limit_minutes", 60))
    itineraries.workspaces[workspace.workspace_id] = itineraries.workspaces[workspace.workspace_id].model_copy(
        update={"current_member_constraint_revision": 1},
    )
    audits.place_records[workspace.workspace_id] = {
        "walk-place": {"place_id": "walk-place", "name": "景点", "opening_hours": "08:00-18:00"},
        "food-place": {"place_id": "food-place", "name": "餐厅", "opening_hours": "08:00-18:00"},
        "hotel-place": {"place_id": "hotel-place", "name": "酒店", "opening_hours": "00:00-23:59"},
    }
    service = AuditApplicationService(
        itinerary_repository=itineraries,
        audit_repository=audits,
        member_constraint_repository=members,
    )
    baseline = await service.run_current_audit(workspace.workspace_id, now=NOW)
    assert baseline.member_constraint_revision_set == {"walk": 1}
    assert any(item.reason_code == "WALKING_LIMIT_EXCEEDED" for item in baseline.findings)

    recheck, replayed = await PreTripRecheckService(
        itinerary_repository=itineraries,
        audit_repository=audits,
        audit_service=service,
    ).run_idempotent(
        source_report_id=baseline.report_id,
        actor_user_id="member-senior",
        idempotency_key="member-constraint-recheck",
        command_repository=InMemoryCreationCommandRepository(),
    )
    assert replayed is False
    assert recheck.report.member_constraint_revision_set == {"walk": 1}
    assert any(item.reason_code == "WALKING_LIMIT_EXCEEDED" for item in recheck.report.findings)
