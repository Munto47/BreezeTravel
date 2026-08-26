from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.audit.models import (
    AuditDependency,
    AuditFinding,
    AuditSeverity,
    AuditStatus,
    EvidenceFact,
    EvidenceFreshness,
)
from app.itineraries.models import CommitmentKind, ItineraryStop


_MODE_ALIASES = {
    "car": "driving",
    "drive": "driving",
    "driving": "driving",
    "walk": "walking",
    "walking": "walking",
    "bus": "transit",
    "public_transport": "transit",
    "transit": "transit",
}
_RAIL_TERMINAL = re.compile(r"(?:高铁|铁路|火车|动车|东站|西站|南站|北站|虹桥站)")


def _minutes(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _mode(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _MODE_ALIASES.get(normalized, normalized)


def _route_facts(context: Any, edge_id: str) -> list[EvidenceFact]:
    return [
        fact
        for fact in context.evidence_snapshot.facts
        if fact.subject_type == "ROUTE_EDGE"
        and fact.subject_id == edge_id
        and fact.fact_type == "ROUTE_TIME"
    ]


@dataclass(frozen=True)
class _RouteAssessment:
    duration_minutes: int | None
    facts: tuple[EvidenceFact, ...]
    unknown_reason: str | None = None
    observed_mode: str | None = None


def _assess_route(context: Any, left: ItineraryStop, right: ItineraryStop) -> _RouteAssessment:
    edge_id = f"{left.stop_id}->{right.stop_id}"
    facts = tuple(_route_facts(context, edge_id))
    if not facts:
        return _RouteAssessment(None, facts, "ROUTE_EDGE_FACT_MISSING")
    if any(fact.freshness_status == EvidenceFreshness.CONFLICTING for fact in facts):
        return _RouteAssessment(None, facts, "ROUTE_EDGE_FACT_CONFLICTING")

    fresh = [
        fact
        for fact in facts
        if fact.freshness_status == EvidenceFreshness.FRESH
        and (fact.valid_from is None or fact.valid_from <= context.now)
        and (fact.valid_until is None or fact.valid_until >= context.now)
    ]
    if not fresh:
        return _RouteAssessment(None, facts, "ROUTE_EDGE_FACT_NOT_FRESH")

    values: set[tuple[str, int]] = set()
    for fact in fresh:
        if not isinstance(fact.value, dict):
            return _RouteAssessment(None, facts, "ROUTE_EDGE_VALUE_INVALID")
        raw_duration = fact.value.get("duration_minutes")
        if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)) or raw_duration < 0:
            return _RouteAssessment(None, facts, "ROUTE_EDGE_DURATION_INVALID")
        observed_mode = _mode(fact.value.get("mode"))
        if not observed_mode:
            return _RouteAssessment(None, facts, "ROUTE_EDGE_MODE_MISSING")
        values.add((observed_mode, int(raw_duration)))
    if len(values) != 1:
        return _RouteAssessment(None, facts, "ROUTE_EDGE_FRESH_VALUES_CONFLICT")

    observed_mode, duration = values.pop()
    expected_mode = _mode(left.transport_to_next.mode if left.transport_to_next else "driving")
    if observed_mode != expected_mode:
        return _RouteAssessment(None, facts, "ROUTE_EDGE_MODE_MISMATCH", observed_mode)
    return _RouteAssessment(duration, facts, observed_mode=observed_mode)


def _is_commitment_target(stop: ItineraryStop) -> bool:
    if stop.commitment_kind == CommitmentKind.ARRIVAL:
        return False
    return bool(
        stop.fixed_commitment
        or stop.commitment_kind in {CommitmentKind.FIXED_VISIT, CommitmentKind.RETURN_DEPARTURE}
    )


def _route_input(
    *,
    left: ItineraryStop,
    right: ItineraryStop,
    available_minutes: int | None,
    assessment: _RouteAssessment,
) -> dict[str, Any]:
    return {
        "edge_id": f"{left.stop_id}->{right.stop_id}",
        "from_stop_id": left.stop_id,
        "to_stop_id": right.stop_id,
        "available_minutes": available_minutes,
        "route_duration_minutes": assessment.duration_minutes,
        "expected_mode": _mode(left.transport_to_next.mode if left.transport_to_next else "driving"),
        "observed_mode": assessment.observed_mode,
        "evidence_freshness": [fact.freshness_status.value for fact in assessment.facts],
        "evidence_unknown_reason": assessment.unknown_reason,
    }


