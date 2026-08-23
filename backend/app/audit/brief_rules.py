from __future__ import annotations

from uuid import uuid4

from app.audit.models import (
    AuditDependency,
    AuditFinding,
    AuditSeverity,
    AuditStatus,
    EvidenceFreshness,
)


class ConfirmedGroupIntensityRule:
    """Keep a confirmed large-group intensity concern explicit, never HARD.

    Traveler count alone cannot prove that a day is infeasible. For a confirmed
    five-person Brief with multiple stops, the correct deterministic outcome is
    therefore UNKNOWN plus a concrete confirmation action, not PASS and not a
    fabricated capacity limit.
    """

    rule_id = "trip_brief.group_intensity"
    rule_version = "1.0.0"
    dependencies = (AuditDependency.MEMBER_CONSTRAINT, AuditDependency.DAY_ORDER)

    def evaluate(self, context) -> list[AuditFinding]:
        facts = [
            fact
            for fact in context.evidence_snapshot.facts
            if fact.subject_type == "TRIP_BRIEF"
            and fact.fact_type == "TRAVELER_COUNT"
            and fact.freshness_status == EvidenceFreshness.FRESH
        ]
        if not facts:
            return []
        fact = facts[-1]
        try:
            traveler_count = int(fact.value)
        except (TypeError, ValueError):
            return []
        if traveler_count < 5:
            return []
        findings: list[AuditFinding] = []
        for day in context.revision.days:
            if len(day.stops) < 2:
                continue
            findings.append(
                AuditFinding(
                    finding_id=str(uuid4()),
                    rule_id=self.rule_id,
                    rule_version=self.rule_version,
                    status=AuditStatus.UNKNOWN,
                    severity=AuditSeverity.MEDIUM,
                    reason_code="DAILY_CAPACITY",
                    message=(
                        f"第 {day.day_index + 1} 天为 {traveler_count} 人安排了 "
                        f"{len(day.stops)} 个地点；仅凭人数不能证明强度可接受"
                    ),
                    input_values={
                        "traveler_count": traveler_count,
                        "stop_count": len(day.stops),
                        "brief_confirmation": "CONFIRMED",
                    },
                    affected_days=[day.day_index],
                    affected_stop_ids=[stop.stop_id for stop in day.stops],
                    evidence_fact_ids=[fact.fact_id],
                    repairable=False,
                    confirmation_action="请同行人确认当日步行与停留强度，必要时减少地点",
                )
            )
        return findings
