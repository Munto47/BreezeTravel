from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.audit.engine import AuditEngine
from app.audit.models import (
    AuditDependency,
    AuditFinding,
    AuditReport,
    AuditRunInput,
    AuditSeverity,
    AuditStatus,
    EvidenceFreshness,
    EvidenceSnapshot,
)
from app.audit.registry import AuditRuleContext, AuditRuleRegistry
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevision,
    ItineraryRevisionContent,
    ItineraryStop,
    ResolutionStatus,
    RevisionSource,
    TripDateRange,
)
from app.schemas.task_spec import DateRange, Travelers, TripTaskSpec
from app.trip_understanding.models import (
    ActivityInsertCommand,
    PublicChangePreview,
    PublicTripCheckItem,
    PublicTripChecksView,
    UserFacingTripResult,
)
from app.trip_understanding.pipeline import canonical_sha256


G03_EVIDENCE_POLICY_VERSION = "g03-evidence-v1"
G03_SYSTEM_USER_ID = "__breezetravel_materializer__"
_DATE_PATTERN = re.compile(r"(20\d{2}-\d{2}-\d{2})")


@dataclass(frozen=True)
class CalendarProfile:
    mode: str
    start: date | None
    end: date | None
    party_size: int
    party_size_source: str

    @property
    def public_calendar(self) -> str:
        if self.start is None or self.end is None:
            return "按 Day 编号安排"
        if self.start == self.end:
            return self.start.isoformat()
        return f"{self.start.isoformat()} 至 {self.end.isoformat()}"


def calendar_profile(
    assumptions: list[dict[str, Any]],
    *,
    day_count: int,
) -> CalendarProfile:
    by_key = {
        str(item.get("key")): item
        for item in assumptions
        if isinstance(item, dict) and item.get("key")
    }
    calendar = by_key.get("calendar", {})
    raw_calendar = str(calendar.get("value", "")).strip()
    dates = _DATE_PATTERN.findall(raw_calendar)
    start: date | None = None
    end: date | None = None
    if dates:
        try:
            start = date.fromisoformat(dates[0])
            end = (
                date.fromisoformat(dates[1])
                if len(dates) > 1
                else start + timedelta(days=max(day_count - 1, 0))
            )
        except ValueError:
            start = end = None
    if start is not None and end is not None and end < start:
        start = end = None

    party = by_key.get("party_size", {})
    raw_party = party.get("value", 2)
    try:
        if isinstance(raw_party, str):
            match = re.search(r"\d+", raw_party)
            parsed_party = int(match.group(0)) if match else 2
        else:
            parsed_party = int(raw_party)
    except (TypeError, ValueError):
        parsed_party = 2
    parsed_party = min(max(parsed_party, 1), 50)
    source = str(party.get("source", "SOFT_ASSUMPTION"))
    party_source = "USER_PROVIDED" if source == "USER_EDIT" else "DEFAULT_TWO"
    return CalendarProfile(
        mode="ABSOLUTE_DATES" if start is not None else "DAY_INDEX_ONLY",
        start=start,
        end=end,
        party_size=parsed_party,
        party_size_source=party_source,
    )


def _stable_internal_id(prefix: str, value: str) -> str:
    return f"{prefix}_{canonical_sha256(value)[:24]}"


def _finding_id(context: AuditRuleContext, rule_id: str, suffix: str) -> str:
    """Keep audit primary keys stable within a trip and unique across trips."""
    return _stable_internal_id(
        "finding",
        (
            f"{context.revision.workspace_id}:{context.revision.itinerary_id}:"
            f"{context.revision.revision}:{rule_id}:{suffix}"
        ),
    )


def _time_window(raw: str | None) -> tuple[str | None, str | None]:
    value = (raw or "").strip()
    if not value:
        return None, None
    exact = re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value)
    if exact:
        return value, None
    if "上午" in value:
        return "09:00", "12:00"
    if "下午" in value:
        return "14:00", "17:00"
    if "晚上" in value or "夜" in value:
        return "19:00", "21:00"
    return None, None


