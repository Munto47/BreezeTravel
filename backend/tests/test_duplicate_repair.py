from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.audit.models import AuditFinding, AuditReport, AuditSeverity, AuditStatus
from app.audit.repositories import InMemoryAuditRepository
from app.audit.service import AuditApplicationService
from app.itineraries.hash_service import canonical_json, with_content_hash
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
from app.repairs.errors import RepairNoFeasibleOptionError
from app.repairs.models import RepairOperationType
from app.repairs.objective import introduces_new_high_violation
from app.repairs.repositories import InMemoryRepairRepository
from app.repairs.search import BoundedRepairSearch


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def _report_with_finding(report_id: str, finding: AuditFinding) -> AuditReport:
    return AuditReport(
        report_id=report_id,
        workspace_id="risk-identity-workspace",
        itinerary_id="risk-identity-itinerary",
        itinerary_revision=1,
        task_id="risk-identity-task",
        task_revision=1,
        evidence_snapshot_id=f"snapshot-{report_id}",
        audit_rule_set_version="test",
        report_input_hash="0" * 64,
        overall_status=finding.status,
        findings=[finding],
        created_at=NOW,
    )


def _place_high_finding(finding_id: str, place_id: str, stop_ids: list[str]) -> AuditFinding:
    return AuditFinding(
        finding_id=finding_id,
        rule_id="constraint.opening_hours",
        rule_version="1.0.0",
        status=AuditStatus.VIOLATED,
        severity=AuditSeverity.HIGH,
        reason_code="OUTSIDE_OPENING_HOURS",
        message="outside opening hours",
        input_values={"place_id": place_id, "day_stops": stop_ids},
        affected_days=[0],
        affected_stop_ids=stop_ids,
    )


def _stop(
    stop_id: str,
    place_id: str,
    day_index: int,
    order_index: int,
    start_time: str,
    end_time: str,
    *,
    category: str = "attraction",
    locked: bool = False,
    fixed_commitment: bool = False,
) -> ItineraryStop:
    start_hour, start_minute = (int(part) for part in start_time.split(":"))
    end_hour, end_minute = (int(part) for part in end_time.split(":"))
    return ItineraryStop(
        stop_id=stop_id,
        place_id=place_id,
        day_index=day_index,
        order_index=order_index,
        start_time=start_time,
        end_time=end_time,
        visit_duration_minutes=(end_hour * 60 + end_minute) - (start_hour * 60 + start_minute),
        raw_name="故宫博物院" if place_id == "duplicate-place" else "酒店",
        category=category,
        locked=locked,
        fixed_commitment=fixed_commitment,
    )


async def _context(
    *,
    earlier_locked: bool = False,
    later_fixed: bool = False,
    attraction_opening_hours: str | None = "08:00-18:00",
):
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    earlier = _stop(
        "duplicate-earlier",
        "duplicate-place",
        0,
        0,
        "09:00",
        "11:00",
        locked=earlier_locked,
    )
    later = _stop(
        "duplicate-later",
        "duplicate-place",
        1,
        0,
        "09:00",
        "11:00",
        fixed_commitment=later_fixed,
    )
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="duplicate-itinerary",
        workspace_id="duplicate-workspace",
        revision=1,
        source_type=RevisionSource.IMPORT,
        city="北京",
        date_range=date_range,
        days=[
            ItineraryDay(day_index=0, date=date_range.start, stops=[
                earlier,
                _stop("hotel-day-1", "hotel-place", 0, 1, "20:00", "21:00", category="hotel"),
            ]),
            ItineraryDay(day_index=1, date=date_range.end, stops=[
                later,
                _stop("hotel-day-2", "hotel-place", 1, 1, "20:00", "21:00", category="hotel"),
            ]),
        ],
        locked_commitments=[
            stop.stop_id for stop in (earlier, later) if stop.locked or stop.fixed_commitment
        ],
        created_by="repair-user",
        created_at=NOW,
    ))
    workspace = TripWorkspace(
        workspace_id=revision.workspace_id,
        room_id="duplicate-room",
        city="北京",
        trip_date_range=date_range,
        current_itinerary_revision=revision.revision,
        created_by="repair-user",
    )
    itinerary_repository = InMemoryItineraryRepository()
    await itinerary_repository.create_workspace(workspace, revision)
    audit_repository = InMemoryAuditRepository()
    audit_repository.current_revisions[workspace.workspace_id] = revision.revision
    audit_repository.place_records[workspace.workspace_id] = {
        "duplicate-place": {
            "place_id": "duplicate-place",
            "name": "故宫博物院",
            "city": "北京",
            "category": "attraction",
            "opening_hours": attraction_opening_hours,
            "retrieval_observed_at": NOW,
        },
        "hotel-place": {
            "place_id": "hotel-place",
            "name": "酒店",
            "city": "北京",
            "category": "hotel",
            "opening_hours": "00:00-23:59",
            "retrieval_observed_at": NOW,
        },
    }
    source_report = await AuditApplicationService(
        itinerary_repository=itinerary_repository,
        audit_repository=audit_repository,
    ).run_current_audit(workspace.workspace_id, now=NOW)
    itinerary_repository.workspaces[workspace.workspace_id] = workspace.model_copy(update={
        "current_report_id": source_report.report_id,
        "status": WorkspaceStatus.NEEDS_CONFIRMATION,
    })
    repair_repository = InMemoryRepairRepository(itinerary_repository, audit_repository)
    search = BoundedRepairSearch(
        itinerary_repository=itinerary_repository,
        audit_repository=audit_repository,
        repair_repository=repair_repository,
    )
    return search, audit_repository, revision, source_report


