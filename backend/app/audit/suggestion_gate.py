"""Authoritative, non-persisting AuditEngine gate for next-stop suggestions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.audit.engine import AuditEngine
from app.audit.evidence_service import EvidenceObservation, EvidenceService
from app.audit.models import (
    AuditDependency,
    AuditFinding,
    AuditRunInput,
    AuditSeverity,
    AuditStatus,
    EvidenceFreshness,
)
from app.audit.registry import AuditRuleContext, AuditRuleRegistry
from app.audit.repositories import AuditRepository
from app.audit.system_constraints import with_system_constraints
from app.itineraries.hash_service import sha256_canonical, with_content_hash
from app.itineraries.models import (
    ItineraryRevision,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    RevisionTransport,
    TripWorkspace,
)
from app.members.models import MemberConstraint
from app.members.repositories import MemberConstraintRepository
from app.schemas.task_spec import DateRange, TripTaskSpec
from app.suggestions.models import (
    HardGate,
    RouteReceipt,
    RouteReceiptLeg,
    SuggestionAuditGateReceipt,
    SuggestionCandidateDraft,
    SuggestionClassification,
    SuggestionGateFinding,
)


SLOT_POLICY_VERSION = "suggestion-slot-v1"
_MIN_VISIT_MINUTES = 30


def _without_candidate(value, *, stop_id: str, place_id: str):
    if isinstance(value, dict):
        if stop_id and value.get("stop_id") == stop_id:
            return None
        return {
            key: scrubbed
            for key, item in sorted(value.items())
            if (scrubbed := _without_candidate(item, stop_id=stop_id, place_id=place_id))
            is not None
        }
    if isinstance(value, list):
        return [
            scrubbed
            for item in value
            if (scrubbed := _without_candidate(item, stop_id=stop_id, place_id=place_id))
            is not None
        ]
    if value in {stop_id, place_id}:
        return None
    return value


def _finding_delta_fingerprint(
    finding: AuditFinding,
    *,
    candidate_stop_id: str = "",
    candidate_place_id: str = "",
) -> str:
    progressive_completion = finding.rule_id in {
        "constraint.daily_hotel",
        "constraint.meal_window",
        "constraint.pacing",
    }
    scrubbed_inputs = _without_candidate(
        finding.input_values,
        stop_id=candidate_stop_id,
        place_id=candidate_place_id,
    )
    if progressive_completion and isinstance(scrubbed_inputs, dict):
        scrubbed_inputs = {
            key: value
            for key, value in scrubbed_inputs.items()
            if key not in {"day_stops", "place_id", "stop_id"}
        }
    return sha256_canonical({
        "rule_id": finding.rule_id,
        "rule_version": finding.rule_version,
        "status": finding.status.value,
        "severity": finding.severity.value,
        "reason_code": finding.reason_code,
        "affected_stop_ids": [] if progressive_completion else sorted(
            value for value in finding.affected_stop_ids if value != candidate_stop_id
        ),
        "affected_days": sorted(finding.affected_days),
        "affected_member_ids": sorted(finding.affected_member_ids),
        "input_values": scrubbed_inputs,
    })


def _minutes(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (TypeError, ValueError):
        return None
    return hour * 60 + minute if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def _clock(value: int | None) -> str | None:
    if value is None or not 0 <= value < 24 * 60:
        return None
    return f"{value // 60:02d}:{value % 60:02d}"


def candidate_slot_times(
    after: ItineraryStop | None,
    before: ItineraryStop | None,
    receipts: dict[RouteReceiptLeg, RouteReceipt],
) -> tuple[str | None, str | None]:
    """Versioned deterministic slot policy shared by gate and acceptance."""

    previous_leg = receipts.get(RouteReceiptLeg.PREVIOUS_TO_CANDIDATE)
    next_leg = receipts.get(RouteReceiptLeg.CANDIDATE_TO_NEXT)
    if after and before and previous_leg and next_leg:
        after_end = _minutes(after.end_time)
        before_start = _minutes(before.start_time)
        start_minutes = (after_end if after_end is not None else -1) + previous_leg.duration_minutes
        end_minutes = (before_start if before_start is not None else -1) - next_leg.duration_minutes
        if end_minutes - start_minutes >= _MIN_VISIT_MINUTES:
            return _clock(start_minutes), _clock(end_minutes)
    elif after and previous_leg:
        after_end = _minutes(after.end_time)
        start_minutes = (after_end if after_end is not None else -1) + previous_leg.duration_minutes
        return _clock(start_minutes), _clock(start_minutes + 60)
    elif before and next_leg:
        before_start = _minutes(before.start_time)
        end_minutes = (before_start if before_start is not None else -1) - next_leg.duration_minutes
        return _clock(end_minutes - 60), _clock(end_minutes)
    return None, None


def _normalise(value: object) -> str:
    return "".join(str(value or "").casefold().split())


class DirectAcceptCurrentFactRule:
    """Require only current facts that are applicable to this candidate."""

    rule_id = "suggestion.direct_accept_current_facts"
    rule_version = "1.0.0"
    dependencies = (AuditDependency.EVIDENCE_FRESHNESS, AuditDependency.MEMBER_CONSTRAINT)

    def evaluate(self, context: AuditRuleContext) -> list[AuditFinding]:
        summary = context.revision.change_summary
        if summary.get("operation") != "SUGGESTION_GATE_PREVIEW":
            return []
        stop_id = str(summary.get("candidate_stop_id") or "")
        place_id = str(summary.get("candidate_place_id") or "")
        stop = next(
            (item for day in context.revision.days for item in day.stops if item.stop_id == stop_id),
            None,
        )
        if stop is None:
            return [self._finding(
                status=AuditStatus.UNKNOWN,
                reason="SUGGESTION_SLOT_MISSING",
                message="候选没有版本化时间槽，不能直接加入行程。",
                stop_id=stop_id,
            )]

        required = {"OPENING_HOURS"} if stop.category in {"attraction", "food"} else set()
        task_kinds = {_normalise(item.type) for item in context.task_spec.hard_constraints}
        member_kinds = {_normalise(item.type) for item in context.member_constraints}
        if any(token in kind for kind in task_kinds for token in ("reservation", "booking", "预约")):
            required.add("RESERVATION_POLICY")
        if any(
            token in kind
            for kind in task_kinds | member_kinds
            for token in ("accessibility", "wheelchair", "stroller", "无障碍", "轮椅", "婴儿车")
        ):
            required.add("ACCESSIBILITY_POLICY")

        findings: list[AuditFinding] = []
        for fact_type in sorted(required):
            facts = [
                fact
                for fact in context.evidence_snapshot.facts
                if fact.subject_type == "PLACE"
                and fact.subject_id == place_id
                and fact.fact_type == fact_type
            ]
            fresh = [
                fact
                for fact in facts
                if fact.freshness_status == EvidenceFreshness.FRESH
                and fact.value not in (None, "", [], {})
                and (fact.valid_until is None or fact.valid_until >= context.now)
            ]
            if fresh:
                findings.append(self._finding(
                    status=AuditStatus.SATISFIED,
                    reason=f"{fact_type}_CURRENT",
                    message=f"候选具有新鲜、可追溯的 {fact_type} 事实。",
                    stop_id=stop_id,
                    evidence_ids=[fact.fact_id for fact in fresh],
                ))
            else:
                findings.append(self._finding(
                    status=AuditStatus.UNKNOWN,
                    reason=f"{fact_type}_CURRENT_FACT_UNKNOWN",
                    message=f"候选缺少新鲜、可追溯的 {fact_type} 事实，不能直接接受。",
                    stop_id=stop_id,
                    evidence_ids=[fact.fact_id for fact in facts],
                ))
        if not findings:
            findings.append(self._finding(
                status=AuditStatus.SATISFIED,
                reason="NO_APPLICABLE_DYNAMIC_FACT_REQUIREMENT",
                message="该候选没有额外适用的动态事实要求。",
                stop_id=stop_id,
            ))
        return findings

    def _finding(
        self,
        *,
        status: AuditStatus,
        reason: str,
        message: str,
        stop_id: str,
        evidence_ids: list[str] | None = None,
    ) -> AuditFinding:
        return AuditFinding(
            finding_id=str(uuid4()),
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            status=status,
            severity=AuditSeverity.INFO if status is AuditStatus.SATISFIED else AuditSeverity.HIGH,
            reason_code=reason,
            message=message,
            affected_stop_ids=[stop_id] if stop_id else [],
            evidence_fact_ids=evidence_ids or [],
            confirmation_action=None if status is AuditStatus.SATISFIED else "请刷新对应官方或运营方事实",
        )


class LockedAnchorSuggestionRule:
    """Fail closed where the product has no declared locked-edge edit rule."""

    rule_id = "suggestion.locked_anchor_policy"
    rule_version = "1.0.0"
    dependencies = (AuditDependency.DAY_ORDER, AuditDependency.ROUTE_EDGE)

    def evaluate(self, context: AuditRuleContext) -> list[AuditFinding]:
        summary = context.revision.change_summary
        if summary.get("operation") != "SUGGESTION_GATE_PREVIEW":
            return []
        candidate_stop_id = str(summary.get("candidate_stop_id") or "")
        after_id = summary.get("insert_after_stop_id")
        before_id = summary.get("insert_before_stop_id")
        stops = {stop.stop_id: stop for day in context.revision.days for stop in day.stops}
        findings: list[AuditFinding] = []
        for role, stop_id in (("AFTER", after_id), ("BEFORE", before_id)):
            stop = stops.get(str(stop_id or ""))
            if stop is None or not stop.locked:
                continue
            # A fixed target immediately after the candidate is already owned
            # by CommitmentFeasibilityRule, including route/buffer evidence.
            if role == "BEFORE" and stop.fixed_commitment:
                continue
            findings.append(AuditFinding(
                finding_id=str(uuid4()),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                status=AuditStatus.UNKNOWN,
                severity=AuditSeverity.BLOCKER,
                reason_code="LOCKED_EDGE_POLICY_NOT_IMPLEMENTED",
                message="候选触及 locked stop 的相邻边，但当前没有可验证的锁边编辑规则。",
                input_values={"anchor_role": role, "locked_stop_id": stop.stop_id},
                affected_stop_ids=[stop.stop_id, candidate_stop_id],
                confirmation_action="请先显式解锁该相邻边或使用人工编辑并重新审计",
            ))
        return findings


def _authority_hash(
    *,
    workspace: TripWorkspace,
    base: ItineraryRevision,
    candidate: SuggestionCandidateDraft,
    task_id: str,
    task_revision: int,
    task_workspace_revision: int | None,
    member_constraint_workspace_revision: int | None,
    member_revision_set: dict[str, int],
    evidence_input_hash: str,
    audit_rule_set_version: str,
) -> str:
    candidate_payload = candidate.model_dump(
        mode="json",
        exclude={"audit_gate", "suggestion_set_id"},
    )
    return sha256_canonical({
        "workspace_id": workspace.workspace_id,
        "base_revision": base.revision,
        "base_content_hash": base.content_hash,
        "task_id": task_id,
        "task_revision": task_revision,
        "workspace_task_revision": task_workspace_revision,
        "workspace_member_constraint_revision": member_constraint_workspace_revision,
        "member_constraint_revision_set": member_revision_set,
        "candidate": candidate_payload,
        "evidence_input_hash": evidence_input_hash,
        "audit_rule_set_version": audit_rule_set_version,
        "slot_policy_version": SLOT_POLICY_VERSION,
    })


def verify_frozen_gate_inputs(
    *,
    workspace: TripWorkspace,
    base: ItineraryRevision,
    candidate: SuggestionCandidateDraft,
) -> bool:
    gate = candidate.audit_gate
    if gate is None:
        return False
    if workspace.current_task_spec_revision != gate.task_workspace_revision:
        return False
    if workspace.current_member_constraint_revision != gate.member_constraint_workspace_revision:
        return False
    registry_version = SuggestionAuditGate.registry().rule_set_version
    if registry_version != gate.audit_rule_set_version:
        return False
    return gate.authority_input_hash == _authority_hash(
        workspace=workspace,
        base=base,
        candidate=candidate,
        task_id=gate.task_id,
        task_revision=gate.task_revision,
        task_workspace_revision=gate.task_workspace_revision,
        member_constraint_workspace_revision=gate.member_constraint_workspace_revision,
        member_revision_set=gate.member_constraint_revision_set,
        evidence_input_hash=gate.evidence_input_hash,
        audit_rule_set_version=gate.audit_rule_set_version,
    )


class SuggestionAuditGate:
    def __init__(
        self,
        audit_repository: AuditRepository,
        member_repository: MemberConstraintRepository | None = None,
        *,
        evidence_service: EvidenceService | None = None,
        clock=None,
    ):
        self.audit_repository = audit_repository
        self.member_repository = member_repository
        self.evidence_service = evidence_service or EvidenceService()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.engine = AuditEngine(self.registry())

    @staticmethod
    def registry() -> AuditRuleRegistry:
        default = AuditRuleRegistry()
        return AuditRuleRegistry([
            *default.rules,
            DirectAcceptCurrentFactRule(),
            LockedAnchorSuggestionRule(),
        ])

    async def evaluate_candidate(
        self,
        *,
        workspace: TripWorkspace,
        base: ItineraryRevision,
        candidate: SuggestionCandidateDraft,
        day_index: int,
        insert_after_stop_id: str | None,
        insert_before_stop_id: str | None,
    ) -> SuggestionCandidateDraft:
        now = self.clock()
        task = await self.audit_repository.load_task_spec(workspace.workspace_id)
        if task is None:
            days = (workspace.trip_date_range.end - workspace.trip_date_range.start).days + 1
            task = TripTaskSpec(
                task_id=f"workspace:{workspace.workspace_id}:default",
                room_id=workspace.room_id,
                task_revision=workspace.current_task_spec_revision or 1,
                city=workspace.city,
                date_range=DateRange(start=workspace.trip_date_range.start, days=days),
            )
        task = with_system_constraints(task)
        constraints = await self._constraints(workspace)
        member_revisions = {item.constraint_id: item.revision for item in constraints}
        preview, candidate_stop_id = self._preview_revision(
            base,
            candidate,
            day_index=day_index,
            insert_after_stop_id=insert_after_stop_id,
            insert_before_stop_id=insert_before_stop_id,
        )
        base_place_ids = sorted({stop.place_id for day in base.days for stop in day.stops})
        base_records = await self.audit_repository.load_place_records(
            workspace.workspace_id,
            base_place_ids,
            target_itinerary_revision=base.revision,
        )
        baseline_observations = self.evidence_service.observations_from_revision(
            base,
            base_records,
            now=now,
        )
        baseline_snapshot = self.evidence_service.create_snapshot(
            workspace_id=workspace.workspace_id,
            itinerary_revision=base.revision,
            observations=baseline_observations,
            now=now,
        )
        baseline_report = self.engine.run(
            run_input=AuditRunInput(
                workspace_id=workspace.workspace_id,
                itinerary_revision=base.revision,
                task_id=task.task_id,
                task_revision=task.task_revision,
                member_constraint_revision_set=member_revisions,
                member_constraints=constraints,
                place_resolution_versions={
                    stop.place_id: 1 for day in base.days for stop in day.stops
                },
            ),
            revision=base,
            task_spec=task,
            evidence_snapshot=baseline_snapshot,
            now=now,
        )
        baseline_fingerprints = {
            _finding_delta_fingerprint(finding)
            for finding in baseline_report.findings
            if finding.status is not AuditStatus.SATISFIED
        }
        records = dict(base_records)
        receipt = candidate.provider_receipt
        records[candidate.canonical_place.place_id] = {
            "name": receipt.name,
            "city": receipt.city,
            "district": receipt.district,
            "address": receipt.address,
            "coords": {"lng": receipt.longitude, "lat": receipt.latitude},
            "category": receipt.category or candidate.canonical_place.category,
            "provider": receipt.provider,
            "retrieval_observed_at": receipt.observed_at,
            "retrieval_confidence": 1.0,
        }
        observations = self.evidence_service.observations_from_revision(preview, records, now=now)
        observations.extend(self._candidate_fact_observations(candidate))
        observations.extend(self._route_observations(
            candidate,
            candidate_stop_id=candidate_stop_id,
            insert_after_stop_id=insert_after_stop_id,
            insert_before_stop_id=insert_before_stop_id,
        ))
        evidence_input_hash = sha256_canonical([
            item.model_dump(mode="json") for item in observations
        ])
        snapshot = self.evidence_service.create_snapshot(
            workspace_id=workspace.workspace_id,
            itinerary_revision=preview.revision,
            observations=observations,
            now=now,
        )
        report = self.engine.run(
            run_input=AuditRunInput(
                workspace_id=workspace.workspace_id,
                itinerary_revision=preview.revision,
                task_id=task.task_id,
                task_revision=task.task_revision,
                member_constraint_revision_set=member_revisions,
                member_constraints=constraints,
                place_resolution_versions={stop.place_id: 1 for day in preview.days for stop in day.stops},
            ),
            revision=preview,
            task_spec=task,
            evidence_snapshot=snapshot,
            now=now,
        )
        impacted: list[AuditFinding] = []
        for finding in report.findings:
            candidate_related = (
                candidate_stop_id in finding.affected_stop_ids
                or finding.rule_id == DirectAcceptCurrentFactRule.rule_id
                or finding.rule_id == "member.confirmed_hard_constraints"
            )
            if not candidate_related:
                continue
            # The Builder is progressive: a pre-existing incomplete-itinerary
            # finding remains in the report but cannot be misattributed to one
            # newly inserted candidate.  Candidate-specific safety rules are
            # always evaluated, even if a superficially similar base finding
            # existed already.
            always_candidate_specific = finding.rule_id in {
                "audit.place_city",
                "audit.route_gap",
                "audit.commitment_feasibility",
                "constraint.opening_hours",
                DirectAcceptCurrentFactRule.rule_id,
                LockedAnchorSuggestionRule.rule_id,
                "member.confirmed_hard_constraints",
            }
            inherited = (
                finding.status is not AuditStatus.SATISFIED
                and _finding_delta_fingerprint(
                    finding,
                    candidate_stop_id=candidate_stop_id,
                    candidate_place_id=candidate.canonical_place.place_id,
                )
                in baseline_fingerprints
            )
            if not inherited or always_candidate_specific:
                impacted.append(finding)
        blocking_violations = [
            item
            for item in impacted
            if item.status is AuditStatus.VIOLATED
            and item.severity in {AuditSeverity.BLOCKER, AuditSeverity.HIGH}
        ]
        unknowns = [item for item in impacted if item.status is AuditStatus.UNKNOWN]
        nonblocking_violations = [
            item
            for item in impacted
            if item.status is AuditStatus.VIOLATED
            and item.severity in {AuditSeverity.MEDIUM, AuditSeverity.LOW}
        ]
        # Audit status and risk severity are independent.  The frozen receipt's
        # status is the direct-accept decision, not a copy of the full report's
        # aggregate status: UNKNOWN always fails closed, while only
        # BLOCKER/HIGH violations are HARD blockers.  MEDIUM/LOW findings stay
        # in the receipt as actionable warnings and may influence ranking.
        if blocking_violations:
            status = AuditStatus.VIOLATED
        elif unknowns:
            status = AuditStatus.UNKNOWN
        else:
            status = AuditStatus.SATISFIED
        passed = status is AuditStatus.SATISFIED
        blocking_findings = [*blocking_violations, *unknowns]
        reasons = sorted({item.reason_code for item in blocking_findings})
        gated_material = candidate.model_copy(update={
            "hard_gate": HardGate(passed=passed, reason_codes=[] if passed else reasons or ["AUDIT_GATE_UNKNOWN"]),
            "classification": candidate.classification if passed else SuggestionClassification.INFEASIBLE,
            "explanation_codes": list(dict.fromkeys((
                *candidate.explanation_codes,
                "AUDIT_GATE_SATISFIED" if passed else f"AUDIT_GATE_{status.value}",
                *(
                    ("AUDIT_GATE_NONBLOCKING_WARNING",)
                    if nonblocking_violations
                    else ()
                ),
            ))),
        })
        authority_hash = _authority_hash(
            workspace=workspace,
            base=base,
            candidate=gated_material,
            task_id=task.task_id,
            task_revision=task.task_revision,
            task_workspace_revision=workspace.current_task_spec_revision,
            member_constraint_workspace_revision=workspace.current_member_constraint_revision,
            member_revision_set=member_revisions,
            evidence_input_hash=evidence_input_hash,
            audit_rule_set_version=self.engine.registry.rule_set_version,
        )
        gate = SuggestionAuditGateReceipt(
            status=status,
            task_id=task.task_id,
            task_revision=task.task_revision,
            task_workspace_revision=workspace.current_task_spec_revision,
            member_constraint_workspace_revision=workspace.current_member_constraint_revision,
            member_constraint_revision_set=member_revisions,
            evidence_snapshot_id=snapshot.snapshot_id,
            evidence_input_hash=evidence_input_hash,
            audit_report_input_hash=report.report_input_hash,
            audit_rule_set_version=self.engine.registry.rule_set_version,
            slot_policy_version=SLOT_POLICY_VERSION,
            authority_input_hash=authority_hash,
            findings=tuple(SuggestionGateFinding(
                rule_id=item.rule_id,
                rule_version=item.rule_version,
                status=item.status,
                severity=item.severity,
                reason_code=item.reason_code,
                affected_stop_ids=tuple(item.affected_stop_ids),
                evidence_fact_ids=tuple(item.evidence_fact_ids),
            ) for item in impacted),
            evaluated_at=now,
        )
        return gated_material.model_copy(update={
            "audit_gate": gate,
        })

    async def _constraints(self, workspace: TripWorkspace) -> list[MemberConstraint]:
        if self.member_repository is None or workspace.current_member_constraint_revision is None:
            return []
        return await self.member_repository.list_effective_constraints(
            workspace.workspace_id,
            workspace.current_member_constraint_revision,
        )

    @staticmethod
    def _candidate_fact_observations(candidate: SuggestionCandidateDraft) -> list[EvidenceObservation]:
        observations: list[EvidenceObservation] = []
        for fact in candidate.current_facts:
            observations.extend((
                EvidenceObservation(
                    subject_type="PLACE",
                    subject_id=candidate.canonical_place.place_id,
                    fact_type=fact.fact_type,
                    value=fact.value,
                    provider=fact.provider,
                    source_url=fact.source_url,
                    observed_at=fact.observed_at,
                    valid_until=fact.valid_until,
                    confidence=fact.confidence,
                ),
                EvidenceObservation(
                    subject_type="PLACE",
                    subject_id=candidate.canonical_place.place_id,
                    fact_type="PROVIDER_FACT_RECEIPT",
                    value={
                        "fact_type": fact.fact_type,
                        "request_hash": fact.request_hash,
                        "response_hash": fact.response_hash,
                        "execution_mode": fact.execution_mode.value,
                    },
                    provider=fact.provider,
                    source_url=fact.source_url,
                    observed_at=fact.observed_at,
                    valid_until=fact.valid_until,
                    confidence=fact.confidence,
                ),
            ))
        return observations

    @staticmethod
    def _route_observations(
        candidate: SuggestionCandidateDraft,
        *,
        candidate_stop_id: str,
        insert_after_stop_id: str | None,
        insert_before_stop_id: str | None,
    ) -> list[EvidenceObservation]:
        edge_by_leg = {
            RouteReceiptLeg.PREVIOUS_TO_CANDIDATE: (
                f"{insert_after_stop_id}->{candidate_stop_id}" if insert_after_stop_id else None
            ),
            RouteReceiptLeg.CANDIDATE_TO_NEXT: (
                f"{candidate_stop_id}->{insert_before_stop_id}" if insert_before_stop_id else None
            ),
        }
        return [EvidenceObservation(
            subject_type="ROUTE_EDGE",
            subject_id=edge_by_leg[receipt.leg],
            fact_type="ROUTE_TIME",
            value={"mode": receipt.transport_mode, "duration_minutes": receipt.duration_minutes},
            provider=receipt.provider,
            source_url=receipt.source_url,
            observed_at=receipt.observed_at,
            valid_until=receipt.observed_at + timedelta(seconds=receipt.max_age_seconds),
            confidence=1.0,
        ) for receipt in candidate.route_delta.route_receipts
        if receipt.leg in edge_by_leg and edge_by_leg[receipt.leg] is not None]

    @staticmethod
    def _preview_revision(
        base: ItineraryRevision,
        candidate: SuggestionCandidateDraft,
        *,
        day_index: int,
        insert_after_stop_id: str | None,
        insert_before_stop_id: str | None,
    ) -> tuple[ItineraryRevision, str]:
        if day_index >= len(base.days):
            raise ValueError("suggestion day is outside the itinerary")
        day = base.days[day_index]
        ids = [stop.stop_id for stop in day.stops]
        after_index = ids.index(insert_after_stop_id) if insert_after_stop_id else None
        before_index = ids.index(insert_before_stop_id) if insert_before_stop_id else None
        if after_index is not None and before_index is not None and before_index != after_index + 1:
            raise ValueError("suggestion anchors do not form one edge")
        insert_index = before_index if before_index is not None else (after_index + 1 if after_index is not None else len(ids))
        stop_id = f"suggestion-preview-{candidate.candidate_id}"
        receipts = {item.leg: item for item in candidate.route_delta.route_receipts}
        after = day.stops[after_index] if after_index is not None else None
        before = day.stops[before_index] if before_index is not None else None
        start, end = candidate_slot_times(after, before, receipts)
        inserted = ItineraryStop(
            stop_id=stop_id,
            place_id=candidate.canonical_place.place_id,
            day_index=day_index,
            order_index=insert_index,
            start_time=start,
            end_time=end,
            visit_duration_minutes=(
                (_minutes(end) - _minutes(start)) if start and end else None
            ),
            raw_name=candidate.canonical_place.name,
            category=candidate.canonical_place.category,
        )
        stops = list(day.stops)
        if after_index is not None:
            leg = receipts.get(RouteReceiptLeg.PREVIOUS_TO_CANDIDATE)
            if leg:
                stops[after_index] = stops[after_index].model_copy(update={
                    "transport_to_next": RevisionTransport(mode=leg.transport_mode, duration_minutes=leg.duration_minutes),
                })
        next_leg = receipts.get(RouteReceiptLeg.CANDIDATE_TO_NEXT)
        if next_leg:
            inserted = inserted.model_copy(update={
                "transport_to_next": RevisionTransport(mode=next_leg.transport_mode, duration_minutes=next_leg.duration_minutes),
            })
        stops.insert(insert_index, inserted)
        normalized = [stop.model_copy(update={"order_index": index}) for index, stop in enumerate(stops)]
        days = [item.model_copy(update={"stops": normalized}) if item.day_index == day_index else item for item in base.days]
        preview = with_content_hash(ItineraryRevisionContent(
            itinerary_id=base.itinerary_id,
            workspace_id=base.workspace_id,
            revision=base.revision + 1,
            parent_revision=base.revision,
            source_type=RevisionSource.PLANNER,
            city=base.city,
            date_range=base.date_range,
            days=days,
            locked_commitments=base.locked_commitments,
            change_summary={
                "operation": "SUGGESTION_GATE_PREVIEW",
                "candidate_stop_id": stop_id,
                "candidate_place_id": candidate.canonical_place.place_id,
                "insert_after_stop_id": insert_after_stop_id,
                "insert_before_stop_id": insert_before_stop_id,
                "slot_policy_version": SLOT_POLICY_VERSION,
            },
            created_by="suggestion-audit-gate",
        ))
        return preview, stop_id