def _category(raw: str) -> str:
    value = raw.strip()
    if value == "用餐安排":
        return "meal_break"
    if "餐" in value:
        return "dining"
    if "住宿" in value or "酒店" in value:
        return "hotel"
    return value or "place"


def build_itinerary_revision(
    *,
    result: UserFacingTripResult,
    bindings: dict[str, dict[str, Any]],
    assumptions: list[dict[str, Any]],
    city: str,
    workspace_id: str,
    itinerary_id: str,
    revision: int,
    parent_revision: int | None,
    source_type: RevisionSource,
    created_at: datetime | None = None,
) -> tuple[ItineraryRevision, CalendarProfile]:
    profile = calendar_profile(assumptions, day_count=len(result.days))
    days: list[ItineraryDay] = []
    for day_offset, public_day in enumerate(result.days):
        stops: list[ItineraryStop] = []
        for order_index, card in enumerate(public_day.activities):
            binding = bindings.get(card.activity_token, {})
            canonical_place_id = binding.get("canonical_place_id")
            raw_status = str(binding.get("resolution_status", "UNRESOLVED"))
            resolved = raw_status == "AUTO_MATCHED" or card.category == "用餐安排"
            start_time, end_time = _time_window(card.time_hint)
            stops.append(
                ItineraryStop(
                    stop_id=_stable_internal_id("stop", card.activity_token),
                    place_id=(
                        str(canonical_place_id)
                        if canonical_place_id
                        else _stable_internal_id("place", card.activity_token)
                    ),
                    day_index=day_offset,
                    order_index=order_index,
                    start_time=start_time,
                    end_time=end_time,
                    raw_name=card.name,
                    source_raw_stop_id=None,
                    resolution_status=(
                        ResolutionStatus.AUTO_MATCHED
                        if resolved
                        else ResolutionStatus.AMBIGUOUS
                    ),
                    category=_category(card.category),
                    notes=card.area_or_address,
                )
            )
        day_date = profile.start + timedelta(days=day_offset) if profile.start else None
        days.append(ItineraryDay(day_index=day_offset, date=day_date, stops=stops))
    content = ItineraryRevisionContent(
        itinerary_id=itinerary_id,
        workspace_id=workspace_id,
        revision=revision,
        parent_revision=parent_revision,
        source_type=source_type,
        city=city.strip() or "目的地待确认",
        date_range=TripDateRange(start=profile.start, end=profile.end),
        days=days,
        change_summary={
            "kind": "G03_MATERIALIZATION" if revision == 1 else "G03_USER_CHANGE",
            "route_provider_calls": 0,
        },
        created_by=G03_SYSTEM_USER_ID,
        created_at=created_at or datetime.now(timezone.utc),
    )
    return with_content_hash(content), profile


def build_task_spec(
    revision: ItineraryRevision,
    profile: CalendarProfile,
    *,
    room_id: str,
) -> TripTaskSpec:
    return TripTaskSpec(
        task_id=f"g03:{revision.workspace_id}",
        room_id=room_id,
        task_revision=1,
        city=revision.city,
        date_range=DateRange(start=profile.start, days=len(revision.days)),
        travelers=Travelers(adults=profile.party_size),
        assumptions=(
            ["未提供真实日历日期，按 Day 编号核验"]
            if profile.mode == "DAY_INDEX_ONLY"
            else []
        ),
    )


class PlaceReadinessRule:
    rule_id = "g03.place_readiness"
    rule_version = "1.0.0"
    dependencies = (AuditDependency.DAY_ORDER, AuditDependency.EVIDENCE_FRESHNESS)

    def evaluate(self, context: AuditRuleContext) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for day in context.revision.days:
            for stop in day.stops:
                if stop.category == "meal_break":
                    continue
                if stop.resolution_status != ResolutionStatus.AUTO_MATCHED:
                    findings.append(
                        AuditFinding(
                            finding_id=_finding_id(
                                context, self.rule_id, stop.stop_id
                            ),
                            rule_id=self.rule_id,
                            rule_version=self.rule_version,
                            status=AuditStatus.UNKNOWN,
                            severity=AuditSeverity.HIGH,
                            reason_code="PLACE_CONFIRMATION_REQUIRED",
                            message=f"{stop.raw_name or '该地点'}仍需确认",
                            affected_days=[day.day_index],
                            affected_stop_ids=[stop.stop_id],
                            repairable=False,
                            confirmation_action="确认正确地点后重新检查",
                        )
                    )
        return findings or [
            AuditFinding(
                finding_id=_finding_id(context, self.rule_id, "satisfied"),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                status=AuditStatus.SATISFIED,
                severity=AuditSeverity.INFO,
                reason_code="PLACE_INPUTS_READY",
                message="地点输入可用于当前检查",
            )
        ]