@pytest.mark.asyncio
async def test_duplicate_place_offers_earlier_and_later_removal_with_clean_postcheck():
    search, audit_repository, base, source_report = await _context()

    options = await search.propose(source_report.report_id, now=NOW)

    assert len(options) == 2
    assert [option.operations[0].operation for option in options] == [
        RepairOperationType.REMOVE_STOP,
        RepairOperationType.REMOVE_STOP,
    ]
    assert [option.operations[0].payload for option in options] == [
        {"stop_id": "duplicate-earlier"},
        {"stop_id": "duplicate-later"},
    ]
    assert "较早安排" in options[0].tradeoffs[0]
    assert "duplicate-earlier" in options[0].tradeoffs[0]
    assert "较晚安排" in options[1].tradeoffs[0]
    assert "duplicate-later" in options[1].tradeoffs[0]

    source_high = {
        (item.rule_id, item.reason_code, tuple(item.affected_days), tuple(item.affected_stop_ids))
        for item in source_report.findings
        if item.status == AuditStatus.VIOLATED
        and item.severity in {AuditSeverity.BLOCKER, AuditSeverity.HIGH}
    }
    for option in options:
        postcheck = await audit_repository.get_report(option.postcheck_report_id)
        assert postcheck is not None
        assert not any(
            item.status == AuditStatus.VIOLATED and item.reason_code == "DUPLICATE_PLACE"
            for item in postcheck.findings
        )
        postcheck_high = {
            (item.rule_id, item.reason_code, tuple(item.affected_days), tuple(item.affected_stop_ids))
            for item in postcheck.findings
            if item.status == AuditStatus.VIOLATED
            and item.severity in {AuditSeverity.BLOCKER, AuditSeverity.HIGH}
        }
        assert postcheck_high <= source_high
        assert option.new_unknown_count == 0
        assert option.targeted_finding_ids
        assert len([
            stop
            for day in option.result_preview.days
            for stop in day.stops
            if stop.place_id == "duplicate-place"
        ]) == 1

    assert canonical_json(base.days[0].model_dump(mode="json")) == canonical_json(
        options[1].result_preview.days[0].model_dump(mode="json")
    )
    assert canonical_json(base.days[1].model_dump(mode="json")) == canonical_json(
        options[0].result_preview.days[1].model_dump(mode="json")
    )


@pytest.mark.asyncio
async def test_duplicate_place_never_removes_locked_or_fixed_occurrence():
    search, _, base, source_report = await _context(earlier_locked=True)

    options = await search.propose(source_report.report_id, now=NOW)

    assert len(options) == 1
    assert options[0].operations[0].payload == {"stop_id": "duplicate-later"}
    locked_before = base.days[0].stops[0]
    locked_after = options[0].result_preview.days[0].stops[0]
    assert canonical_json(locked_before.model_dump(mode="json")) == canonical_json(
        locked_after.model_dump(mode="json")
    )


@pytest.mark.asyncio
async def test_existing_place_unknown_is_not_new_when_duplicate_stop_set_shrinks():
    search, audit_repository, _, source_report = await _context(attraction_opening_hours=None)
    source_opening_unknown = [
        item
        for item in source_report.findings
        if item.status == AuditStatus.UNKNOWN and item.reason_code == "OPENING_HOURS_MISSING"
    ]
    assert source_opening_unknown
    assert all(
        item.affected_stop_ids == ["duplicate-earlier", "duplicate-later"]
        for item in source_opening_unknown
    )

    options = await search.propose(source_report.report_id, now=NOW)

    assert len(options) == 2
    for option in options:
        assert option.new_unknown_count == 0
        postcheck = await audit_repository.get_report(option.postcheck_report_id)
        assert postcheck is not None
        postcheck_opening_unknown = [
            item
            for item in postcheck.findings
            if item.status == AuditStatus.UNKNOWN and item.reason_code == "OPENING_HOURS_MISSING"
        ]
        assert postcheck_opening_unknown
        assert all(len(item.affected_stop_ids) == 1 for item in postcheck_opening_unknown)


@pytest.mark.asyncio
async def test_duplicate_place_has_no_feasible_option_when_both_occurrences_are_protected():
    search, _, _, source_report = await _context(earlier_locked=True, later_fixed=True)

    with pytest.raises(RepairNoFeasibleOptionError) as captured:
        await search.propose(source_report.report_id, now=NOW)

    assert captured.value.code == "REPAIR_NO_FEASIBLE_OPTION"
    assert captured.value.context["unresolved_finding_ids"]


def test_high_risk_identity_uses_place_not_transient_stop_occurrences():
    source = _report_with_finding(
        "source-high",
        _place_high_finding("source-finding", "same-place", ["earlier", "later"]),
    )
    same_place = _report_with_finding(
        "same-place-high",
        _place_high_finding("same-place-finding", "same-place", ["later"]),
    )
    different_place = _report_with_finding(
        "different-place-high",
        _place_high_finding("different-place-finding", "other-place", ["other-stop"]),
    )

    assert introduces_new_high_violation(source, same_place) is False
    assert introduces_new_high_violation(source, different_place) is True
