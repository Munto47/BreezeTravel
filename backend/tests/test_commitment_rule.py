from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.audit.models import AuditSeverity, AuditStatus, EvidenceFact, EvidenceFreshness, EvidenceSnapshot
from app.audit.registry import AuditRuleContext
from app.audit.route_rules import CommitmentFeasibilityRule
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


NOW = datetime(2026, 8, 21, 6, tzinfo=timezone.utc)


def _stop(
    stop_id: str,
    order: int,
    start: str | None,
    end: str | None,
    *,
    name: str | None = None,
    kind: CommitmentKind | None = None,
    fixed: bool = False,
    locked: bool = False,
    mode: str = "driving",
) -> ItineraryStop:
    return ItineraryStop(
        stop_id=stop_id,
        place_id=f"poi-{stop_id}",
        day_index=0,
        order_index=order,
        start_time=start,
        end_time=end,
        raw_name=name or stop_id,
        commitment_kind=kind,
        fixed_commitment=fixed,
        locked=locked,
        category="transport" if kind == CommitmentKind.RETURN_DEPARTURE else "attraction",
        transport_to_next=RevisionTransport(mode=mode),
    )


def _context(stops: list[ItineraryStop], facts: list[EvidenceFact]) -> AuditRuleContext:
    trip_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="itin-commitment",
        workspace_id="workspace-commitment",
        revision=1,
        source_type=RevisionSource.IMPORT,
        city="上海",
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
            task_id="task-commitment",
            room_id="room-commitment",
            task_revision=1,
            city="上海",
            date_range=DateRange(start=trip_range.start, days=2),
        ),
        revision=revision,
        evidence_snapshot=EvidenceSnapshot(
            snapshot_id="snapshot-commitment",
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
    duration: int = 35,
    mode: str = "driving",
    freshness: EvidenceFreshness = EvidenceFreshness.FRESH,
) -> EvidenceFact:
    return EvidenceFact(
        fact_id="commitment-route-fact",
        snapshot_id="snapshot-commitment",
        subject_type="ROUTE_EDGE",
        subject_id="previous->commitment",
        fact_type="ROUTE_TIME",
        value={"duration_minutes": duration, "mode": mode},
        provider="amap_route",
        observed_at=NOW,
        valid_until=NOW + timedelta(minutes=15),
        response_hash="c" * 64,
        confidence=1,
        freshness_status=freshness,
    )


def _previous(*, end: str | None = "10:00", mode: str = "driving") -> ItineraryStop:
    return _stop("previous", 0, "09:00", end, mode=mode)


def _commitment(
    *,
    start: str | None = "10:30",
    name: str = "上海博物馆",
    kind: CommitmentKind = CommitmentKind.FIXED_VISIT,
    fixed: bool = True,
    locked: bool = True,
) -> ItineraryStop:
    return _stop(
        "commitment",
        1,
        start,
        "12:00",
        name=name,
        kind=kind,
        fixed=fixed,
        locked=locked,
    )


@pytest.mark.parametrize(("fixed", "locked"), [(False, False), (True, False), (False, True)])
def test_fixed_commitment_requires_both_fixed_and_locked_flags(fixed: bool, locked: bool) -> None:
    stop = _commitment(fixed=fixed, locked=locked)

    finding = CommitmentFeasibilityRule().evaluate(_context([_previous(), stop], [_fact()]))[0]

    assert finding.status == AuditStatus.VIOLATED
    assert finding.severity == AuditSeverity.BLOCKER
    assert finding.reason_code == "COMMITMENT_LOCK_INCOMPLETE"


def test_fixed_appointment_predecessor_route_conflict_is_blocker() -> None:
    finding = CommitmentFeasibilityRule().evaluate(
        _context([_previous(), _commitment(start="10:30")], [_fact(duration=35)])
    )[0]

    assert finding.status == AuditStatus.VIOLATED
    assert finding.severity == AuditSeverity.BLOCKER
    assert finding.reason_code == "FIXED_COMMITMENT_CONFLICT"
    assert finding.input_values["available_minutes"] == 30
    assert finding.input_values["required_total_minutes"] == 35
    assert finding.evidence_fact_ids == ["commitment-route-fact"]


def test_fixed_appointment_with_enough_gap_is_satisfied() -> None:
    finding = CommitmentFeasibilityRule().evaluate(
        _context([_previous(), _commitment(start="10:35")], [_fact(duration=35)])
    )[0]

    assert finding.status == AuditStatus.SATISFIED
    assert finding.reason_code == "FIXED_COMMITMENT_FEASIBLE"


