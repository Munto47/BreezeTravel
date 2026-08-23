from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.audit.models import (
    AuditFinding,
    AuditReport,
    AuditSeverity,
    AuditStatus,
    EvidenceFact,
    EvidenceFreshness,
    EvidenceSnapshot,
)
from app.itineraries.hash_service import sha256_canonical, with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    RevisionSource,
    TripDateRange,
)
from app.repairs.candidates import FrozenRepairCandidate, freeze_candidate_set
from app.repairs.errors import UnverifiedCandidateRejectedError
from app.repairs.models import RepairOperation, RepairOperationType, RepairOption
from app.trip_check.executor import build_advice_bundle
from app.trip_check.models import (
    RunBudget,
    RunSpec,
    TripCheckRun,
    TripCheckRunStatus,
    TripCheckStage,
)


NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)


def _context(*, operation: RepairOperation | None = None):
    date_range = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="p4-advice-itinerary",
        workspace_id="p4-advice-workspace",
        revision=1,
        source_type=RevisionSource.IMPORT,
        city="北京",
        date_range=date_range,
        days=[ItineraryDay(day_index=0, date=date_range.start), ItineraryDay(day_index=1, date=date_range.end)],
        created_by="p4-test",
        created_at=NOW,
    ))
    snapshot = EvidenceSnapshot(
        snapshot_id="p4-advice-snapshot",
        workspace_id=revision.workspace_id,
        itinerary_revision=1,
        provider_set=["fixture"],
        policy_version="p4-test-v1",
        facts=[EvidenceFact(
            fact_id="p4-fact-1",
            snapshot_id="p4-advice-snapshot",
            subject_type="PLACE",
            subject_id="source-place",
            fact_type="OPENING_HOURS",
            value={"status": "closed"},
            provider="fixture",
            observed_at=NOW,
            response_hash="a" * 64,
            confidence=1,
            freshness_status=EvidenceFreshness.FRESH,
        )],
        created_at=NOW,
    )
    finding = AuditFinding(
        finding_id="p4-finding-1",
        rule_id="audit.p4-test",
        rule_version="1",
        status=AuditStatus.VIOLATED,
        severity=AuditSeverity.HIGH,
        reason_code="PLACE_REPLACEMENT_REQUIRED",
        message="地点不可用",
        evidence_fact_ids=["p4-fact-1"],
        repairable=operation is not None,
        confirmation_action="请按区域和开放时间筛选替代地点",
    )
    report = AuditReport(
        report_id="p4-advice-report",
        workspace_id=revision.workspace_id,
        itinerary_id=revision.itinerary_id,
        itinerary_revision=1,
        task_id="p4-task",
        task_revision=1,
        evidence_snapshot_id=snapshot.snapshot_id,
        audit_rule_set_version="p4-test-v1",
        report_input_hash="b" * 64,
        overall_status=AuditStatus.VIOLATED,
        findings=[finding],
        created_at=NOW,
    )
    run_spec = RunSpec(
        commit_sha="3ea92a4",
        prompt_version="deterministic-advice-v1",
        model_version="none",
        provider_version="fixture-v1",
        rule_set_version="p4-test-v1",
        execution_mode="fixture",
        dataset_hash="d" * 64,
        snapshot_hash="e" * 64,
        fault_profile="none",
        random_seed=20260823,
        budget=RunBudget(timeout_seconds=2),
    )
    run = TripCheckRun(
        run_id="p4-run",
        workspace_id=revision.workspace_id,
        brief_id="p4-brief",
        brief_revision=1,
        itinerary_revision=1,
        stage=TripCheckStage.BUILD_ADVICE,
        run_spec=run_spec,
        config_hash=sha256_canonical(run_spec.model_dump(mode="json")),
        status=TripCheckRunStatus.RUNNING,
        created_by="p4-test",
        created_at=NOW,
        updated_at=NOW,
    )
    repairs = []
    if operation is not None:
        repairs = [RepairOption(
            repair_id="p4-repair",
            source_report_id=report.report_id,
            base_itinerary_revision=1,
            operations=[operation],
            targeted_finding_ids=[finding.finding_id],
            edit_cost=4,
            risk_cost=0,
            new_unknown_count=0,
            result_preview=revision.model_copy(update={"revision": 2, "parent_revision": 1}),
            postcheck_report_id="p4-postcheck",
            created_at=NOW,
        )]
    return run, report, snapshot, repairs


def test_every_non_pass_finding_gets_evidence_bounded_fallback_advice():
    run, report, snapshot, repairs = _context()

    bundle = build_advice_bundle(
        run=run,
        report=report,
        snapshot=snapshot,
        repairs=repairs,
        evidence_receipt_id="evidence-receipt",
    )

    assert [item.finding_id for item in bundle.actions] == ["p4-finding-1"]
    assert bundle.actions[0].candidate_set_id is None
    assert bundle.actions[0].repair_id is None
    assert "筛选" in bundle.actions[0].action


def test_specific_place_repair_is_bound_to_frozen_place_and_route_receipts():
    operation = RepairOperation(
        operation=RepairOperationType.REPLACE_STOP,
        payload={
            "stop_id": "source-stop",
            "candidate_set_id": "p4-candidates",
            "candidate_place_id": "verified-place",
        },
        rationale="用已核验候选替换不可用地点",
    )
    run, report, snapshot, repairs = _context(operation=operation)
    candidate_set = freeze_candidate_set("p4-candidates", [FrozenRepairCandidate(
        canonical_place_id="verified-place",
        display_name="已核验地点",
        place_receipt_id="place-receipt",
        route_receipt_ids=("route-receipt",),
    )])

    bundle = build_advice_bundle(
        run=run,
        report=report,
        snapshot=snapshot,
        repairs=repairs,
        evidence_receipt_id="evidence-receipt",
        candidate_sets={candidate_set.candidate_set_id: candidate_set},
    )

    action = bundle.actions[0]
    assert action.candidate_set_id == "p4-candidates"
    assert action.provider_receipt_ids == [
        "evidence-receipt", "place-receipt", "route-receipt"
    ]


def test_specific_place_outside_frozen_set_is_rejected():
    operation = RepairOperation(
        operation=RepairOperationType.REPLACE_STOP,
        payload={
            "stop_id": "source-stop",
            "candidate_set_id": "p4-candidates",
            "candidate_place_id": "invented-place",
        },
        rationale="替换地点",
    )
    run, report, snapshot, repairs = _context(operation=operation)

    with pytest.raises(UnverifiedCandidateRejectedError) as captured:
        build_advice_bundle(
            run=run,
            report=report,
            snapshot=snapshot,
            repairs=repairs,
            evidence_receipt_id="evidence-receipt",
            candidate_sets={},
        )

    assert captured.value.code == "UNVERIFIED_CANDIDATE_REJECTED"
