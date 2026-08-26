from __future__ import annotations

import unicodedata
from collections import defaultdict
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


def _normalize_city(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = "".join(normalized.split()).casefold()
    return normalized.removesuffix("市")


def _identity_city(fact: EvidenceFact) -> str:
    if not isinstance(fact.value, dict):
        return ""
    return str(fact.value.get("city") or "")


class PlaceCityRule:
    """Verify every scheduled place against fresh, provider-backed identity evidence."""

    rule_id = "audit.place_city"
    rule_version = "1.0.0"
    dependencies = (AuditDependency.DAY_ORDER, AuditDependency.EVIDENCE_FRESHNESS)

    def evaluate(self, context: Any) -> list[AuditFinding]:
        identity_facts: dict[str, list[EvidenceFact]] = defaultdict(list)
        for fact in context.evidence_snapshot.facts:
            if fact.subject_type == "PLACE" and fact.fact_type == "POI_IDENTITY":
                identity_facts[fact.subject_id].append(fact)

        target_city = context.revision.city
        normalized_target = _normalize_city(target_city)
        findings: list[AuditFinding] = []
        for day in context.revision.days:
            for stop in day.stops:
                facts = identity_facts.get(stop.place_id, [])
                # A conflicting identity has one authoritative finding from
                # EvidenceConflictRule. Do not duplicate it as a city-unknown
                # finding here.
                if any(fact.freshness_status == EvidenceFreshness.CONFLICTING for fact in facts):
                    continue

                fresh = [fact for fact in facts if fact.freshness_status == EvidenceFreshness.FRESH]
                usable = next((fact for fact in fresh if _normalize_city(_identity_city(fact))), None)
                evidence_ids = [fact.fact_id for fact in facts]
                input_values = {
                    "stop_id": stop.stop_id,
                    "place_id": stop.place_id,
                    "target_city": target_city,
                    "observed_cities": [_identity_city(fact) or None for fact in facts],
                    "freshness_statuses": [fact.freshness_status.value for fact in facts],
                }

                if usable is None:
                    findings.append(
                        AuditFinding(
                            finding_id=str(uuid4()),
                            rule_id=self.rule_id,
                            rule_version=self.rule_version,
                            status=AuditStatus.UNKNOWN,
                            severity=AuditSeverity.HIGH,
                            reason_code="PLACE_CITY_UNKNOWN",
                            message=f"{stop.raw_name or stop.place_id} 缺少可用于核验城市的最新地点身份事实",
                            input_values=input_values,
                            affected_days=[day.day_index],
                            affected_stop_ids=[stop.stop_id],
                            evidence_fact_ids=evidence_ids,
                            confirmation_action="请刷新地点身份事实后重新审计",
                        )
                    )
                    continue

                observed_city = _identity_city(usable)
                matches = _normalize_city(observed_city) == normalized_target
                findings.append(
                    AuditFinding(
                        finding_id=str(uuid4()),
                        rule_id=self.rule_id,
                        rule_version=self.rule_version,
                        status=AuditStatus.SATISFIED if matches else AuditStatus.VIOLATED,
                        severity=AuditSeverity.INFO if matches else AuditSeverity.BLOCKER,
                        reason_code="PLACE_CITY_MATCH" if matches else "PLACE_CITY_MISMATCH",
                        message=(
                            f"{stop.raw_name or stop.place_id} 的地点身份城市与行程城市一致"
                            if matches
                            else f"{stop.raw_name or stop.place_id} 位于{observed_city}，不属于目标城市{target_city}"
                        ),
                        input_values={**input_values, "observed_city": observed_city},
                        affected_days=[day.day_index],
                        affected_stop_ids=[stop.stop_id],
                        evidence_fact_ids=[usable.fact_id],
                        repairable=not matches,
                        confirmation_action=None if matches else "请确认地点，或替换为目标城市内的地点",
                    )
                )
        return findings


class EvidenceConflictRule:
    """Expose provider conflicts without selecting the most optimistic value."""

    rule_id = "audit.evidence_conflict"
    rule_version = "1.0.0"
    dependencies = (AuditDependency.EVIDENCE_FRESHNESS,)

    def evaluate(self, context: Any) -> list[AuditFinding]:
        grouped: dict[tuple[str, str, str], list[EvidenceFact]] = defaultdict(list)
        for fact in context.evidence_snapshot.facts:
            if fact.freshness_status == EvidenceFreshness.CONFLICTING:
                grouped[(fact.subject_type, fact.subject_id, fact.fact_type)].append(fact)

        findings: list[AuditFinding] = []
        for (subject_type, subject_id, fact_type), facts in grouped.items():
            affected_days, affected_stops = self._affected_revision_items(context, subject_type, subject_id)
            findings.append(
                AuditFinding(
                    finding_id=str(uuid4()),
                    rule_id=self.rule_id,
                    rule_version=self.rule_version,
                    status=AuditStatus.UNKNOWN,
                    severity=AuditSeverity.HIGH,
                    reason_code="EVIDENCE_CONFLICT",
                    message=f"{subject_type} {subject_id} 的 {fact_type} 事实来源互相冲突，系统不会自动选择其一",
                    input_values={
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                        "fact_type": fact_type,
                        "conflicting_facts": [
                            {
                                "fact_id": fact.fact_id,
                                "provider": fact.provider,
                                "source_url": fact.source_url,
                                "observed_at": fact.observed_at.isoformat(),
                            }
                            for fact in facts
                        ],
                    },
                    affected_days=affected_days,
                    affected_stop_ids=affected_stops,
                    evidence_fact_ids=[fact.fact_id for fact in facts],
                    confirmation_action="请核对权威来源或刷新冲突事实后重新审计",
                )
            )
        return findings

    @staticmethod
    def _affected_revision_items(context: Any, subject_type: str, subject_id: str) -> tuple[list[int], list[str]]:
        if subject_type == "PLACE":
            matches = [
                (day.day_index, stop.stop_id)
                for day in context.revision.days
                for stop in day.stops
                if stop.place_id == subject_id
            ]
            return sorted({day for day, _ in matches}), [stop_id for _, stop_id in matches]
        if subject_type == "DAY":
            try:
                return [int(subject_id)], []
            except ValueError:
                return [], []
        if subject_type == "ROUTE_EDGE":
            stop_ids = set(subject_id.split("->", maxsplit=1))
            matches = [
                (day.day_index, stop.stop_id)
                for day in context.revision.days
                for stop in day.stops
                if stop.stop_id in stop_ids
            ]
            return sorted({day for day, _ in matches}), [stop_id for _, stop_id in matches]
        return [], []
