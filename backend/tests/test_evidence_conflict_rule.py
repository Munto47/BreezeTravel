from __future__ import annotations

from datetime import date, datetime, timezone

from app.audit.engine import AuditEngine
from app.audit.fact_rules import EvidenceConflictRule
from app.audit.models import (
    AuditRunInput,
    AuditSeverity,
    AuditStatus,
    EvidenceFact,
    EvidenceFreshness,
    EvidenceSnapshot,
)
from app.audit.registry import AuditRuleContext
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
)
from app.schemas.task_spec import DateRange, TripTaskSpec


NOW = datetime(2026, 8, 21, 4, tzinfo=timezone.utc)


def _revision():
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    return with_content_hash(
        ItineraryRevisionContent(
            itinerary_id="itin-conflict",
            workspace_id="workspace-conflict",
            revision=1,
            source_type=RevisionSource.IMPORT,
            city="杭州",
            date_range=date_range,
            days=[
                ItineraryDay(
                    day_index=0,
                    date=date_range.start,
                    stops=[
                        ItineraryStop(
                            stop_id="stop-west-lake",
                            place_id="poi-west-lake",
                            day_index=0,
                            order_index=0,
                            start_time="09:00",
                            end_time="11:00",
                            raw_name="西湖景区",
                        ),
                    ],
                ),
                ItineraryDay(day_index=1, date=date_range.end, stops=[]),
            ],
            created_by="tester",
            created_at=NOW,
        )
    )


def _task() -> TripTaskSpec:
    return TripTaskSpec(
        task_id="task-conflict",
        room_id="room-conflict",
        task_revision=1,
        city="杭州",
        date_range=DateRange(start=date(2026, 9, 1), days=2),
    )


def _fact(*, fact_id: str, value: str, provider: str, source_url: str) -> EvidenceFact:
    return EvidenceFact(
        fact_id=fact_id,
        snapshot_id="snapshot-conflict",
        subject_type="PLACE",
        subject_id="poi-west-lake",
        fact_type="OPENING_HOURS",
        value=value,
        provider=provider,
        source_url=source_url,
        observed_at=NOW,
        response_hash=("a" if fact_id.endswith("a") else "b") * 64,
        confidence=0.9,
        freshness_status=EvidenceFreshness.CONFLICTING,
    )


def _snapshot() -> EvidenceSnapshot:
    return EvidenceSnapshot(
        snapshot_id="snapshot-conflict",
        workspace_id="workspace-conflict",
        itinerary_revision=1,
        provider_set=["official", "amap"],
        policy_version="test-v1",
        facts=[
            _fact(
                fact_id="opening-a",
                value="08:00-18:00",
                provider="official",
                source_url="https://wgly.hangzhou.gov.cn/example",
            ),
            _fact(
                fact_id="opening-b",
                value="closed",
                provider="amap",
                source_url="https://restapi.amap.com/example",
            ),
        ],
        created_at=NOW,
    )


def test_conflicting_facts_are_grouped_and_all_source_receipts_are_read_back():
    revision = _revision()
    findings = EvidenceConflictRule().evaluate(
        AuditRuleContext(
            task_spec=_task(),
            revision=revision,
            evidence_snapshot=_snapshot(),
            now=NOW,
        )
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding.status == AuditStatus.UNKNOWN
    assert finding.severity == AuditSeverity.HIGH
    assert finding.reason_code == "EVIDENCE_CONFLICT"
    assert finding.evidence_fact_ids == ["opening-a", "opening-b"]
    assert finding.affected_days == [0]
    assert finding.affected_stop_ids == ["stop-west-lake"]
    assert finding.input_values["subject_type"] == "PLACE"
    assert finding.input_values["subject_id"] == "poi-west-lake"
    assert finding.input_values["fact_type"] == "OPENING_HOURS"
    assert finding.input_values["conflicting_facts"] == [
        {
            "fact_id": "opening-a",
            "provider": "official",
            "source_url": "https://wgly.hangzhou.gov.cn/example",
            "observed_at": NOW.isoformat(),
        },
        {
            "fact_id": "opening-b",
            "provider": "amap",
            "source_url": "https://restapi.amap.com/example",
            "observed_at": NOW.isoformat(),
        },
    ]


def test_opening_conflict_suppresses_misleading_opening_hours_missing_finding():
    revision = _revision()
    task = _task()
    report = AuditEngine().run(
        run_input=AuditRunInput(
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            task_id=task.task_id,
            task_revision=task.task_revision,
            place_resolution_versions={"poi-west-lake": 1},
        ),
        revision=revision,
        task_spec=task,
        evidence_snapshot=_snapshot(),
        now=NOW,
    )

    assert any(item.reason_code == "EVIDENCE_CONFLICT" for item in report.findings)
    assert not any(item.reason_code == "OPENING_HOURS_MISSING" for item in report.findings)
