from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.audit.fact_rules import PlaceCityRule
from app.audit.models import AuditSeverity, AuditStatus, EvidenceFact, EvidenceFreshness, EvidenceSnapshot
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


NOW = datetime(2026, 8, 21, 3, tzinfo=timezone.utc)


def _revision():
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    return with_content_hash(
        ItineraryRevisionContent(
            itinerary_id="itin-city",
            workspace_id="workspace-city",
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
                            stop_id="stop-1",
                            place_id="poi-1",
                            day_index=0,
                            order_index=0,
                            raw_name="测试地点",
                        ),
                    ],
                ),
                ItineraryDay(day_index=1, date=date_range.end, stops=[]),
            ],
            created_by="tester",
            created_at=NOW,
        )
    )


def _fact(*, status: EvidenceFreshness, city: str | None = "北京", fact_id: str = "fact-city") -> EvidenceFact:
    value = {"name": "测试地点"}
    if city is not None:
        value["city"] = city
    return EvidenceFact(
        fact_id=fact_id,
        snapshot_id="snapshot-city",
        subject_type="PLACE",
        subject_id="poi-1",
        fact_type="POI_IDENTITY",
        value=value,
        provider="amap",
        source_url="https://restapi.amap.com/example",
        observed_at=NOW,
        response_hash="a" * 64,
        confidence=1,
        freshness_status=status,
    )


def _context(*facts: EvidenceFact) -> AuditRuleContext:
    revision = _revision()
    task = TripTaskSpec(
        task_id="task-city",
        room_id="room-city",
        task_revision=1,
        city="北京",
        date_range=DateRange(start=date(2026, 9, 1), days=2),
    )
    return AuditRuleContext(
        task_spec=task,
        revision=revision,
        evidence_snapshot=EvidenceSnapshot(
            snapshot_id="snapshot-city",
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            provider_set=["amap"],
            policy_version="test-v1",
            facts=list(facts),
            created_at=NOW,
        ),
        now=NOW,
    )


def test_fresh_identity_normalizes_municipality_suffix_and_satisfies_city_rule():
    finding = PlaceCityRule().evaluate(_context(_fact(status=EvidenceFreshness.FRESH, city=" 北京市 ")))[0]

    assert finding.status == AuditStatus.SATISFIED
    assert finding.severity == AuditSeverity.INFO
    assert finding.reason_code == "PLACE_CITY_MATCH"
    assert finding.evidence_fact_ids == ["fact-city"]
    assert finding.affected_days == [0]
    assert finding.affected_stop_ids == ["stop-1"]


def test_fresh_identity_from_another_city_is_a_blocker():
    finding = PlaceCityRule().evaluate(_context(_fact(status=EvidenceFreshness.FRESH, city="上海市")))[0]

    assert finding.status == AuditStatus.VIOLATED
    assert finding.severity == AuditSeverity.BLOCKER
    assert finding.reason_code == "PLACE_CITY_MISMATCH"
    assert finding.input_values["target_city"] == "北京"
    assert finding.input_values["observed_city"] == "上海市"
    assert finding.repairable is True


@pytest.mark.parametrize(
    ("facts", "expected_ids"),
    [
        ((), []),
        ((_fact(status=EvidenceFreshness.STALE, fact_id="stale"),), ["stale"]),
        ((_fact(status=EvidenceFreshness.UNAVAILABLE, city=None, fact_id="unavailable"),), ["unavailable"]),
        ((_fact(status=EvidenceFreshness.FRESH, city=None, fact_id="missing-city"),), ["missing-city"]),
    ],
)
def test_missing_or_unusable_identity_city_remains_high_unknown(facts, expected_ids):
    finding = PlaceCityRule().evaluate(_context(*facts))[0]

    assert finding.status == AuditStatus.UNKNOWN
    assert finding.severity == AuditSeverity.HIGH
    assert finding.reason_code == "PLACE_CITY_UNKNOWN"
    assert finding.evidence_fact_ids == expected_ids


def test_conflicting_identity_is_left_exclusively_to_conflict_rule():
    facts = (
        _fact(status=EvidenceFreshness.CONFLICTING, city="北京", fact_id="conflict-a"),
        _fact(status=EvidenceFreshness.CONFLICTING, city="上海", fact_id="conflict-b"),
    )

    assert PlaceCityRule().evaluate(_context(*facts)) == []