class RouteGapRule:
    """Check ordinary adjacent stops using only their exact fresh route fact."""

    rule_id = "audit.route_gap"
    rule_version = "1.0.0"
    dependencies = (
        AuditDependency.DAY_ORDER,
        AuditDependency.TIME_WINDOW,
        AuditDependency.ROUTE_EDGE,
        AuditDependency.EVIDENCE_FRESHNESS,
    )

    def evaluate(self, context: Any) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for day in context.revision.days:
            for left, right in zip(day.stops, day.stops[1:]):
                # Fixed appointments and return departures have stronger
                # semantics and are owned by CommitmentFeasibilityRule.
                if _is_commitment_target(right):
                    continue

                left_end = _minutes(left.end_time)
                right_start = _minutes(right.start_time)
                # TimeChainRule owns overlaps and reversed time. Emitting a
                # second route finding would describe the same defect twice.
                if left_end is not None and right_start is not None and right_start < left_end:
                    continue

                assessment = _assess_route(context, left, right)
                available = None if left_end is None or right_start is None else right_start - left_end
                common = {
                    "finding_id": str(uuid4()),
                    "rule_id": self.rule_id,
                    "rule_version": self.rule_version,
                    "affected_days": [day.day_index],
                    "affected_stop_ids": [left.stop_id, right.stop_id],
                    "evidence_fact_ids": [fact.fact_id for fact in assessment.facts],
                    "input_values": _route_input(
                        left=left,
                        right=right,
                        available_minutes=available,
                        assessment=assessment,
                    ),
                }
                if available is None:
                    findings.append(AuditFinding(
                        **common,
                        status=AuditStatus.UNKNOWN,
                        severity=AuditSeverity.HIGH,
                        reason_code="ROUTE_GAP_TIME_UNKNOWN",
                        message=f"{left.raw_name or left.place_id} 到 {right.raw_name or right.place_id} 缺少可计算空档的时间",
                        confirmation_action="请补充相邻地点的结束和开始时间后重新审计",
                    ))
                    continue
                if assessment.duration_minutes is None:
                    findings.append(AuditFinding(
                        **common,
                        status=AuditStatus.UNKNOWN,
                        severity=AuditSeverity.HIGH,
                        reason_code="ROUTE_GAP_EVIDENCE_UNKNOWN",
                        message=f"{left.raw_name or left.place_id} 到 {right.raw_name or right.place_id} 缺少同交通方式的最新路线事实",
                        confirmation_action="请刷新该相邻路线的交通时间后重新审计",
                    ))
                    continue

                feasible = available >= assessment.duration_minutes
                findings.append(AuditFinding(
                    **common,
                    status=AuditStatus.SATISFIED if feasible else AuditStatus.VIOLATED,
                    severity=AuditSeverity.INFO if feasible else AuditSeverity.HIGH,
                    reason_code="ROUTE_GAP_SUFFICIENT" if feasible else "ROUTE_GAP_INSUFFICIENT",
                    message=(
                        f"相邻地点空档 {available} 分钟，可覆盖 {assessment.duration_minutes} 分钟路线"
                        if feasible
                        else f"相邻地点仅有 {available} 分钟空档，少于路线所需 {assessment.duration_minutes} 分钟"
                    ),
                    repairable=not feasible,
                    confirmation_action=None if feasible else "请调整前后活动时间或更换地点",
                ))
        return findings


def _return_buffer(stop: ItineraryStop) -> tuple[int | None, str | None]:
    text = " ".join(part for part in (stop.raw_name, stop.category, stop.notes) if part).lower()
    if any(token in text for token in ("机场", "航班", "airport", "flight")):
        return 60, "FLIGHT"
    if _RAIL_TERMINAL.search(text):
        return 30, "RAIL"
    return None, None


