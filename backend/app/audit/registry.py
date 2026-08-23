from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from app.audit.models import (
    AuditDependency,
    AuditFinding,
    AuditSeverity,
    AuditStatus,
    EvidenceFreshness,
    EvidenceSnapshot,
)
from app.audit.fact_rules import EvidenceConflictRule, PlaceCityRule
from app.audit.brief_rules import ConfirmedGroupIntensityRule
from app.audit.route_rules import CommitmentFeasibilityRule, RouteGapRule
from app.audit.severity import SeverityPolicy
from app.constraints.base import RuleContext
from app.constraints.registry import RuleDescriptor, default_rule_descriptors
from app.itineraries.adapters import revision_to_legacy
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import ItineraryRevision, ResolutionStatus
from app.members.models import MemberConstraint
from app.schemas.itinerary import DayPlan, WeatherInfo
from app.schemas.task_spec import TripTaskSpec


@dataclass(frozen=True)
class AuditRuleContext:
    task_spec: TripTaskSpec
    revision: ItineraryRevision
    evidence_snapshot: EvidenceSnapshot
    now: datetime
    member_constraints: tuple[MemberConstraint, ...] = ()


class AuditRule(Protocol):
    rule_id: str
    rule_version: str
    dependencies: tuple[AuditDependency, ...]

    def evaluate(self, context: AuditRuleContext) -> list[AuditFinding]: ...


class InputCompletenessRule:
    rule_id = "audit.input_completeness"
    rule_version = "1.0.0"
    dependencies = (AuditDependency.DAY_ORDER, AuditDependency.TIME_WINDOW)

    def evaluate(self, context: AuditRuleContext) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for day in context.revision.days:
            for stop in day.stops:
                if stop.resolution_status in {ResolutionStatus.AMBIGUOUS, ResolutionStatus.NOT_FOUND}:
                    findings.append(
                        AuditFinding(
                            finding_id=str(uuid4()),
                            rule_id=self.rule_id,
                            rule_version=self.rule_version,
                            status=AuditStatus.VIOLATED,
                            severity=AuditSeverity.BLOCKER,
                            reason_code="PLACE_NOT_RESOLVED",
                            message=f"{stop.raw_name or stop.stop_id} 尚未完成实体确认",
                            input_values={
                                "stop_id": stop.stop_id,
                                "raw_name": stop.raw_name,
                                "resolution_status": stop.resolution_status.value,
                            },
                            affected_days=[day.day_index],
                            affected_stop_ids=[stop.stop_id],
                            confirmation_action="请先确认正确地点后再审计",
                        )
                    )
        if findings:
            return findings
        return [
            AuditFinding(
                finding_id=str(uuid4()),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                status=AuditStatus.SATISFIED,
                severity=AuditSeverity.INFO,
                reason_code="AUDIT_INPUT_COMPLETE",
                message="行程日期、天数和地点实体满足审计输入要求",
            )
        ]


def _facts_by_place(snapshot: EvidenceSnapshot) -> dict[str, list]:
    result: dict[str, list] = {}
    for fact in snapshot.facts:
        if fact.subject_type == "PLACE":
            result.setdefault(fact.subject_id, []).append(fact)
    return result