class MealBreakRule:
    rule_id = "g03.meal_break"
    rule_version = "1.0.0"
    dependencies = (AuditDependency.DAY_ORDER,)

    def evaluate(self, context: AuditRuleContext) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for day in context.revision.days:
            if len(day.stops) < 2:
                continue
            has_meal = any(stop.category in {"meal_break", "dining"} for stop in day.stops)
            if not has_meal:
                findings.append(
                    AuditFinding(
                        finding_id=_finding_id(
                            context, self.rule_id, str(day.day_index)
                        ),
                        rule_id=self.rule_id,
                        rule_version=self.rule_version,
                        status=AuditStatus.VIOLATED,
                        severity=AuditSeverity.MEDIUM,
                        reason_code="MEAL_BREAK_MISSING",
                        message=f"Day {day.day_index + 1} 没有明确用餐停留",
                        input_values={"day_index": day.day_index},
                        affected_days=[day.day_index],
                        affected_stop_ids=[stop.stop_id for stop in day.stops],
                        repairable=True,
                    )
                )
        return findings or [
            AuditFinding(
                finding_id=_finding_id(context, self.rule_id, "satisfied"),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                status=AuditStatus.SATISFIED,
                severity=AuditSeverity.INFO,
                reason_code="MEAL_BREAKS_PRESENT",
                message="需要用餐停留的日期已有安排",
            )
        ]


def _facts(snapshot: EvidenceSnapshot, fact_type: str) -> dict[str, Any]:
    return {
        fact.subject_id: fact
        for fact in snapshot.facts
        if fact.fact_type == fact_type
    }


class RouteAvailabilityRule:
    rule_id = "g03.route_availability"
    rule_version = "1.0.0"
    dependencies = (AuditDependency.ROUTE_EDGE, AuditDependency.EVIDENCE_FRESHNESS)

    def evaluate(self, context: AuditRuleContext) -> list[AuditFinding]:
        route_facts = _facts(context.evidence_snapshot, "ROUTE_MODE_SET")
        findings: list[AuditFinding] = []
        for day in context.revision.days:
            for index, left in enumerate(day.stops[:-1]):
                right = day.stops[index + 1]
                if left.category == "meal_break" or right.category == "meal_break":
                    continue
                subject = f"{left.stop_id}->{right.stop_id}"
                fact = route_facts.get(subject)
                value = dict(fact.value or {}) if fact else {}
                both_unavailable = (
                    not fact
                    or (
                        value.get("walking") != "AVAILABLE"
                        and value.get("transit") != "AVAILABLE"
                    )
                )
                if both_unavailable:
                    findings.append(
                        AuditFinding(
                            finding_id=_finding_id(
                                context, self.rule_id, f"{subject}:unknown"
                            ),
                            rule_id=self.rule_id,
                            rule_version=self.rule_version,
                            status=AuditStatus.UNKNOWN,
                            severity=AuditSeverity.MEDIUM,
                            reason_code="ROUTE_CONFIRMATION_REQUIRED",
                            message=f"{left.raw_name}到{right.raw_name}的路线还需确认",
                            affected_days=[day.day_index],
                            affected_stop_ids=[left.stop_id, right.stop_id],
                            evidence_fact_ids=[fact.fact_id] if fact else [],
                            repairable=False,
                            confirmation_action="刷新路线后重新检查",
                        )
                    )
                    continue
                duration = value.get("selected_duration_minutes")
                if isinstance(duration, int) and duration > 90:
                    findings.append(
                        AuditFinding(
                            finding_id=_finding_id(
                                context, self.rule_id, f"{subject}:long"
                            ),
                            rule_id=self.rule_id,
                            rule_version=self.rule_version,
                            status=AuditStatus.VIOLATED,
                            severity=AuditSeverity.HIGH,
                            reason_code="ROUTE_TOO_LONG",
                            message=f"{left.raw_name}到{right.raw_name}的单程时间过长",
                            affected_days=[day.day_index],
                            affected_stop_ids=[left.stop_id, right.stop_id],
                            evidence_fact_ids=[fact.fact_id],
                            repairable=False,
                        )
                    )
        return findings or [
            AuditFinding(
                finding_id=_finding_id(context, self.rule_id, "satisfied"),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                status=AuditStatus.SATISFIED,
                severity=AuditSeverity.INFO,
                reason_code="ROUTES_USABLE",
                message="当前相邻地点路线可用",
            )
        ]


