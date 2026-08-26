"""Authoritative audit rules for confirmed, hard member constraints.

The old collaborative vote checks are useful signals, but they cannot waive a
member's explicitly confirmed hard requirement.  These rules read the
append-only member constraint revision chosen by :class:`AuditApplicationService`
and return a three-state result whenever the itinerary/evidence is incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Iterable
from uuid import uuid4

from app.audit.models import (
    AuditDependency,
    AuditFinding,
    AuditSeverity,
    AuditStatus,
    EvidenceFreshness,
)
from app.audit.registry import AuditRuleContext
from app.members.models import (
    ConstraintConfirmationStatus,
    ConstraintHardness,
    MemberConstraint,
)


_WALKING_TYPES = {"walking", "walk", "步行"}
_HOTEL_CATEGORIES = {"hotel", "酒店", "住宿"}
_FOOD_CATEGORIES = {"food", "餐饮", "餐厅", "restaurant"}
_NAP_WINDOW_START = 12 * 60
_NAP_WINDOW_END = 15 * 60
_MIN_NAP_MINUTES = 60


def _minutes(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        return None
    return parsed.hour * 60 + parsed.minute


def _normalise(value: object) -> str:
    return "".join(str(value or "").lower().split())


def _member_input(constraint: MemberConstraint) -> dict[str, object]:
    return {
        "constraint_id": constraint.constraint_id,
        "owner_member_id": constraint.owner_member_id,
        "type": constraint.type,
        "operator": constraint.operator,
        "value": constraint.value,
        "hardness": constraint.hardness.value,
        "confirmation_status": constraint.confirmation_status.value,
        "revision": constraint.revision,
    }


@dataclass(frozen=True)
class _Assessment:
    status: AuditStatus
    reason_code: str
    message: str
    day_index: int | None = None
    stop_ids: tuple[str, ...] = ()
    evidence_fact_ids: tuple[str, ...] = ()
    details: dict[str, object] | None = None


class MemberConstraintAuditRule:
    """Evaluate the small, currently verifiable HARD member-constraint set."""

    rule_id = "member.confirmed_hard_constraints"
    rule_version = "1.0.0"
    dependencies = (
        AuditDependency.MEMBER_CONSTRAINT,
        AuditDependency.DAY_ORDER,
        AuditDependency.TIME_WINDOW,
        AuditDependency.ROUTE_EDGE,
        AuditDependency.EVIDENCE_FRESHNESS,
    )

    def evaluate(self, context: AuditRuleContext) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for constraint in context.member_constraints:
            if constraint.hardness != ConstraintHardness.HARD:
                # Soft member input is intentionally present in the audit
                # context but cannot turn a report into a hard failure.
                continue
            if constraint.confirmation_status != ConstraintConfirmationStatus.CONFIRMED:
                assessments = [_Assessment(
                    AuditStatus.UNKNOWN,
                    "MEMBER_HARD_CONSTRAINT_UNCONFIRMED",
                    "成员硬约束缺少确认，不能据此推断行程合规。",
                )]
            else:
                assessments = self._evaluate_confirmed(constraint, context)
            findings.extend(self._finding(constraint, item) for item in assessments)
        return findings

    def _evaluate_confirmed(
        self,
        constraint: MemberConstraint,
        context: AuditRuleContext,
    ) -> list[_Assessment]:
        kind = _normalise(constraint.type)
        if kind == "walking_limit_minutes":
            return self._walking_limit(constraint, context)
        if kind == "requires_nap":
            return self._requires_nap(constraint, context)
        if kind == "dietary_restrictions":
            return self._dietary_restrictions(constraint, context)
        if kind == "latest_return_time":
            return self._latest_return_time(constraint, context)
        return [_Assessment(
            AuditStatus.UNKNOWN,
            "MEMBER_CONSTRAINT_TYPE_UNSUPPORTED",
            f"成员硬约束 {constraint.type} 尚无可验证规则，不能把它当作已满足。",
        )]

    def _walking_limit(
        self,
        constraint: MemberConstraint,
        context: AuditRuleContext,
    ) -> list[_Assessment]:
        try:
            limit = int(constraint.value)
        except (TypeError, ValueError):
            return [_Assessment(
                AuditStatus.UNKNOWN,
                "WALKING_LIMIT_INVALID",
                "步行上限不是可计算的分钟数，不能验证。",
            )]
        if limit < 0:
            return [_Assessment(
                AuditStatus.UNKNOWN,
                "WALKING_LIMIT_INVALID",
                "步行上限不能为负数，不能验证。",
            )]

        assessments: list[_Assessment] = []
        for day in context.revision.days:
            edges = list(zip(day.stops, day.stops[1:]))
            missing = [left.stop_id for left, _ in edges if left.transport_to_next is None or left.transport_to_next.duration_minutes is None]
            if missing:
                assessments.append(_Assessment(
                    AuditStatus.UNKNOWN,
                    "WALKING_ROUTE_TIME_MISSING",
                    "该日缺少至少一段相邻地点的路线时长，无法核验步行上限。",
                    day.day_index,
                    tuple(missing),
                    details={"walking_limit_minutes": limit, "missing_route_from_stop_ids": missing},
                ))
                continue
            walking_minutes = sum(
                left.transport_to_next.duration_minutes or 0
                for left, _ in edges
                if _normalise(left.transport_to_next.mode) in _WALKING_TYPES
            )
            status = AuditStatus.SATISFIED if walking_minutes <= limit else AuditStatus.VIOLATED
            assessments.append(_Assessment(
                status,
                "WALKING_LIMIT_MET" if status == AuditStatus.SATISFIED else "WALKING_LIMIT_EXCEEDED",
                (
                    f"该日已知步行 {walking_minutes} 分钟，未超过成员上限 {limit} 分钟。"
                    if status == AuditStatus.SATISFIED
                    else f"该日已知步行 {walking_minutes} 分钟，超过成员上限 {limit} 分钟。"
                ),
                day.day_index,
                tuple(left.stop_id for left, _ in edges if _normalise(left.transport_to_next.mode) in _WALKING_TYPES),
                details={"walking_limit_minutes": limit, "walking_minutes": walking_minutes},
            ))
        return assessments or [_Assessment(
            AuditStatus.UNKNOWN,
            "WALKING_ITINERARY_MISSING",
            "行程中没有可核验的日期，无法验证步行上限。",
        )]

    def _requires_nap(
        self,
        constraint: MemberConstraint,
        context: AuditRuleContext,
    ) -> list[_Assessment]:
        if constraint.value is not True:
            return [_Assessment(
                AuditStatus.SATISFIED,
                "NAP_NOT_REQUIRED",
                "该成员未要求午休时段。",
            )]
        assessments: list[_Assessment] = []
        for day in context.revision.days:
            intervals: list[tuple[int, int, str]] = []
            invalid_stops: list[str] = []
            for stop in day.stops:
                start, end = _minutes(stop.start_time), _minutes(stop.end_time)
                if start is None or end is None or end < start:
                    invalid_stops.append(stop.stop_id)
                    continue
                intervals.append((start, end, stop.stop_id))
            if invalid_stops:
                assessments.append(_Assessment(
                    AuditStatus.UNKNOWN,
                    "NAP_TIME_DATA_MISSING",
                    "该日存在缺失或无效的活动时间，无法确认午休窗口。",
                    day.day_index,
                    tuple(invalid_stops),
                ))
                continue
            largest_gap = self._largest_nap_gap(intervals)
            status = AuditStatus.SATISFIED if largest_gap >= _MIN_NAP_MINUTES else AuditStatus.VIOLATED
            assessments.append(_Assessment(
                status,
                "NAP_WINDOW_AVAILABLE" if status == AuditStatus.SATISFIED else "NAP_WINDOW_MISSING",
                (
                    f"该日午休窗口内有 {largest_gap} 分钟连续空档。"
                    if status == AuditStatus.SATISFIED
                    else f"该日午休窗口内最长连续空档仅 {largest_gap} 分钟，不满足至少 {_MIN_NAP_MINUTES} 分钟午休。"
                ),
                day.day_index,
                tuple(stop_id for _, _, stop_id in intervals),
                details={"nap_window": "12:00-15:00", "largest_free_minutes": largest_gap},
            ))
        return assessments or [_Assessment(
            AuditStatus.UNKNOWN,
            "NAP_ITINERARY_MISSING",
            "行程中没有可核验的日期，无法确认午休窗口。",
        )]

    @staticmethod
    def _largest_nap_gap(intervals: Iterable[tuple[int, int, str]]) -> int:
        cursor = _NAP_WINDOW_START
        largest = 0
        for start, end, _ in sorted(intervals):
            if end <= _NAP_WINDOW_START or start >= _NAP_WINDOW_END:
                continue
            start, end = max(start, _NAP_WINDOW_START), min(end, _NAP_WINDOW_END)
            if start > cursor:
                largest = max(largest, start - cursor)
            cursor = max(cursor, end)
        return max(largest, _NAP_WINDOW_END - cursor)

    def _dietary_restrictions(
        self,
        constraint: MemberConstraint,
        context: AuditRuleContext,
    ) -> list[_Assessment]:
        restrictions = {_normalise(item) for item in constraint.value or [] if _normalise(item)}
        if not restrictions:
            return [_Assessment(
                AuditStatus.UNKNOWN,
                "DIETARY_RESTRICTIONS_INVALID",
                "饮食限制为空或格式无效，不能验证。",
            )]
        food_stops = [
            stop
            for day in context.revision.days
            for stop in day.stops
            if _normalise(stop.category) in _FOOD_CATEGORIES
        ]
        if not food_stops:
            return [_Assessment(
                AuditStatus.UNKNOWN,
                "DIETARY_FOOD_STOP_MISSING",
                "行程没有可核验的餐饮地点，不能证明饮食限制会被满足。",
            )]
        facts_by_place: dict[str, list] = {}
        for fact in context.evidence_snapshot.facts:
            if fact.subject_type == "PLACE":
                facts_by_place.setdefault(fact.subject_id, []).append(fact)
        assessments: list[_Assessment] = []
        for stop in food_stops:
            facts = [
                fact for fact in facts_by_place.get(stop.place_id, [])
                if fact.fact_type == "DIETARY_SUPPORT" and fact.freshness_status == EvidenceFreshness.FRESH
            ]
            if not facts:
                assessments.append(_Assessment(
                    AuditStatus.UNKNOWN,
                    "DIETARY_EVIDENCE_MISSING",
                    "该餐饮地点没有新鲜的饮食限制支持证据，不能推断菜单合规。",
                    stop.day_index,
                    (stop.stop_id,),
                ))
                continue
            support, evidence_ids, explicit_false = self._dietary_support(facts)
            if restrictions <= support:
                assessments.append(_Assessment(
                    AuditStatus.SATISFIED,
                    "DIETARY_RESTRICTIONS_SUPPORTED",
                    "该餐饮地点的已验证限制标签覆盖成员饮食限制。",
                    stop.day_index,
                    (stop.stop_id,),
                    tuple(evidence_ids),
                    {"required_restrictions": sorted(restrictions), "supported_restrictions": sorted(support)},
                ))
            elif explicit_false:
                assessments.append(_Assessment(
                    AuditStatus.VIOLATED,
                    "DIETARY_RESTRICTIONS_UNSUPPORTED",
                    "该餐饮地点的证据明确表示无法满足成员饮食限制。",
                    stop.day_index,
                    (stop.stop_id,),
                    tuple(evidence_ids),
                    {"required_restrictions": sorted(restrictions), "supported_restrictions": sorted(support)},
                ))
            else:
                assessments.append(_Assessment(
                    AuditStatus.UNKNOWN,
                    "DIETARY_RESTRICTIONS_INCOMPLETE",
                    "该餐饮地点的已验证标签不足以覆盖全部饮食限制，不能按满足处理。",
                    stop.day_index,
                    (stop.stop_id,),
                    tuple(evidence_ids),
                    {"required_restrictions": sorted(restrictions), "supported_restrictions": sorted(support)},
                ))
        return assessments

    @staticmethod
    def _dietary_support(facts: Iterable) -> tuple[set[str], list[str], bool]:
        support: set[str] = set()
        ids: list[str] = []
        explicit_false = False
        for fact in facts:
            ids.append(fact.fact_id)
            value = fact.value
            if isinstance(value, dict):
                items = value.get("supported_restrictions", value.get("restrictions", []))
                if isinstance(items, list):
                    support.update(_normalise(item) for item in items if _normalise(item))
                explicit_false = explicit_false or value.get("supports") is False
        return support, ids, explicit_false

    def _latest_return_time(
        self,
        constraint: MemberConstraint,
        context: AuditRuleContext,
    ) -> list[_Assessment]:
        deadline = _minutes(str(constraint.value) if constraint.value is not None else None)
        if deadline is None:
            return [_Assessment(
                AuditStatus.UNKNOWN,
                "LATEST_RETURN_TIME_INVALID",
                "最晚返程时间不是 HH:MM，不能验证。",
            )]
        assessments: list[_Assessment] = []
        for day in context.revision.days:
            if not day.stops:
                assessments.append(_Assessment(
                    AuditStatus.UNKNOWN,
                    "LATEST_RETURN_ITINERARY_EMPTY",
                    "该日没有可核验的返程或酒店到达记录。",
                    day.day_index,
                ))
                continue
            last = day.stops[-1]
            if _normalise(last.category) in _HOTEL_CATEGORIES:
                arrival = _minutes(last.start_time)
                if arrival is None:
                    assessments.append(_Assessment(
                        AuditStatus.UNKNOWN,
                        "LATEST_RETURN_ARRIVAL_MISSING",
                        "最后一站是酒店，但没有酒店到达时间。",
                        day.day_index,
                        (last.stop_id,),
                    ))
                    continue
                status = AuditStatus.SATISFIED if arrival <= deadline else AuditStatus.VIOLATED
                assessments.append(_Assessment(
                    status,
                    "LATEST_RETURN_MET" if status == AuditStatus.SATISFIED else "LATEST_RETURN_EXCEEDED",
                    "该日酒店到达时间未超过成员最晚返程时间。" if status == AuditStatus.SATISFIED else "该日酒店到达时间晚于成员最晚返程时间。",
                    day.day_index,
                    (last.stop_id,),
                    details={"latest_return_time": constraint.value, "hotel_arrival_time": last.start_time},
                ))
                continue
            end = _minutes(last.end_time)
            if end is None:
                assessments.append(_Assessment(
                    AuditStatus.UNKNOWN,
                    "LATEST_RETURN_TIME_MISSING",
                    "该日最后一项活动缺少结束时间，无法核验最晚返程。",
                    day.day_index,
                    (last.stop_id,),
                ))
            elif end > deadline:
                assessments.append(_Assessment(
                    AuditStatus.VIOLATED,
                    "LATEST_RETURN_EXCEEDED",
                    "最后一项活动结束已晚于成员最晚返程时间。",
                    day.day_index,
                    (last.stop_id,),
                    details={"latest_return_time": constraint.value, "last_activity_end_time": last.end_time},
                ))
            else:
                assessments.append(_Assessment(
                    AuditStatus.UNKNOWN,
                    "LATEST_RETURN_ROUTE_TO_HOTEL_UNKNOWN",
                    "最后一项活动虽未超时，但缺少回酒店路线和到达事实，不能确认按时返程。",
                    day.day_index,
                    (last.stop_id,),
                    details={"latest_return_time": constraint.value, "last_activity_end_time": last.end_time},
                ))
        return assessments

    @staticmethod
    def _finding(constraint: MemberConstraint, assessment: _Assessment) -> AuditFinding:
        severity = (
            AuditSeverity.BLOCKER
            if assessment.status == AuditStatus.VIOLATED
            else AuditSeverity.HIGH
            if assessment.status == AuditStatus.UNKNOWN
            else AuditSeverity.INFO
        )
        values = _member_input(constraint)
        values.update(assessment.details or {})
        return AuditFinding(
            finding_id=str(uuid4()),
            rule_id=MemberConstraintAuditRule.rule_id,
            rule_version=MemberConstraintAuditRule.rule_version,
            status=assessment.status,
            severity=severity,
            reason_code=assessment.reason_code,
            message=assessment.message,
            input_values=values,
            affected_days=[assessment.day_index] if assessment.day_index is not None else [],
            affected_stop_ids=list(assessment.stop_ids),
            affected_member_ids=[constraint.owner_member_id],
            evidence_fact_ids=list(assessment.evidence_fact_ids),
            repairable=assessment.status != AuditStatus.SATISFIED,
            confirmation_action=(
                "请补充成员约束所需的行程时间、路线或证据后重新审计"
                if assessment.status == AuditStatus.UNKNOWN
                else None
            ),
        )
