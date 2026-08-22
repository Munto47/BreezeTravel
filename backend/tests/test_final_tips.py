from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.audit.models import (
    AuditFinding,
    AuditReport,
    AuditSeverity,
    AuditStatus,
    EvidenceSnapshot,
)
from app.audit.repositories import InMemoryAuditRepository
from app.itineraries.errors import TipsInputConflictError, TipsNotEligibleError
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
from app.itineraries.tips_repositories import InMemoryFinalTipsRepository
from app.itineraries.tips_service import FinalTipsService


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


async def _context(*, high_violation: bool = False):
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    revision = with_content_hash(
        ItineraryRevisionContent(
            itinerary_id="tips-itinerary",
            workspace_id="tips-workspace",
            revision=1,
            source_type=RevisionSource.REPAIR,
            city="北京",
            date_range=date_range,
            days=[
                ItineraryDay(
                    day_index=0,
                    date=date_range.start,
                    stops=[
                        ItineraryStop(
                            stop_id="tips-stop",
                            place_id="tips-place",
                            day_index=0,
                            order_index=0,
                            start_time="09:00",
                            end_time="11:00",
                            raw_name="测试地点",
                        )
                    ],
                ),
                ItineraryDay(day_index=1, date=date_range.end, stops=[]),
            ],
            created_by="tips-user",
            created_at=NOW,
        )
    )
    report_id = "tips-report"
    workspace = TripWorkspace(
        workspace_id=revision.workspace_id,
        room_id="tips-room",
        city="北京",
        trip_date_range=date_range,
        current_itinerary_revision=1,
        current_report_id=report_id,
        created_by="tips-user",
        created_at=NOW,
        updated_at=NOW,
    )
    itinerary_repository = InMemoryItineraryRepository()
    await itinerary_repository.create_workspace(workspace, revision)
    finding = AuditFinding(
        finding_id="tips-blocker",
        rule_id="constraint.time_chain",
        rule_version="1.0.0",
        status=AuditStatus.VIOLATED,
        severity=AuditSeverity.HIGH,
        reason_code="TIME_CHAIN_BROKEN",
        message="时间冲突",
    )
    report = AuditReport(
        report_id=report_id,
        workspace_id=revision.workspace_id,
        itinerary_id=revision.itinerary_id,
        itinerary_revision=1,
        task_id="tips-task",
        task_revision=1,
        evidence_snapshot_id="tips-snapshot",
        audit_rule_set_version="rules-v1",
        report_input_hash="a" * 64,
        overall_status=AuditStatus.VIOLATED if high_violation else AuditStatus.SATISFIED,
        findings=[finding] if high_violation else [],
        created_at=NOW,
    )
    snapshot = EvidenceSnapshot(
        snapshot_id="tips-snapshot",
        workspace_id=revision.workspace_id,
        itinerary_revision=1,
        policy_version="policy-v1",
        created_at=NOW,
    )
    audit_repository = InMemoryAuditRepository()
    audit_repository.reports[report_id] = report
    audit_repository.snapshots[snapshot.snapshot_id] = snapshot
    audit_repository.current_revisions[revision.workspace_id] = 1
    audit_repository.current_reports[revision.workspace_id] = report_id
    return itinerary_repository, audit_repository, report


@pytest.mark.asyncio
async def test_tips_are_bound_to_current_revision_and_report_and_are_idempotent():
    itinerary_repository, audit_repository, report = await _context()
    generated_preferences: list[str] = []

    async def generator(itinerary, preferences):
        generated_preferences.append(preferences)
        days = []
        for day in itinerary.days:
            slots = [slot.model_copy(update={"tips": ["基于最终报告的提示"]}) for slot in day.slots]
            days.append(day.model_copy(update={"slots": slots}))
        return itinerary.model_copy(update={"days": days})

    tips_repository = InMemoryFinalTipsRepository()
    service = FinalTipsService(
        itinerary_repository=itinerary_repository,
        audit_repository=audit_repository,
        tips_repository=tips_repository,
        generator=generator,
    )
    first = await service.generate_for_report(report.report_id, preferences="少走路", now=NOW)
    second = await service.generate_for_report(report.report_id, preferences="少走路", now=NOW)

    assert first == second
    assert len(generated_preferences) == 1
    assert report.report_id in generated_preferences[0]
    assert first.itinerary_revision == 1
    assert first.itinerary.version == 1
    assert first.itinerary.days[0].slots[0].tips == ["基于最终报告的提示"]
    assert len(first.basis_content_hash) == 64
    assert len(first.artifact_hash) == 64

    with pytest.raises(TipsInputConflictError):
        await service.generate_for_report(report.report_id, preferences="不同文本", now=NOW)


@pytest.mark.asyncio
async def test_tips_refuse_stale_report_even_if_revision_still_exists():
    itinerary_repository, audit_repository, report = await _context()
    workspace = await itinerary_repository.get_workspace(report.workspace_id)
    itinerary_repository.workspaces[report.workspace_id] = workspace.model_copy(
        update={
            "current_report_id": "newer-report",
        }
    )
    service = FinalTipsService(
        itinerary_repository=itinerary_repository,
        audit_repository=audit_repository,
        tips_repository=InMemoryFinalTipsRepository(),
    )
    with pytest.raises(TipsNotEligibleError, match="current itinerary revision"):
        await service.generate_for_report(report.report_id)


@pytest.mark.asyncio
async def test_tips_refuse_unrepaired_high_finding():
    itinerary_repository, audit_repository, report = await _context(high_violation=True)
    service = FinalTipsService(
        itinerary_repository=itinerary_repository,
        audit_repository=audit_repository,
        tips_repository=InMemoryFinalTipsRepository(),
    )
    with pytest.raises(TipsNotEligibleError, match="BLOCKER/HIGH"):
        await service.generate_for_report(report.report_id)
