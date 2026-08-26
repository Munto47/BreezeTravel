from __future__ import annotations

from collections.abc import Iterable

from app.audit.models import (
    AuditReport,
    AuditSeverity,
    AuditStatus,
    EvidenceFreshness,
    EvidenceSnapshot,
)
from app.itineraries.hash_service import canonical_json
from app.itineraries.models import ItineraryRevision
from app.repairs.errors import RepairUnsafePostcheckError
from app.repairs.models import RepairOperation, RepairOperationType, RepairOption


_EDIT_WEIGHTS = {
    RepairOperationType.ADJUST_TIME: 1.0,
    RepairOperationType.MOVE_WITHIN_DAY: 1.5,
    RepairOperationType.INSERT_BREAK: 1.5,
    RepairOperationType.INSERT_MEAL: 2.0,
    RepairOperationType.MOVE_TO_DAY: 3.0,
    RepairOperationType.REPLACE_STOP: 4.0,
    RepairOperationType.CHANGE_HOTEL_AREA: 5.0,
    RepairOperationType.REMOVE_STOP: 8.0,
    RepairOperationType.SPLIT_GROUP: 10.0,
}


_STABLE_INPUT_KEYS = (
    "stop_id",
    "member_id",
    "constraint_id",
    "edge_id",
    "from_place_id",
    "to_place_id",
    "category",
    "meal_window",
    "field",
)


def _freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple, set)):
        return tuple(_freeze(item) for item in value)
    return value


def _risk_identity(finding) -> tuple:
    """Identify a risk by its semantic subject, not its transient stop set.

    A repair may remove or reorder one occurrence without changing the
    underlying place-level risk.  Using all affected stop IDs as identity would
    therefore turn an existing UNKNOWN into an apparently new one.  Place ID is
    the strongest subject key; other stable input fields are used when present,
    followed by member/day scope and finally the stop set as a last resort.
    """

    place_id = finding.input_values.get("place_id")
    if place_id is not None and place_id != "":
        subject = ("place", _freeze(place_id))
    else:
        semantic_inputs = tuple(
            (key, _freeze(finding.input_values[key]))
            for key in _STABLE_INPUT_KEYS
            if finding.input_values.get(key) is not None and finding.input_values.get(key) != ""
        )
        if semantic_inputs:
            subject = ("inputs", semantic_inputs)
        elif finding.affected_member_ids:
            subject = ("members", tuple(sorted(finding.affected_member_ids)))
        elif finding.affected_days:
            subject = ("day_scope", tuple(sorted(finding.affected_days)))
        elif finding.affected_stop_ids:
            subject = ("stops", tuple(sorted(finding.affected_stop_ids)))
        else:
            subject = ("global",)
    return (
        finding.rule_id,
        finding.reason_code,
        tuple(sorted(finding.affected_days)),
        subject,
    )


def edit_cost(operations: list[RepairOperation]) -> float:
    return sum(_EDIT_WEIGHTS[item.operation] for item in operations)


def _required_route_edges(revision: ItineraryRevision) -> set[str]:
    return {
        f"{left.stop_id}->{right.stop_id}"
        for day in revision.days
        for left, right in zip(day.stops, day.stops[1:])
    }


def _route_minutes(snapshot: EvidenceSnapshot, required_edges: set[str]) -> float | None:
    """Return a complete route total, or None when any required edge is unknown.

    Only fresh ROUTE_TIME facts are usable.  Missing, stale, conflicting,
    unavailable, malformed, or duplicate-conflicting facts keep route cost
    explicitly unknown instead of silently contributing zero.
    """

    if not required_edges:
        return 0.0
    values: dict[str, float] = {}
    conflicting: set[str] = set()
    for fact in snapshot.facts:
        if (
            fact.subject_type != "ROUTE_EDGE"
            or fact.fact_type != "ROUTE_TIME"
            or fact.subject_id not in required_edges
            or fact.freshness_status != EvidenceFreshness.FRESH
            or not isinstance(fact.value, dict)
        ):
            continue
        raw_duration = fact.value.get("duration_minutes")
        if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)) or raw_duration < 0:
            continue
        duration = float(raw_duration)
        previous = values.get(fact.subject_id)
        if previous is not None and previous != duration:
            conflicting.add(fact.subject_id)
        values[fact.subject_id] = duration
    if conflicting or set(values) != required_edges:
        return None
    return sum(values.values())


def route_cost_delta_minutes(
    source_revision: ItineraryRevision,
    candidate_revision: ItineraryRevision,
    source_snapshot: EvidenceSnapshot,
    candidate_snapshot: EvidenceSnapshot,
) -> float | None:
    """Compute candidate minus source route minutes from complete evidence."""

    source_total = _route_minutes(source_snapshot, _required_route_edges(source_revision))
    candidate_total = _route_minutes(candidate_snapshot, _required_route_edges(candidate_revision))
    if source_total is None or candidate_total is None:
        return None
    return candidate_total - source_total