class CommitmentFeasibilityRule:
    """Protect fixed appointments and departures with conservative route buffers."""

    rule_id = "audit.commitment_feasibility"
    rule_version = "1.0.0"
    dependencies = (
        AuditDependency.DAY_ORDER,
        AuditDependency.TIME_WINDOW,
        AuditDependency.ROUTE_EDGE,
        AuditDependency.EVIDENCE_FRESHNESS,
    )

    def evaluate(self, context: Any) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for day in context.revision.days:
            for index, stop in enumerate(day.stops):
                if not _is_commitment_target(stop):
                    continue
                is_return = stop.commitment_kind == CommitmentKind.RETURN_DEPARTURE
                prefix = "RETURN_DEPARTURE" if is_return else "FIXED_COMMITMENT"

                if not stop.fixed_commitment or not stop.locked:
                    findings.append(AuditFinding(
                        finding_id=str(uuid4()),
                        rule_id=self.rule_id,
                        rule_version=self.rule_version,
                        status=AuditStatus.VIOLATED,
                        severity=AuditSeverity.BLOCKER,
                        reason_code="COMMITMENT_LOCK_INCOMPLETE",
                        message=f"{stop.raw_name or stop.place_id} 的固定承诺没有同时标记为 fixed 和 locked",
                        input_values={
                            "stop_id": stop.stop_id,
                            "commitment_kind": stop.commitment_kind.value if stop.commitment_kind else None,
                            "fixed_commitment": stop.fixed_commitment,
                            "locked": stop.locked,
                        },
                        affected_days=[day.day_index],
                        affected_stop_ids=[stop.stop_id],
                        confirmation_action="请恢复固定承诺的锁定状态后重新审计",
                    ))
                    continue

                start = _minutes(stop.start_time)
                if start is None:
                    findings.append(AuditFinding(
                        finding_id=str(uuid4()),
                        rule_id=self.rule_id,
                        rule_version=self.rule_version,
                        status=AuditStatus.UNKNOWN,
                        severity=AuditSeverity.BLOCKER,
                        reason_code=f"{prefix}_TIME_UNKNOWN",
                        message=f"{stop.raw_name or stop.place_id} 缺少可核验的固定开始时间",
                        input_values={"stop_id": stop.stop_id, "start_time": stop.start_time},
                        affected_days=[day.day_index],
                        affected_stop_ids=[stop.stop_id],
                        confirmation_action="请确认固定预约或返程时间后重新审计",
                    ))
                    continue

                buffer_minutes = 0
                terminal_type = None
                if is_return:
                    buffer, terminal_type = _return_buffer(stop)
                    if buffer is None:
                        findings.append(AuditFinding(
                            finding_id=str(uuid4()),
                            rule_id=self.rule_id,
                            rule_version=self.rule_version,
                            status=AuditStatus.UNKNOWN,
                            severity=AuditSeverity.BLOCKER,
                            reason_code="RETURN_TRANSPORT_TYPE_UNKNOWN",
                            message=f"无法识别 {stop.raw_name or stop.place_id} 的铁路或航班返程类型，不能套用安全缓冲",
                            input_values={"stop_id": stop.stop_id, "raw_name": stop.raw_name, "category": stop.category},
                            affected_days=[day.day_index],
                            affected_stop_ids=[stop.stop_id],
                            confirmation_action="请确认返程是铁路还是航班后重新审计",
                        ))
                        continue
                    buffer_minutes = buffer

                if index == 0:
                    findings.append(AuditFinding(
                        finding_id=str(uuid4()),
                        rule_id=self.rule_id,
                        rule_version=self.rule_version,
                        status=AuditStatus.SATISFIED,
                        severity=AuditSeverity.INFO,
                        reason_code=f"{prefix}_FEASIBLE",
                        message=f"{stop.raw_name or stop.place_id} 是当天首项，不存在行程内前序路线冲突",
                        input_values={"stop_id": stop.stop_id, "route_required": False},
                        affected_days=[day.day_index],
                        affected_stop_ids=[stop.stop_id],
                    ))
                    continue

                left = day.stops[index - 1]
                left_end = _minutes(left.end_time)
                # TimeChainRule owns overlap/reversed-time findings.
                if left_end is not None and start < left_end:
                    continue

                assessment = _assess_route(context, left, stop)
                available = None if left_end is None else start - left_end
                input_values = {
                    **_route_input(
                        left=left,
                        right=stop,
                        available_minutes=available,
                        assessment=assessment,
                    ),
                    "commitment_kind": stop.commitment_kind.value if stop.commitment_kind else None,
                    "terminal_type": terminal_type,
                    "buffer_policy_version": "commitment-buffer-v1",
                    "required_buffer_minutes": buffer_minutes,
                }
                common = {
                    "finding_id": str(uuid4()),
                    "rule_id": self.rule_id,
                    "rule_version": self.rule_version,
                    "affected_days": [day.day_index],
                    "affected_stop_ids": [left.stop_id, stop.stop_id],
                    "evidence_fact_ids": [fact.fact_id for fact in assessment.facts],
                    "input_values": input_values,
                }
                if available is None:
                    findings.append(AuditFinding(
                        **common,
                        status=AuditStatus.UNKNOWN,
                        severity=AuditSeverity.BLOCKER,
                        reason_code=f"{prefix}_TIME_UNKNOWN",
                        message=f"{left.raw_name or left.place_id} 缺少结束时间，无法核验固定承诺前的空档",
                        confirmation_action="请补充前序活动结束时间后重新审计",
                    ))
                    continue
                if assessment.duration_minutes is None:
                    findings.append(AuditFinding(
                        **common,
                        status=AuditStatus.UNKNOWN,
                        severity=AuditSeverity.BLOCKER,
                        reason_code=f"{prefix}_ROUTE_UNKNOWN",
                        message="固定承诺前缺少同交通方式的最新路线事实，不能判定可达",
                        confirmation_action="请刷新固定承诺前序路线后重新审计",
                    ))
                    continue

                required = assessment.duration_minutes + buffer_minutes
                input_values["required_total_minutes"] = required
                feasible = available >= required
                findings.append(AuditFinding(
                    **common,
                    status=AuditStatus.SATISFIED if feasible else AuditStatus.VIOLATED,
                    severity=AuditSeverity.INFO if feasible else AuditSeverity.BLOCKER,
                    reason_code=f"{prefix}_FEASIBLE" if feasible else f"{prefix}_CONFLICT",
                    message=(
                        f"固定承诺前有 {available} 分钟，可覆盖路线和 {buffer_minutes} 分钟缓冲"
                        if feasible
                        else f"固定承诺前仅有 {available} 分钟，路线和安全缓冲共需 {required} 分钟"
                    ),
                    repairable=not feasible,
                    confirmation_action=None if feasible else "请提前结束前序活动、调整路线或更换前序地点",
                ))
        return findings
