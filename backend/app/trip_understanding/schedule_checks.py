"""Schedule checks consume recorded times and fresh route evidence only."""
from datetime import datetime, timezone

from app.audit.models import AuditDependency, AuditFinding, AuditSeverity, AuditStatus, EvidenceFreshness
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.timing import clock_minutes, shift_clock


def stop_end(stop):
    if stop.end_time:
        return clock_minutes(stop.end_time)
    if stop.start_time and stop.visit_duration_minutes is not None:
        return clock_minutes(stop.start_time) + stop.visit_duration_minutes
    return None


def route_minutes(fact, now=None):
    """A mode flag alone is never sufficient evidence of a usable duration."""
    value = getattr(fact, "value", None)
    if not isinstance(value, dict):
        return None
    duration = value.get("selected_duration_minutes")
    mode = value.get("selected_mode")
    observed_at = now or datetime.now(timezone.utc)
    valid_from, valid_until = getattr(fact, "valid_from", None), getattr(fact, "valid_until", None)
    if (
        fact.freshness_status != EvidenceFreshness.FRESH
        or not isinstance(mode, str) or mode not in {"walking", "transit"}
        or value.get(mode) != "AVAILABLE"
        or type(duration) is not int
        or duration <= 0
        or (valid_from is not None and (not isinstance(valid_from, datetime) or valid_from.tzinfo is None or valid_from > observed_at))
        or not isinstance(valid_until, datetime) or valid_until.tzinfo is None or valid_until <= observed_at
    ):
        return None
    return duration


def route_facts_by_edge(snapshot):
    facts = {}
    for fact in snapshot.facts:
        if fact.fact_type == "ROUTE_MODE_SET":
            # A second competing fact must not silently replace the first one.
            facts[fact.subject_id] = None if fact.subject_id in facts else fact
    return facts


def propagate_delay(stops, start_index, routes, sources, inconsistent, now=None):
    """Preserve visits; consume each real gap before delaying the following stop."""
    changes, evidence = [], []
    previous_end = stop_end(stops[start_index])
    for index in range(start_index + 1, len(stops)):
        left, current = stops[index - 1], stops[index]
        fact = routes.get(f"{left.stop_id}->{current.stop_id}")
        duration = route_minutes(fact, now)
        if previous_end is None or duration is None:
            return [], evidence, "ROUTE_OR_DURATION_UNKNOWN"
        evidence.append(fact.fact_id)
        original_start = clock_minutes(current.start_time)
        if original_start is None or sources.get(current.stop_id) == "SUGGESTED" or current.stop_id in inconsistent:
            return [], evidence, "TIME_NEEDS_CONFIRMATION"
        earliest = previous_end + duration
        if earliest <= original_start:
            # This existing gap absorbs the delay. Later appointments and unknown
            # routes are unaffected and must not make this safe prefix move fail.
            return changes, evidence, None
        if current.locked or current.fixed_commitment:
            return [], evidence, "LOCKED_ACTIVITY"
        shift = earliest - original_start
        current_end = stop_end(current)
        if earliest >= 1440 or (current_end is not None and current_end + shift >= 1440):
            return [], evidence, "DAY_BOUNDARY"
        if current_end is None and index < len(stops) - 1:
            return [], evidence, "DURATION_UNKNOWN"
        changes.append({"stop_id": current.stop_id, "minutes": shift,
            "start_time": shift_clock(current.start_time, shift),
            "end_time": shift_clock(current.end_time, shift)})
        previous_end = current_end + shift if current_end is not None else None
    return changes, evidence, None


