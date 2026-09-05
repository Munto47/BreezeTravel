"""Schedule checks consume recorded times and fresh route evidence only."""
from app.audit.models import AuditDependency, AuditFinding, AuditSeverity, AuditStatus, EvidenceFreshness
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.timing import clock_minutes, shift_clock


def stop_end(stop):
    if stop.end_time:
        return clock_minutes(stop.end_time)
    if stop.start_time and stop.visit_duration_minutes is not None:
        return clock_minutes(stop.start_time) + stop.visit_duration_minutes
    return None


def route_minutes(fact):
    duration = (fact.value or {}).get("selected_duration_minutes") if fact else None
    if fact is None or fact.freshness_status != EvidenceFreshness.FRESH or type(duration) is not int or duration <= 0:
        return None
    return duration


def propagate_delay(stops, start_index, routes, sources, inconsistent):
    """Preserve visits; consume each real gap before delaying the following stop."""
    changes, evidence = [], []
    previous_end = stop_end(stops[start_index])
    for index in range(start_index + 1, len(stops)):
        left, current = stops[index - 1], stops[index]
        fact = routes.get(f"{left.stop_id}->{current.stop_id}")
        duration = route_minutes(fact)
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
    rule_version = "1.1.0"
    dependencies = (AuditDependency.TIME_WINDOW, AuditDependency.ROUTE_EDGE, AuditDependency.EVIDENCE_FRESHNESS)

    def evaluate(self, context):
        routes = {fact.subject_id: fact for fact in context.evidence_snapshot.facts if fact.fact_type == "ROUTE_MODE_SET"}
        sources = context.revision.change_summary.get("timing_sources", {})
        findings = []
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
                duration = route_minutes(fact)
                reliable = duration is not None
                if reliable and end + duration <= start:
                    continue
                shift = max(0, end + duration - start) if reliable else 0
                changes, evidence, blocked = propagate_delay(day.stops, index, routes, sources, inconsistent) if reliable else ([], [], "ROUTE_UNKNOWN")
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
            for reason, stop_ids in [("SCHEDULE_TIMES_MISSING", incomplete), ("SCHEDULE_TIMES_INCONSISTENT", inconsistent)]:
                if stop_ids:
                    findings.append(AuditFinding(
                        finding_id="finding_" + canonical_sha256(f"{context.revision.workspace_id}:{context.revision.revision}:{context.evidence_snapshot.snapshot_id}:{day.day_index}:{reason}")[:24],
                        rule_id=self.rule_id, rule_version=self.rule_version, status=AuditStatus.UNKNOWN,
                        severity=AuditSeverity.MEDIUM, reason_code=reason,
                        message="请补充或确认活动时间后，再判断当天是否来得及。",
                        affected_days=[day.day_index], affected_stop_ids=list(dict.fromkeys(stop_ids)), repairable=False))
        return findings