def test_rail_return_uses_versioned_thirty_minute_buffer() -> None:
    returned = _commitment(
        start="11:00",
        name="上海虹桥站高铁返程",
        kind=CommitmentKind.RETURN_DEPARTURE,
    )

    finding = CommitmentFeasibilityRule().evaluate(
        _context([_previous(), returned], [_fact(duration=35)])
    )[0]

    assert finding.status == AuditStatus.VIOLATED
    assert finding.reason_code == "RETURN_DEPARTURE_CONFLICT"
    assert finding.severity == AuditSeverity.BLOCKER
    assert finding.input_values["terminal_type"] == "RAIL"
    assert finding.input_values["required_buffer_minutes"] == 30
    assert finding.input_values["buffer_policy_version"] == "commitment-buffer-v1"
    assert finding.input_values["required_total_minutes"] == 65


def test_flight_return_uses_sixty_minute_buffer() -> None:
    returned = _commitment(
        start="11:40",
        name="浦东机场航班返程",
        kind=CommitmentKind.RETURN_DEPARTURE,
    )

    finding = CommitmentFeasibilityRule().evaluate(
        _context([_previous(), returned], [_fact(duration=35)])
    )[0]

    assert finding.status == AuditStatus.SATISFIED
    assert finding.reason_code == "RETURN_DEPARTURE_FEASIBLE"
    assert finding.input_values["terminal_type"] == "FLIGHT"
    assert finding.input_values["required_buffer_minutes"] == 60


def test_unknown_return_transport_type_never_guesses_a_buffer() -> None:
    returned = _commitment(
        name="集合点返程",
        kind=CommitmentKind.RETURN_DEPARTURE,
    )

    finding = CommitmentFeasibilityRule().evaluate(_context([_previous(), returned], [_fact()]))[0]

    assert finding.status == AuditStatus.UNKNOWN
    assert finding.reason_code == "RETURN_TRANSPORT_TYPE_UNKNOWN"
    assert finding.severity == AuditSeverity.BLOCKER


def test_unknown_first_stop_return_transport_type_is_also_unknown() -> None:
    returned = _stop(
        "commitment",
        0,
        "10:30",
        "12:00",
        name="集合点返程",
        kind=CommitmentKind.RETURN_DEPARTURE,
        fixed=True,
        locked=True,
    )

    finding = CommitmentFeasibilityRule().evaluate(_context([returned], []))[0]

    assert finding.status == AuditStatus.UNKNOWN
    assert finding.reason_code == "RETURN_TRANSPORT_TYPE_UNKNOWN"


@pytest.mark.parametrize(
    ("facts", "mode", "unknown_reason"),
    [
        ([], "driving", "ROUTE_EDGE_FACT_MISSING"),
        ([_fact(freshness=EvidenceFreshness.STALE)], "driving", "ROUTE_EDGE_FACT_NOT_FRESH"),
        ([_fact(freshness=EvidenceFreshness.CONFLICTING)], "driving", "ROUTE_EDGE_FACT_CONFLICTING"),
        ([_fact(mode="walking")], "driving", "ROUTE_EDGE_MODE_MISMATCH"),
    ],
)
def test_commitment_route_uncertainty_stays_unknown(
    facts: list[EvidenceFact],
    mode: str,
    unknown_reason: str,
) -> None:
    finding = CommitmentFeasibilityRule().evaluate(
        _context([_previous(mode=mode), _commitment()], facts)
    )[0]

    assert finding.status == AuditStatus.UNKNOWN
    assert finding.reason_code == "FIXED_COMMITMENT_ROUTE_UNKNOWN"
    assert finding.input_values["evidence_unknown_reason"] == unknown_reason


def test_missing_commitment_or_predecessor_time_is_unknown() -> None:
    missing_commitment_time = CommitmentFeasibilityRule().evaluate(
        _context([_previous(), _commitment(start=None)], [_fact()])
    )[0]
    missing_predecessor_time = CommitmentFeasibilityRule().evaluate(
        _context([_previous(end=None), _commitment()], [_fact()])
    )[0]

    assert missing_commitment_time.reason_code == "FIXED_COMMITMENT_TIME_UNKNOWN"
    assert missing_commitment_time.status == AuditStatus.UNKNOWN
    assert missing_predecessor_time.reason_code == "FIXED_COMMITMENT_TIME_UNKNOWN"
    assert missing_predecessor_time.status == AuditStatus.UNKNOWN


def test_overlap_is_left_to_time_chain_without_duplicate_commitment_finding() -> None:
    assert CommitmentFeasibilityRule().evaluate(
        _context([_previous(end="11:00"), _commitment(start="10:30")], [_fact()])
    ) == []


def test_arrival_commitment_does_not_create_fixed_or_return_false_positive() -> None:
    arrival = _stop(
        "arrival",
        0,
        "14:00",
        "14:10",
        name="上海虹桥站抵达",
        kind=CommitmentKind.ARRIVAL,
    )

    assert CommitmentFeasibilityRule().evaluate(_context([arrival], [])) == []