class CalendarEvidenceRule:
    rule_id = "g03.calendar_evidence"
    rule_version = "1.0.0"
    dependencies = (AuditDependency.TIME_WINDOW, AuditDependency.EVIDENCE_FRESHNESS)

    def evaluate(self, context: AuditRuleContext) -> list[AuditFinding]:
        if context.task_spec.date_range.start is None:
            return [
                AuditFinding(
                    finding_id=_finding_id(context, self.rule_id, "day-index"),
                    rule_id=self.rule_id,
                    rule_version=self.rule_version,
                    status=AuditStatus.SATISFIED,
                    severity=AuditSeverity.INFO,
                    reason_code="DAY_INDEX_HAS_NO_DATE_HARD_CONCLUSION",
                    message="未使用真实日期，不生成天气或日期闭馆硬结论",
                )
            ]
        opening = _facts(context.evidence_snapshot, "OPENING_HOURS")
        findings: list[AuditFinding] = []
        for day in context.revision.days:
            for stop in day.stops:
                if stop.category == "meal_break":
                    continue
                fact = opening.get(stop.place_id)
                if fact is None or fact.freshness_status != EvidenceFreshness.FRESH:
                    findings.append(
                        AuditFinding(
                            finding_id=_finding_id(
                                context, self.rule_id, stop.stop_id
                            ),
                            rule_id=self.rule_id,
                            rule_version=self.rule_version,
                            status=AuditStatus.UNKNOWN,
                            severity=AuditSeverity.MEDIUM,
                            reason_code="OPENING_CONFIRMATION_REQUIRED",
                            message=f"{stop.raw_name}在该日期的开放安排还需确认",
                            affected_days=[day.day_index],
                            affected_stop_ids=[stop.stop_id],
                            evidence_fact_ids=[fact.fact_id] if fact else [],
                            repairable=False,
                            confirmation_action="出发前确认开放和预约安排",
                        )
                    )
        return findings or [
            AuditFinding(
                finding_id=_finding_id(context, self.rule_id, "satisfied"),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                status=AuditStatus.SATISFIED,
                severity=AuditSeverity.INFO,
                reason_code="DATE_FACTS_AVAILABLE",
                message="日期相关事实可用",
            )
        ]


class StayCommuteRule:
    rule_id = "g03.stay_commute"
    rule_version = "1.0.0"
    dependencies = (AuditDependency.HOTEL, AuditDependency.ROUTE_EDGE)

    def evaluate(self, context: AuditRuleContext) -> list[AuditFinding]:
        facts = [
            fact
            for fact in context.evidence_snapshot.facts
            if fact.fact_type == "STAY_COMMUTE"
        ]
        findings: list[AuditFinding] = []
        for fact in facts:
            value = dict(fact.value or {})
            maximum = value.get("max_single_leg_minutes")
            if isinstance(maximum, int) and maximum > 75:
                findings.append(
                    AuditFinding(
                        finding_id=_finding_id(
                            context, self.rule_id, fact.subject_id
                        ),
                        rule_id=self.rule_id,
                        rule_version=self.rule_version,
                        status=AuditStatus.VIOLATED,
                        severity=AuditSeverity.MEDIUM,
                        reason_code="STAY_COMMUTE_LONG",
                        message="当前住宿有一段通勤时间较长",
                        evidence_fact_ids=[fact.fact_id],
                        repairable=False,
                    )
                )
        return findings or [
            AuditFinding(
                finding_id=_finding_id(context, self.rule_id, "satisfied"),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                status=AuditStatus.SATISFIED,
                severity=AuditSeverity.INFO,
                reason_code="STAY_COMMUTE_ACCEPTABLE_OR_NOT_SELECTED",
                message="住宿通勤未发现已证实的硬冲突",
            )
        ]