def _operation_change_key(operations: list[RepairOperation]) -> tuple[int, int, float, float]:
    deletion_count = sum(item.operation == RepairOperationType.REMOVE_STOP for item in operations)
    move_day_count = sum(item.operation == RepairOperationType.MOVE_TO_DAY for item in operations)
    time_shift_minutes = 0.0
    for item in operations:
        if item.operation != RepairOperationType.ADJUST_TIME:
            continue
        raw_shift = item.payload.get("shift_minutes")
        if isinstance(raw_shift, (int, float)) and not isinstance(raw_shift, bool):
            time_shift_minutes += abs(float(raw_shift))
        else:
            # Older operations describe absolute clocks. edit_cost remains a
            # deterministic fallback until both before/after clocks are present.
            time_shift_minutes += 1.0
    return deletion_count, move_day_count, time_shift_minutes, edit_cost(operations)


def repair_option_sort_key(option: RepairOption) -> tuple:
    """Stable approximation of Final 1.0 section 10.2 lexicographic goals.

    Hard invariant/new-risk candidates have already been rejected by search.
    Among feasible options we first minimize remaining risk and itinerary edits,
    then prefer known route deltas over UNKNOWN and minimize added route time.
    The canonical operation digest is the final deterministic tie breaker.
    """

    route_unknown = option.route_cost_delta is None
    route_delta = option.route_cost_delta if option.route_cost_delta is not None else 0.0
    operation_canonical = canonical_json([
        item.model_dump(mode="json") for item in option.operations
    ])
    return (
        option.risk_cost,
        option.new_unknown_count,
        *_operation_change_key(option.operations),
        route_unknown,
        route_delta,
        len(option.affected_member_ids),
        operation_canonical,
    )


def unresolved_risk_cost(report: AuditReport) -> float:
    severity_weight = {
        AuditSeverity.BLOCKER: 100.0,
        AuditSeverity.HIGH: 30.0,
        AuditSeverity.MEDIUM: 8.0,
        AuditSeverity.LOW: 2.0,
        AuditSeverity.INFO: 0.0,
    }
    status_weight = {
        AuditStatus.VIOLATED: 1.0,
        AuditStatus.UNKNOWN: 0.6,
        AuditStatus.SATISFIED: 0.0,
    }
    return sum(severity_weight[item.severity] * status_weight[item.status] for item in report.findings)


def new_unknown_count(source: AuditReport, candidate: AuditReport) -> int:
    source_keys = {
        _risk_identity(item)
        for item in source.findings
        if item.status == AuditStatus.UNKNOWN
    }
    return sum(
        1
        for item in candidate.findings
        if item.status == AuditStatus.UNKNOWN
        and _risk_identity(item) not in source_keys
    )


def introduces_new_high_violation(source: AuditReport, candidate: AuditReport) -> bool:
    source_keys = {
        _risk_identity(item)
        for item in source.findings
        if item.status == AuditStatus.VIOLATED and item.severity in {AuditSeverity.BLOCKER, AuditSeverity.HIGH}
    }
    return any(
        item.status == AuditStatus.VIOLATED
        and item.severity in {AuditSeverity.BLOCKER, AuditSeverity.HIGH}
        and _risk_identity(item) not in source_keys
        for item in candidate.findings
    )


def assert_repair_postcheck_safe(
    source: AuditReport,
    candidate: AuditReport,
    *,
    targeted_finding_ids: Iterable[str] = (),
) -> None:
    """Reject an apply before mutation when its immutable postcheck is unsafe."""

    reasons: list[str] = []
    if introduces_new_high_violation(source, candidate):
        reasons.append("NEW_BLOCKER_OR_HIGH")
    unknown_count = new_unknown_count(source, candidate)
    if unknown_count:
        reasons.append("NEW_UNKNOWN")
    targeted_ids = set(targeted_finding_ids)
    targeted = {
        _risk_identity(finding)
        for finding in source.findings
        if finding.finding_id in targeted_ids
    }
    if any(
        finding.status != AuditStatus.SATISFIED
        and _risk_identity(finding) in targeted
        for finding in candidate.findings
    ):
        reasons.append("TARGET_FINDING_UNRESOLVED")
    if reasons:
        raise RepairUnsafePostcheckError(
            "repair postcheck introduced a new serious or unknown finding",
            context={
                "source_report_id": source.report_id,
                "postcheck_report_id": candidate.report_id,
                "reasons": reasons,
                "new_unknown_count": unknown_count,
            },
        )
