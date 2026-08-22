from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.audit.errors import AuditInputStaleError
from app.audit.repositories import InMemoryAuditRepository
from app.audit.service import AuditApplicationService
from app.itineraries.errors import RevisionConflictError
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
from app.members.models import (
    ConstraintConfirmationStatus,
    ConstraintHardness,
    ConstraintSource,
    MemberConstraintDraft,
    TravelerProfile,
)
from app.members.repositories import InMemoryMemberConstraintRepository
from app.members.service import MemberConstraintService


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def _draft(
    constraint_id: str = "walking-limit",
    *,
    value: int = 90,
    source: ConstraintSource = ConstraintSource.MEMBER_EXPLICIT,
    hardness: ConstraintHardness = ConstraintHardness.HARD,
    confirmation: ConstraintConfirmationStatus = ConstraintConfirmationStatus.CONFIRMED,
) -> MemberConstraintDraft:
    return MemberConstraintDraft(
        constraint_id=constraint_id,
        owner_member_id="member-1",
        type="walking_limit_minutes",
        operator="LTE",
        value=value,
        hardness=hardness,
        priority=80,
        source=source,
        confirmation_status=confirmation,
        waivable_by=["member-1"],
    )


async def _context():
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="member-itinerary",
        workspace_id="member-workspace",
        revision=1,
        source_type=RevisionSource.IMPORT,
        city="北京",
        date_range=date_range,
        days=[
            ItineraryDay(day_index=0, date=date_range.start, stops=[
                ItineraryStop(
                    stop_id="stop-1",
                    place_id="place-1",
                    day_index=0,
                    order_index=0,
                    raw_name="故宫",
                ),
            ]),
            ItineraryDay(day_index=1, date=date_range.end, stops=[]),
        ],
        created_by="member-1",
        created_at=NOW,
    ))
    workspace = TripWorkspace(
        workspace_id=revision.workspace_id,
        room_id="member-room",
        city="北京",
        trip_date_range=date_range,
        created_by="member-1",
    )
    itineraries = InMemoryItineraryRepository()
    await itineraries.create_workspace(workspace, revision)
    members = InMemoryMemberConstraintRepository(itineraries.workspaces)
    audits = InMemoryAuditRepository(itineraries.workspaces)
    audits.current_revisions[workspace.workspace_id] = 1
    audits.place_records[workspace.workspace_id] = {
        "place-1": {
            "place_id": "place-1",
            "name": "故宫",
            "city": "北京",
            "category": "attraction",
            "opening_hours": "08:30-17:00",
            "retrieval_observed_at": NOW,
        },
    }
    return itineraries, members, audits


def test_memory_and_inferred_cannot_be_hard_and_hard_requires_confirmation():
    for source in (ConstraintSource.MEMORY, ConstraintSource.INFERRED):
        with pytest.raises(ValidationError, match="must remain SOFT"):
            _draft(source=source)
    with pytest.raises(ValidationError, match="explicit member confirmation"):
        _draft(confirmation=ConstraintConfirmationStatus.PENDING)


@pytest.mark.asyncio
async def test_profile_contract_and_append_only_constraint_reconstruction():
    itineraries, members, _ = await _context()
    service = MemberConstraintService(members)
    profile = TravelerProfile(
        workspace_id="member-workspace",
        member_id="member-1",
        display_name="同行老人",
        age_group="senior",
        walking_limit_minutes=90,
        requires_nap=True,
        wheelchair_or_stroller=False,
        dietary_restrictions=["低盐"],
        medication_times=["12:30"],
        latest_return_time="20:00",
        confirmed_revision=1,
    )
    assert await service.save_profile(profile) == profile

    itineraries.workspaces["member-workspace"] = itineraries.workspaces["member-workspace"].model_copy(
        update={"current_report_id": "old-report"}
    )
    first = await service.write_constraint(
        "member-workspace", _draft(value=90), expected_base_revision=0
    )
    second = await service.write_constraint(
        "member-workspace", _draft(value=60), expected_base_revision=1
    )

    assert first.stale_report_id == "old-report"
    assert first.current_workspace_revision == 1
    assert second.current_workspace_revision == 2
    assert (await members.list_effective_constraints("member-workspace", 1))[0].value == 90
    effective_at_two = await members.list_effective_constraints("member-workspace", 2)
    assert len(effective_at_two) == 1
    assert effective_at_two[0].value == 60
    workspace = await itineraries.get_workspace("member-workspace")
    assert workspace.current_member_constraint_revision == 2
    assert workspace.current_report_id is None


@pytest.mark.asyncio
async def test_same_base_member_constraint_concurrency_has_one_winner():
    _, members, _ = await _context()
    service = MemberConstraintService(members)

    results = await asyncio.gather(
        service.write_constraint("member-workspace", _draft("constraint-a"), expected_base_revision=0),
        service.write_constraint("member-workspace", _draft("constraint-b"), expected_base_revision=0),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, BaseException) for item in results) == 1
    conflicts = [item for item in results if isinstance(item, RevisionConflictError)]
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409


@pytest.mark.asyncio
async def test_audit_save_rejects_stale_task_input_token():
    itineraries, members, audits = await _context()
    report = await AuditApplicationService(
        itinerary_repository=itineraries,
        audit_repository=audits,
        member_constraint_repository=members,
    ).run_current_audit("member-workspace", now=NOW)
    itineraries.workspaces["member-workspace"] = itineraries.workspaces["member-workspace"].model_copy(
        update={"current_task_spec_revision": 2}
    )

    with pytest.raises(AuditInputStaleError) as stale:
        await audits.save_report(report.model_copy(update={"report_id": "stale-task-report"}))

    assert stale.value.context["expected_task_revision"] == 1
    assert stale.value.context["actual_task_revision"] == 2


@pytest.mark.asyncio
async def test_next_audit_binds_effective_member_revision_and_changes_hash():
    itineraries, members, audits = await _context()
    audit_service = AuditApplicationService(
        itinerary_repository=itineraries,
        audit_repository=audits,
        member_constraint_repository=members,
    )
    first_report = await audit_service.run_current_audit("member-workspace", now=NOW)
    itineraries.workspaces["member-workspace"] = itineraries.workspaces["member-workspace"].model_copy(
        update={"current_report_id": first_report.report_id}
    )

    await MemberConstraintService(members).write_constraint(
        "member-workspace", _draft(), expected_base_revision=0
    )
    with pytest.raises(AuditInputStaleError) as stale:
        await audits.save_report(first_report.model_copy(update={"report_id": "stale-report"}))
    assert stale.value.code == "AUDIT_INPUT_STALE"
    assert stale.value.context["expected_member_constraint_revision"] == 0
    assert stale.value.context["actual_member_constraint_revision"] == 1

    second_report = await audit_service.run_current_audit("member-workspace", now=NOW)

    assert first_report.member_constraint_revision_set == {}
    assert second_report.member_constraint_revision_set == {"walking-limit": 1}
    assert second_report.report_input_hash != first_report.report_input_hash
    assert await audits.get_report(first_report.report_id) == first_report
