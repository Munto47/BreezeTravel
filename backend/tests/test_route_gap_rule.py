from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.audit.models import AuditSeverity, AuditStatus, EvidenceFact, EvidenceFreshness, EvidenceSnapshot
from app.audit.registry import AuditRuleContext
from app.audit.route_rules import CommitmentFeasibilityRule, RouteGapRule
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    CommitmentKind,
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    RevisionTransport,
    TripDateRange,
)
from app.schemas.task_spec import DateRange, TripTaskSpec


NOW = datetime(2026, 8, 21, 5, tzinfo=timezone.utc)


def _stop(
    stop_id: str,
    order: int,
    start: str,
    end: str,
    *,
    mode: str = "driving",
    commitment: CommitmentKind | None = None,
    fixed: bool = False,
    locked: bool = False,
) -> ItineraryStop:
    return ItineraryStop(
        stop_id=stop_id,
        place_id=f"poi-{stop_id}",
        day_index=0,
        order_index=order,
        start_time=start,
        end_time=end,
        raw_name=stop_id,
        transport_to_next=RevisionTransport(mode=mode),
        commitment_kind=commitment,
        fixed_commitment=fixed,
        locked=locked,
    )


def _context(stops: list[ItineraryStop], facts: list[EvidenceFact]) -> AuditRuleContext:
    trip_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="itin-route-gap",
        workspace_id="workspace-route-gap",
        revision=1,
        source_type=RevisionSource.IMPORT,
        city="北京",
        date_range=trip_range,
        days=[
            ItineraryDay(day_index=0, date=trip_range.start, stops=stops),
            ItineraryDay(day_index=1, date=trip_range.end, stops=[]),
        ],
        created_by="tester",
        created_at=NOW,
    ))
    return AuditRuleContext(
        task_spec=TripTaskSpec(
            task_id="task-route-gap",
            room_id="room-route-gap",
            task_revision=1,
            city="北京",
            date_range=DateRange(start=trip_range.start, days=2),
        ),
        revision=revision,
        evidence_snapshot=EvidenceSnapshot(
            snapshot_id="snapshot-route-gap",
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            provider_set=["amap_route"],
            policy_version="test-v1",
            facts=facts,
            created_at=NOW,
        ),
        now=NOW,
    )


def _fact(
    *,
    edge_id: str = "a->b",
    duration: int = 45,
    mode: str = "driving",
    freshness: EvidenceFreshness = EvidenceFreshness.FRESH,
    fact_id: str = "route-fact",
) -> EvidenceFact:
    return EvidenceFact(
        fact_id=fact_id,
        snapshot_id="snapshot-route-gap",
        subject_type="ROUTE_EDGE",
        subject_id=edge_id,
        fact_type="ROUTE_TIME",
        value={"duration_minutes": duration, "mode": mode},
        provider="amap_route",
        observed_at=NOW,
        valid_until=NOW + timedelta(minutes=15),
        response_hash=("a" if fact_id == "route-fact" else "b") * 64,
        confidence=1,
        freshness_status=freshness,
    )


def test_exact_adjacent_edge_uses_fresh_duration_and_reports_insufficient_gap() -> None:
    stops = [_stop("a", 0, "09:00", "10:00"), _stop("b", 1, "10:30", "12:00")]
    context = _context(stops, [_fact(edge_id="wrong->edge", duration=1, fact_id="wrong"), _fact()])

    finding = RouteGapRule().evaluate(context)[0]

    assert finding.rule_id == "audit.route_gap"
    assert finding.reason_code == "ROUTE_GAP_INSUFFICIENT"
    assert finding.status == AuditStatus.VIOLATED
    assert finding.severity == AuditSeverity.HIGH
    assert finding.evidence_fact_ids == ["route-fact"]
    assert finding.input_values["available_minutes"] == 30
    assert finding.input_values["route_duration_minutes"] == 45
    assert finding.affected_stop_ids == ["a", "b"]


def test_gap_equal_to_fresh_route_duration_is_satisfied() -> None:
    stops = [_stop("a", 0, "09:00", "10:00"), _stop("b", 1, "10:45", "12:00")]

    finding = RouteGapRule().evaluate(_context(stops, [_fact()]))[0]

    assert finding.status == AuditStatus.SATISFIED
    assert finding.reason_code == "ROUTE_GAP_SUFFICIENT"


@pytest.mark.parametrize(
    ("facts", "left_mode", "unknown_reason"),
    [
        ([], "driving", "ROUTE_EDGE_FACT_MISSING"),
        ([_fact(freshness=EvidenceFreshness.STALE)], "driving", "ROUTE_EDGE_FACT_NOT_FRESH"),
        ([_fact(freshness=EvidenceFreshness.UNAVAILABLE)], "driving", "ROUTE_EDGE_FACT_NOT_FRESH"),
        ([_fact(freshness=EvidenceFreshness.CONFLICTING)], "driving", "ROUTE_EDGE_FACT_CONFLICTING"),
        ([_fact(mode="walking")], "driving", "ROUTE_EDGE_MODE_MISMATCH"),
    ],
)
def test_missing_stale_conflicting_unavailable_or_mode_mismatch_stays_unknown(
    facts: list[EvidenceFact],
    left_mode: str,
    unknown_reason: str,
) -> None:
    stops = [_stop("a", 0, "09:00", "10:00", mode=left_mode), _stop("b", 1, "11:00", "12:00")]

    finding = RouteGapRule().evaluate(_context(stops, facts))[0]

    assert finding.status == AuditStatus.UNKNOWN
    assert finding.reason_code == "ROUTE_GAP_EVIDENCE_UNKNOWN"
    assert finding.input_values["evidence_unknown_reason"] == unknown_reason


def test_overlap_is_owned_by_time_chain_and_not_duplicated_as_route_gap() -> None:
    stops = [_stop("a", 0, "09:00", "11:00"), _stop("b", 1, "10:30", "12:00")]

    assert RouteGapRule().evaluate(_context(stops, [_fact()])) == []


def test_fixed_and_return_edges_are_delegated_to_commitment_rule() -> None:
    fixed = _stop(
        "b",
        1,
        "10:30",
        "12:00",
        commitment=CommitmentKind.FIXED_VISIT,
        fixed=True,
        locked=True,
    )
    stops = [_stop("a", 0, "09:00", "10:00"), fixed]
    context = _context(stops, [_fact()])

    assert RouteGapRule().evaluate(context) == []
    finding = CommitmentFeasibilityRule().evaluate(context)[0]
    assert finding.reason_code == "FIXED_COMMITMENT_CONFLICT"
    assert finding.severity == AuditSeverity.BLOCKER


def test_arrival_as_left_stop_does_not_suppress_ordinary_route_gap() -> None:
    arrival = _stop("a", 0, "09:00", "09:10", commitment=CommitmentKind.ARRIVAL)
    next_stop = _stop("b", 1, "09:40", "12:00")

    finding = RouteGapRule().evaluate(_context([arrival, next_stop], [_fact(duration=45)]))[0]

    assert finding.reason_code == "ROUTE_GAP_INSUFFICIENT"