class ProviderFailureRule:
    rule_id = "g03.provider_failure"
    rule_version = "1.0.0"
    dependencies = (AuditDependency.EVIDENCE_FRESHNESS,)

    def evaluate(self, context: AuditRuleContext) -> list[AuditFinding]:
        return [
            AuditFinding(
                finding_id=_finding_id(
                    context, self.rule_id, f"{failure.provider}:{index}"
                ),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                status=AuditStatus.UNKNOWN,
                severity=AuditSeverity.LOW,
                reason_code="PROVIDER_RESULT_UNAVAILABLE",
                message="有一部分核对信息暂时不可用",
                input_values={"category": failure.error_category},
                repairable=False,
                confirmation_action="稍后重新检查",
            )
            for index, failure in enumerate(context.evidence_snapshot.provider_failures)
        ]


def run_g03_audit(
    *,
    revision: ItineraryRevision,
    profile: CalendarProfile,
    room_id: str,
    snapshot: EvidenceSnapshot,
    supersedes_report_id: str | None = None,
    now: datetime | None = None,
) -> AuditReport:
    task = build_task_spec(revision, profile, room_id=room_id)
    registry = AuditRuleRegistry(
        [
            PlaceReadinessRule(),
            MealBreakRule(),
            RouteAvailabilityRule(),
            CalendarEvidenceRule(),
            StayCommuteRule(),
            ProviderFailureRule(),
        ]
    )
    return AuditEngine(registry).run(
        run_input=AuditRunInput(
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            task_id=task.task_id,
            task_revision=task.task_revision,
        ),
        revision=revision,
        task_spec=task,
        evidence_snapshot=snapshot,
        supersedes_report_id=supersedes_report_id,
        now=now,
    )


_SEVERITY_ORDER = {
    AuditSeverity.BLOCKER: 0,
    AuditSeverity.HIGH: 1,
    AuditSeverity.MEDIUM: 2,
    AuditSeverity.LOW: 3,
    AuditSeverity.INFO: 4,
}
_FRESHNESS_ORDER = {
    EvidenceFreshness.FRESH: 0,
    EvidenceFreshness.STALE: 1,
    EvidenceFreshness.CONFLICTING: 2,
    EvidenceFreshness.UNAVAILABLE: 3,
}


def _friendly(finding: AuditFinding) -> tuple[str, str]:
    day = finding.affected_days[0] + 1 if finding.affected_days else None
    mapping = {
        "PLACE_CONFIRMATION_REQUIRED": (
            "确认地点",
            f"Day {day} 有地点还需要你确认后才能可靠核对。",
        ),
        "MEAL_BREAK_MISSING": (
            "补一个用餐停留",
            f"Day {day} 安排了多处活动，但还没有明确的用餐停留。",
        ),
        "ROUTE_CONFIRMATION_REQUIRED": (
            "确认相邻路线",
            f"Day {day} 有一段步行和公交信息还不完整。",
        ),
        "ROUTE_TOO_LONG": (
            "缩短当天通勤",
            f"Day {day} 有一段已核对的单程时间过长。",
        ),
        "OPENING_CONFIRMATION_REQUIRED": (
            "确认开放安排",
            f"Day {day} 的开放或预约安排还需要出发前确认。",
        ),
        "STAY_COMMUTE_LONG": (
            "留意住宿通勤",
            "当前住宿有一段已核对的通勤时间较长。",
        ),
        "PROVIDER_RESULT_UNAVAILABLE": (
            "稍后再确认",
            "有一部分信息暂时没有得到可靠结果。",
        ),
    }
    return mapping.get(
        finding.reason_code,
        ("检查这项安排", "这项安排还需要进一步确认。"),
    )


