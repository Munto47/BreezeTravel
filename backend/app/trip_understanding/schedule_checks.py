"""Schedule checks consume recorded times and fresh route evidence only."""
from app.audit.models import AuditDependency, AuditFinding, AuditSeverity, AuditStatus, EvidenceFreshness
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.timing import clock_minutes


def stop_end(stop):
    if stop.end_time:
        return clock_minutes(stop.end_time)
    if stop.start_time and stop.visit_duration_minutes is not None:
        return clock_minutes(stop.start_time) + stop.visit_duration_minutes
    return None


class ScheduleFeasibilityRule:
    rule_id = "experience.schedule_feasibility"
    rule_version = "1.0.0"
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
                value = dict(fact.value or {}) if fact else {}
                duration = value.get("selected_duration_minutes")
                reliable = fact is not None and fact.freshness_status == EvidenceFreshness.FRESH and isinstance(duration, int) and duration > 0
                if reliable and end + duration <= start:
                    continue
                shift = max(0, end + duration - start) if reliable else 0
                shifted = []
                repairable = reliable
                for stop in day.stops[index + 1:]:
                    if stop.locked or stop.fixed_commitment:
                        # Do not move an appointment or offer a partial shift whose boundary is uncertain.
                        repairable = False
                        break
                    if not stop.start_time or clock_minutes(stop.start_time) + shift >= 1440 or (stop_end(stop) is not None and stop_end(stop) + shift >= 1440):
                        repairable = False
                        break
                    shifted.append(stop.stop_id)
                findings.append(AuditFinding(
                    finding_id="finding_" + canonical_sha256(f"{context.revision.workspace_id}:{context.revision.revision}:{context.evidence_snapshot.snapshot_id}:{subject}:time")[:24],
                    rule_id=self.rule_id, rule_version=self.rule_version,
                    status=AuditStatus.VIOLATED if reliable else AuditStatus.UNKNOWN,
                    severity=AuditSeverity.HIGH if reliable else AuditSeverity.MEDIUM,
                    reason_code="SCHEDULE_CONFLICT" if reliable else "SCHEDULE_ROUTE_UNKNOWN",
                    message=(f"{left.raw_name}结束并到达{right.raw_name}，预计比安排晚{shift}分钟。" if reliable else "行程时间需要补充路线后确认。"),
                    affected_days=[day.day_index], affected_stop_ids=[left.stop_id, right.stop_id],
                    evidence_fact_ids=[fact.fact_id] if fact else [],
                    input_values={"shift_minutes": shift, "shift_stop_ids": shifted},
                    repairable=bool(repairable and shifted),
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