class LegacyConstraintRuleAdapter:
    def __init__(self, descriptor: RuleDescriptor, severity_policy: SeverityPolicy):
        self._descriptor = descriptor
        self._rule = descriptor.rule
        self.rule_id = f"constraint.{self._rule.rule_id}"
        self.rule_version = descriptor.version
        self.dependencies = tuple(AuditDependency(item) for item in descriptor.dependencies)
        self._severity_policy = severity_policy

    def evaluate(self, context: AuditRuleContext) -> list[AuditFinding]:
        facts_by_place = _facts_by_place(context.evidence_snapshot)
        place_lookup: dict[str, dict] = {}
        place_meta: dict[str, dict] = {}
        for day in context.revision.days:
            for stop in day.stops:
                identity = next(
                    (
                        fact
                        for fact in facts_by_place.get(stop.place_id, [])
                        if fact.fact_type == "POI_IDENTITY" and fact.freshness_status == EvidenceFreshness.FRESH
                    ),
                    None,
                )
                place = dict(identity.value or {}) if identity else {}
                place.setdefault("place_id", stop.place_id)
                place.setdefault("name", stop.raw_name or stop.place_id)
                place.setdefault("category", stop.category)
                place_lookup[stop.place_id] = place
                opening_candidates = [
                    fact for fact in facts_by_place.get(stop.place_id, []) if fact.fact_type == "OPENING_HOURS"
                ]
                opening = (
                    next(
                        (fact for fact in opening_candidates if fact.freshness_status == EvidenceFreshness.FRESH), None
                    )
                    or next(
                        (fact for fact in opening_candidates if fact.freshness_status == EvidenceFreshness.STALE), None
                    )
                    or (opening_candidates[0] if opening_candidates else None)
                )
                if opening and opening.freshness_status == EvidenceFreshness.FRESH:
                    place_meta.setdefault(stop.place_id, {})["opening_hours"] = opening.value
                    if opening.valid_until:
                        place_meta[stop.place_id]["expires_at"] = opening.valid_until.isoformat()
                elif opening and opening.freshness_status == EvidenceFreshness.STALE:
                    place_meta.setdefault(stop.place_id, {})["opening_hours"] = opening.value
                    place_meta[stop.place_id]["expires_at"] = opening.valid_until or context.now

        legacy = revision_to_legacy(
            context.revision,
            thread_id=context.revision.workspace_id,
            place_lookup=place_lookup,
            preserve_unknown_times=True,
        )
        route_facts = {
            fact.subject_id: fact
            for fact in context.evidence_snapshot.facts
            if fact.subject_type == "ROUTE_EDGE"
            and fact.fact_type == "ROUTE_TIME"
            and fact.freshness_status == EvidenceFreshness.FRESH
        }
        weather_facts = {
            fact.subject_id: fact
            for fact in context.evidence_snapshot.facts
            if fact.subject_type == "DAY"
            and fact.fact_type == "WEATHER"
            and fact.freshness_status == EvidenceFreshness.FRESH
        }
        legacy_days: list[DayPlan] = []
        for day in legacy.days:
            slots = []
            revision_day = context.revision.days[day.day_index]
            for index, slot in enumerate(day.slots):
                transport = None
                if index < len(revision_day.stops) - 1:
                    left = revision_day.stops[index]
                    right = revision_day.stops[index + 1]
                    fact = route_facts.get(f"{left.stop_id}->{right.stop_id}")
                    if fact and isinstance(fact.value, dict):
                        from app.schemas.itinerary import TransportLeg

                        transport = TransportLeg(
                            mode=str(fact.value.get("mode") or "driving"),
                            duration_mins=int(fact.value.get("duration_minutes") or 0),
                            distance_km=float(fact.value.get("distance_km") or 0),
                        )
                slots.append(slot.model_copy(update={"transport": transport}))
            weather = None
            weather_fact = weather_facts.get(str(day.day_index))
            if weather_fact and isinstance(weather_fact.value, dict):
                weather = WeatherInfo.model_validate(weather_fact.value)
            legacy_days.append(day.model_copy(update={"slots": slots, "weather_summary": weather}))
        legacy = legacy.model_copy(update={"days": legacy_days})

        checks = self._rule.evaluate(
            RuleContext(
                task_spec=context.task_spec,
                itinerary=legacy,
                place_meta=place_meta,
                now=context.now,
            )
        )
        findings: list[AuditFinding] = []
        for check in checks:
            if (
                self._rule.rule_id == "opening_hours"
                and check.reason_code == "OPENING_HOURS_MISSING"
                and check.place_id is not None
                and any(
                    fact.subject_type == "PLACE"
                    and fact.subject_id == check.place_id
                    and fact.fact_type == "OPENING_HOURS"
                    and fact.freshness_status == EvidenceFreshness.CONFLICTING
                    for fact in context.evidence_snapshot.facts
                )
            ):
                # EvidenceConflictRule owns this UNKNOWN outcome and retains
                # every conflicting source. A second "missing" finding would
                # misdescribe the data and duplicate the same user action.
                continue
            status = AuditStatus(check.status.value)
            affected_stops = [
                stop.stop_id
                for day in context.revision.days
                for stop in day.stops
                if check.place_id is not None and stop.place_id == check.place_id
            ]
            evidence_ids = [
                fact.fact_id
                for fact in context.evidence_snapshot.facts
                if (
                    (check.place_id is not None and fact.subject_id == check.place_id)
                    or (
                        check.day_index is not None
                        and fact.subject_type == "DAY"
                        and fact.subject_id == str(check.day_index)
                    )
                )
            ]
            findings.append(
                AuditFinding(
                    finding_id=str(uuid4()),
                    rule_id=self.rule_id,
                    rule_version=self.rule_version,
                    status=status,
                    severity=self._severity_policy.classify(status=status, reason_code=check.reason_code),
                    reason_code=check.reason_code,
                    message=check.message,
                    input_values={
                        "day_index": check.day_index,
                        "place_id": check.place_id,
                        "day_stops": [
                            {
                                "stop_id": stop.stop_id,
                                "place_id": stop.place_id,
                                "start_time": stop.start_time,
                                "end_time": stop.end_time,
                                "locked": stop.locked,
                                "fixed_commitment": stop.fixed_commitment,
                            }
                            for day in context.revision.days
                            if check.day_index is not None and day.day_index == check.day_index
                            for stop in day.stops
                        ],
                    },
                    affected_days=[check.day_index] if check.day_index is not None else [],
                    affected_stop_ids=affected_stops,
                    evidence_fact_ids=evidence_ids,
                    repairable=check.repairable,
                    confirmation_action=(
                        "请补充该日程的开始和结束时间后重新审计"
                        if status == AuditStatus.UNKNOWN and check.reason_code == "TIME_DATA_INVALID"
                        else "请补充或刷新对应事实后重新审计"
                        if status == AuditStatus.UNKNOWN
                        else None
                    ),
                )
            )
        return findings


class AuditRuleRegistry:
    def __init__(self, rules: list[AuditRule] | None = None):
        if rules is None:
            # Imported lazily: the member rule consumes AuditRuleContext from
            # this module, while the registry remains the sole rule-set owner.
            from app.audit.member_constraints import MemberConstraintAuditRule

            severity = SeverityPolicy()
            rules = [
                InputCompletenessRule(),
                PlaceCityRule(),
                EvidenceConflictRule(),
                RouteGapRule(),
                CommitmentFeasibilityRule(),
                ConfirmedGroupIntensityRule(),
                MemberConstraintAuditRule(),
                *[LegacyConstraintRuleAdapter(descriptor, severity) for descriptor in default_rule_descriptors()],
            ]
        self.rules = rules
        ids = [rule.rule_id for rule in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("audit rule ids must be unique")

    @property
    def rule_set_version(self) -> str:
        digest = sha256_canonical(
            [
                {
                    "rule_id": rule.rule_id,
                    "rule_version": rule.rule_version,
                    "dependencies": [item.value for item in rule.dependencies],
                }
                for rule in self.rules
            ]
        )[:16]
        return f"audit-rules-{digest}"