def _label(finding: AuditFinding) -> str:
    if finding.status == AuditStatus.UNKNOWN:
        return "需要确认"
    if finding.status == AuditStatus.VIOLATED and finding.severity in {
        AuditSeverity.BLOCKER,
        AuditSeverity.HIGH,
    }:
        return "必须调整"
    return "可以更好"


def public_checks(
    report: AuditReport,
    snapshot: EvidenceSnapshot,
    *,
    check_tokens: dict[str, str],
) -> PublicTripChecksView:
    freshness = {fact.fact_id: fact.freshness_status for fact in snapshot.facts}

    def sort_key(finding: AuditFinding) -> tuple[Any, ...]:
        certainty = 0 if finding.status == AuditStatus.VIOLATED else 1
        fact_freshness = min(
            (_FRESHNESS_ORDER[freshness[fact_id]] for fact_id in finding.evidence_fact_ids if fact_id in freshness),
            default=2,
        )
        impact = len(set(finding.affected_days)) + len(set(finding.affected_stop_ids))
        return (
            _SEVERITY_ORDER[finding.severity],
            certainty,
            fact_freshness,
            0 if finding.repairable else 1,
            -impact,
            finding.rule_id,
            finding.reason_code,
            tuple(finding.affected_days),
            tuple(finding.affected_stop_ids),
        )

    unresolved = sorted(
        (
            finding
            for finding in report.findings
            if finding.status in {AuditStatus.VIOLATED, AuditStatus.UNKNOWN}
        ),
        key=sort_key,
    )
    selected = unresolved[:3]
    items: list[PublicTripCheckItem] = []
    for finding in selected:
        token = check_tokens.get(finding.finding_id)
        if token is None:
            continue
        title, message = _friendly(finding)
        items.append(
            PublicTripCheckItem(
                check_token=token,
                label=_label(finding),
                title=title,
                message=message,
                affected_days=[
                    f"Day {day_index + 1}" for day_index in sorted(set(finding.affected_days))
                ],
                can_preview=finding.repairable,
            )
        )
    total_must = sum(_label(item) == "必须调整" for item in unresolved)
    visible_must = sum(item.label == "必须调整" for item in items)
    needs_confirmation = any(
        finding.status == AuditStatus.UNKNOWN
        or (
            finding.status == AuditStatus.VIOLATED
            and finding.severity in {AuditSeverity.BLOCKER, AuditSeverity.HIGH}
        )
        for finding in unresolved
    )
    return PublicTripChecksView(
        status="STILL_NEEDS_CONFIRMATION" if needs_confirmation else "READY",
        message=(
            "还有内容需要确认，已先列出最值得处理的三项"
            if unresolved
            else "当前没有需要优先处理的问题"
        ),
        items=items,
        remaining_must_adjust=max(total_must - visible_must, 0),
        available_actions=["PREVIEW_CHANGE"] if any(item.can_preview for item in items) else [],
    )


def command_for_finding(finding: AuditFinding) -> ActivityInsertCommand:
    if finding.reason_code != "MEAL_BREAK_MISSING" or not finding.affected_days:
        raise ValueError("this check does not have a safe automatic preview")
    day_index = finding.affected_days[0] + 1
    return ActivityInsertCommand(
        command_type="ACTIVITY_INSERT",
        day_index=day_index,
        position=1,
        name="午餐时间",
        category="用餐安排",
        area_or_address="当天活动附近再选择",
        time_hint="12:30",
    )


def preview_for_finding(
    finding: AuditFinding,
    *,
    change_token: str,
) -> PublicChangePreview:
    command = command_for_finding(finding)
    label = f"Day {command.day_index}"
    return PublicChangePreview(
        change_token=change_token,
        title="加入用餐休息",
        summary=f"在 {label} 的前两处活动之间预留午餐时间。",
        affected_days=[label],
        before=["当天没有明确用餐停留"],
        after=["12:30 预留午餐时间，具体餐厅仍可稍后选择"],
    )