class ScheduleFeasibilityRule:
    rule_id = "experience.schedule_feasibility"
    rule_version = "1.2.0"
    dependencies = (AuditDependency.TIME_WINDOW, AuditDependency.ROUTE_EDGE, AuditDependency.EVIDENCE_FRESHNESS)

    def evaluate(self, context):
        routes = route_facts_by_edge(context.evidence_snapshot)
        sources = context.revision.change_summary.get("timing_sources", {})
        findings = []
        missing_days, missing_stops = [], []
        now = getattr(context, "now", None)
        for day in context.revision.days:
            incomplete = []
            inconsistent = []
            for stop in day.stops:
                if stop.start_time and stop.end_time and stop.visit_duration_minutes is not None and clock_minutes(stop.end_time) - clock_minutes(stop.start_time) != stop.visit_duration_minutes:
                    inconsistent.append(stop.stop_id)
            for index, left in enumerate(day.stops[:-1]):
                right = day.stops[index + 1]
                end, start = stop_end(left), clock_minutes(right.start_time)
                if end is None or start is None or sources.get(left.stop_id) == "SUGGESTED" or sources.get(right.stop_id) == "SUGGESTED":
                    incomplete.extend([left.stop_id, right.stop_id])
                    continue
                if left.stop_id in inconsistent or right.stop_id in inconsistent:
                    continue
                subject = f"{left.stop_id}->{right.stop_id}"
                fact = routes.get(subject)
                duration = route_minutes(fact, now)
                reliable = duration is not None
                if reliable and end + duration <= start:
                    continue
                if not reliable and end > start:
                    findings.append(AuditFinding(
                        finding_id="finding_" + canonical_sha256(f"{context.revision.workspace_id}:{context.revision.revision}:{context.evidence_snapshot.snapshot_id}:{subject}:overlap")[:24],
                        rule_id=self.rule_id, rule_version=self.rule_version,
                        status=AuditStatus.VIOLATED, severity=AuditSeverity.HIGH,
                        reason_code="SCHEDULE_TIME_OVERLAP",
                        message=f"{left.raw_name}尚未结束，{right.raw_name}就已开始，两处活动的明确时间重叠。交通信息不足，暂不自动顺延。",
                        affected_days=[day.day_index], affected_stop_ids=[left.stop_id, right.stop_id],
                        repairable=False,
                    ))
                    continue
                shift = max(0, end + duration - start) if reliable else 0
                changes, evidence, blocked = propagate_delay(day.stops, index, routes, sources, inconsistent, now) if reliable else ([], [], "ROUTE_UNKNOWN")
                message = "行程时间需要补充路线后确认。"
                if reliable:
                    earliest = end + duration
                    arrival = f"{earliest // 60:02d}:{earliest % 60:02d}" if earliest < 1440 else "次日"
                    message = f"{left.raw_name}结束后交通约{duration}分钟，最早约{arrival}到达{right.raw_name}；比计划{right.start_time}晚{shift}分钟。"
                    if blocked == "LOCKED_ACTIVITY":
                        message += "涉及已锁定或预约的活动，请手动调整前一站或预约时间。"
                    elif blocked == "DAY_BOUNDARY":
                        message += "顺延会跨过当天，请手动调整。"
                    elif blocked:
                        message += "后续时间或路线信息不完整，暂不能自动顺延。"
                findings.append(AuditFinding(
                    finding_id="finding_" + canonical_sha256(f"{context.revision.workspace_id}:{context.revision.revision}:{context.evidence_snapshot.snapshot_id}:{subject}:time")[:24],
                    rule_id=self.rule_id, rule_version=self.rule_version,
                    status=AuditStatus.VIOLATED if reliable else AuditStatus.UNKNOWN,
                    severity=AuditSeverity.HIGH if reliable else AuditSeverity.MEDIUM,
                    reason_code="SCHEDULE_CONFLICT" if reliable else "SCHEDULE_ROUTE_UNKNOWN",
                    message=message,
                    affected_days=[day.day_index], affected_stop_ids=[left.stop_id, right.stop_id],
                    evidence_fact_ids=list(dict.fromkeys(([fact.fact_id] if fact else []) + evidence)),
                    input_values={"shift_minutes": shift, "shift_stop_ids": [change["stop_id"] for change in changes],
                        "shift_changes": changes, "propagation_blocked": blocked},
                    repairable=bool(changes and blocked is None),
                ))
            if incomplete:
                missing_days.append(day.day_index)
                missing_stops.extend(incomplete)
            for reason, stop_ids in [("SCHEDULE_TIMES_INCONSISTENT", inconsistent)]:
                if stop_ids:
                    findings.append(AuditFinding(
                        finding_id="finding_" + canonical_sha256(f"{context.revision.workspace_id}:{context.revision.revision}:{context.evidence_snapshot.snapshot_id}:{day.day_index}:{reason}")[:24],
                        rule_id=self.rule_id, rule_version=self.rule_version, status=AuditStatus.UNKNOWN,
                        severity=AuditSeverity.MEDIUM, reason_code=reason,
                        message="请补充或确认活动时间后，再判断当天是否来得及。",
                        affected_days=[day.day_index], affected_stop_ids=list(dict.fromkeys(stop_ids)), repairable=False))
        if missing_stops:
            findings.append(AuditFinding(
                finding_id="finding_" + canonical_sha256(f"{context.revision.workspace_id}:{context.revision.revision}:{context.evidence_snapshot.snapshot_id}:missing-times")[:24],
                rule_id=self.rule_id, rule_version=self.rule_version, status=AuditStatus.UNKNOWN,
                severity=AuditSeverity.INFO, reason_code="SCHEDULE_TIMES_MISSING",
                message="目前按地点先后顺序整理，未提供的活动时间和停留时长尚未核对。",
                affected_days=missing_days, affected_stop_ids=list(dict.fromkeys(missing_stops)), repairable=False,
            ))
        return findings
