from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TypedDict
from uuid import NAMESPACE_URL, uuid5

from langgraph.graph import END, START, StateGraph

from app.audit.evidence_service import EvidenceObservation
from app.audit.models import AuditReport, AuditStatus, EvidenceFreshness, EvidenceSnapshot, ProviderFailure
from app.audit.repositories import AuditRepository
from app.audit.service import AuditApplicationService
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import ItineraryRevision
from app.itineraries.repositories import ItineraryRepository
from app.operations.repositories import CreationCommandRepository
from app.repairs.candidates import FrozenRepairCandidateSet, candidate_binding_for_option
from app.repairs.errors import RepairNoFeasibleOptionError
from app.repairs.models import RepairOption
from app.repairs.models import RepairApplyResult
from app.repairs.objective import introduces_new_high_violation, new_unknown_count
from app.repairs.search import BoundedRepairSearch
from app.trip_check.advice import AdviceRepository
from app.trip_check.briefs import TripBriefRepository
from app.trip_check.models import (
    AdviceAction,
    AdviceBundle,
    RunPartialFailure,
    SideEffectReceipt,
    TripCheckRun,
    TripCheckPostcheckLineage,
    TripCheckRunStatus,
    TripCheckStage,
)
from app.trip_check.runs import TripCheckRunRepository
from app.trip_check.trace import TripCheckTelemetry
from app.trip_check.provider_integrity import TripCheckProviderIntegrityCollector


class TripCheckExecutionState(TypedDict):
    run_id: str
    lease_owner: str
    stage: str
    terminated_after_evidence: bool


class ControlledProviderCollectionResult(TypedDict):
    observations: list[EvidenceObservation]
    provider_failures: list[ProviderFailure]
    partial_failures: list[RunPartialFailure]
    provider_attempt_count: int
    provider_receipts: list[dict]


def _stage_side_effect_key(run: TripCheckRun, stage: TripCheckStage) -> str:
    return f"trip-check:{run.run_id}:{stage.value}:{run.config_hash}"


def _stage_input_hash(run: TripCheckRun) -> str:
    return sha256_canonical(
        {
            "run_id": run.run_id,
            "stage": run.stage.value,
            "stage_attempt": run.stage_attempt,
            "config_hash": run.config_hash,
            "itinerary_revision": run.itinerary_revision,
            "brief_revision": run.brief_revision,
            "evidence_snapshot_id": run.evidence_snapshot_id,
            "report_id": run.report_id,
            "advice_bundle_id": run.advice_bundle_id,
        }
    )


def _route_duration(left_name: str, right_name: str) -> int:
    text = f"{left_name} {right_name}"
    if any(token in text for token in ("八达岭", "迪士尼", "西溪湿地")):
        return 90
    if "天坛" in text and "故宫" in text:
        return 35
    if "灵隐寺" in text and "西湖" in text:
        return 30
    return 25


def _controlled_route_observation(left, right, *, observed_at: datetime) -> EvidenceObservation:
    mode = left.transport_to_next.mode if left.transport_to_next else "driving"
    return EvidenceObservation(
        subject_type="ROUTE_EDGE",
        subject_id=f"{left.stop_id}->{right.stop_id}",
        fact_type="ROUTE_TIME",
        value={
            "mode": mode,
            "duration_minutes": _route_duration(
                left.raw_name or left.place_id,
                right.raw_name or right.place_id,
            ),
        },
        provider="controlled_route_fixture_v1",
        observed_at=observed_at,
        valid_until=observed_at + timedelta(days=365),
        confidence=1.0,
    )


def controlled_route_observations(
    revision: ItineraryRevision,
    *,
    observed_at: datetime,
) -> list[EvidenceObservation]:
    """Build deterministic route facts for P1 fixture runs only."""

    observations: list[EvidenceObservation] = []
    for day in revision.days:
        for left, right in zip(day.stops, day.stops[1:]):
            observations.append(_controlled_route_observation(left, right, observed_at=observed_at))
    return observations


def _unavailable_route_observation(
    left,
    right,
    *,
    observed_at: datetime,
    category: str,
) -> EvidenceObservation:
    mode = left.transport_to_next.mode if left.transport_to_next else "driving"
    return EvidenceObservation(
        subject_type="ROUTE_EDGE",
        subject_id=f"{left.stop_id}->{right.stop_id}",
        fact_type="ROUTE_TIME",
        value={"mode": mode, "duration_minutes": None, "reason_code": category},
        provider="controlled_route_fixture_v2",
        observed_at=observed_at,
        confidence=0,
        freshness_status=EvidenceFreshness.UNAVAILABLE,
    )


def confirmed_brief_observations(run: TripCheckRun, traveler_count: int) -> list[EvidenceObservation]:
    return [
        EvidenceObservation(
            subject_type="TRIP_BRIEF",
            subject_id=run.brief_id,
            fact_type="TRAVELER_COUNT",
            value=traveler_count,
            provider="confirmed_trip_brief_revision",
            observed_at=run.created_at,
            valid_until=run.created_at + timedelta(days=3650),
            confidence=1.0,
        )
    ]


def build_advice_bundle(
    *,
    run: TripCheckRun,
    report: AuditReport,
    snapshot: EvidenceSnapshot,
    repairs: list[RepairOption],
    evidence_receipt_id: str,
    candidate_sets: dict[str, FrozenRepairCandidateSet] | None = None,
) -> AdviceBundle:
    candidate_sets = candidate_sets or {}
    repair_by_finding = {
        finding_id: option
        for option in repairs
        for finding_id in option.targeted_finding_ids
    }
    actions: list[AdviceAction] = []
    for finding in report.findings:
        if finding.status == AuditStatus.SATISFIED:
            continue
        option = repair_by_finding.get(finding.finding_id)
        candidate_binding = (
            candidate_binding_for_option(option, candidate_sets)
            if option is not None
            else None
        )
        if option is not None:
            operation_summary = "；".join(item.rationale for item in option.operations)
            action_text = operation_summary or "应用经过预览和 postcheck 的修复选项"
            expected_impact = "创建新行程 revision，并以完整 postcheck 重新判断该问题"
            uncertainty = "仅使用本次受控 Evidence；采纳前仍应核对时间调整是否符合同行人安排"
        else:
            action_text = finding.confirmation_action or "补充该项事实后重新运行核验"
            expected_impact = "补齐不确定事实或解除当前冲突，不会把 UNKNOWN 直接视为通过"
            uncertainty = "当前没有通过安全门禁的自动修复，系统不会代替用户确认"
        actions.append(
            AdviceAction(
                advice_id=str(uuid5(NAMESPACE_URL, f"breezetravel:{run.run_id}:advice:{finding.finding_id}")),
                finding_id=finding.finding_id,
                action=action_text,
                expected_impact=expected_impact,
                uncertainty=uncertainty,
                candidate_set_id=(
                    candidate_binding.candidate_set_id
                    if candidate_binding is not None
                    else None
                ),
                evidence_fact_ids=finding.evidence_fact_ids,
                provider_receipt_ids=[
                    evidence_receipt_id,
                    *(candidate_binding.receipt_ids if candidate_binding is not None else ()),
                ],
                route_delta=(
                    {"duration_delta_minutes": option.route_cost_delta}
                    if option is not None and option.route_cost_delta is not None
                    else None
                ),
                repair_id=option.repair_id if option is not None else None,
                tradeoffs=option.tradeoffs if option is not None else [],
            )
        )
    bundle = AdviceBundle(
        advice_bundle_id=str(uuid5(NAMESPACE_URL, f"breezetravel:{run.run_id}:advice-bundle")),
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        report_id=report.report_id,
        itinerary_revision=run.itinerary_revision,
        brief_revision=run.brief_revision,
        evidence_snapshot_id=snapshot.snapshot_id,
        actions=actions,
        created_at=run.created_at,
    )
    validate_advice_bundle_contract(bundle, report=report, snapshot=snapshot)
    return bundle


def validate_advice_bundle_contract(
    bundle: AdviceBundle,
    *,
    report: AuditReport,
    snapshot: EvidenceSnapshot,
) -> None:
    expected = {
        finding.finding_id
        for finding in report.findings
        if finding.status != AuditStatus.SATISFIED
    }
    actual = [action.finding_id for action in bundle.actions]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("Advice must bind exactly once to every non-PASS Finding")
    snapshot_fact_ids = {fact.fact_id for fact in snapshot.facts}
    for action in bundle.actions:
        if not set(action.evidence_fact_ids) <= snapshot_fact_ids:
            raise ValueError("Advice references evidence outside its frozen snapshot")
        if not action.provider_receipt_ids:
            raise ValueError("Advice must retain at least one provider/stage receipt")
        if action.candidate_set_id is not None:
            if action.repair_id is None or len(action.provider_receipt_ids) < 3:
                raise ValueError("candidate Advice requires repair, place and route receipts")


class TripCheckExecutor:
    """Thin LangGraph orchestration over PostgreSQL-authoritative domain services."""

    def __init__(
        self,
        *,
        run_repository: TripCheckRunRepository,
        itinerary_repository: ItineraryRepository,
        audit_repository: AuditRepository,
        advice_repository: AdviceRepository,
        brief_repository: TripBriefRepository,
        repair_search: BoundedRepairSearch,
        command_repository: CreationCommandRepository,
        telemetry: TripCheckTelemetry | None = None,
        provider_integrity_collector: TripCheckProviderIntegrityCollector | None = None,
    ):
        self.run_repository = run_repository
        self.itinerary_repository = itinerary_repository
        self.audit_repository = audit_repository
        self.advice_repository = advice_repository
        self.brief_repository = brief_repository
        self.repair_search = repair_search
        self.command_repository = command_repository
        self.telemetry = telemetry or TripCheckTelemetry()
        self.provider_integrity_collector = provider_integrity_collector or TripCheckProviderIntegrityCollector()
        self.audit_service = AuditApplicationService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
        )
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(TripCheckExecutionState)
        graph.add_node("collect_evidence", self._collect_evidence_traced)
        graph.add_node("audit", self._audit_traced)
        graph.add_node("build_advice", self._build_advice_traced)
        destinations = {
            TripCheckStage.COLLECT_EVIDENCE.value: "collect_evidence",
            TripCheckStage.AUDIT.value: "audit",
            TripCheckStage.BUILD_ADVICE.value: "build_advice",
            TripCheckStage.WAIT_ADOPTION.value: END,
            TripCheckStage.POSTCHECK.value: END,
        }
        graph.add_conditional_edges(START, self._route_stage, destinations)
        graph.add_conditional_edges("collect_evidence", self._route_stage, destinations)
        graph.add_conditional_edges("audit", self._route_stage, destinations)
        graph.add_conditional_edges("build_advice", self._route_stage, destinations)
        return graph.compile()

    @staticmethod
    def _route_stage(state: TripCheckExecutionState) -> str:
        if state.get("terminated_after_evidence"):
            return TripCheckStage.POSTCHECK.value
        return state["stage"]

    async def execute(self, run_id: str, *, lease_owner: str | None = None) -> TripCheckRun:
        run = await self.run_repository.get_run(run_id)
        if run is None:
            from app.itineraries.errors import ResourceNotFound

            raise ResourceNotFound("trip check run does not exist")
        if run.stage in {TripCheckStage.WAIT_ADOPTION, TripCheckStage.POSTCHECK}:
            return run
        if lease_owner is None:
            run = await self.run_repository.claim_for_execution(run_id, now=datetime.now(timezone.utc))
            lease_owner = run.lease_owner
        if not lease_owner or run.lease_owner != lease_owner:
            from app.trip_check.errors import TripCheckRunConflictError

            raise TripCheckRunConflictError("trip check executor does not own the active lease")
        with self.telemetry.run_span(run) as span:
            try:
                await self.graph.ainvoke(
                    TripCheckExecutionState(
                        run_id=run_id,
                        lease_owner=lease_owner,
                        stage=run.stage.value,
                        terminated_after_evidence=False,
                    )
                )
            except Exception as exc:
                self.telemetry.mark_failure(span, type(exc).__name__)
                await self.run_repository.fail_stage(
                    run_id,
                    lease_owner=lease_owner,
                    category=type(exc).__name__,
                    now=datetime.now(timezone.utc),
                )
                raise
        final = await self.run_repository.get_run(run_id)
        if final is None:
            raise RuntimeError("trip check run disappeared after execution")
        return final

    async def _trace_stage(self, state: TripCheckExecutionState, handler) -> TripCheckExecutionState:
        run = await self.run_repository.get_run(state["run_id"])
        if run is None:
            raise RuntimeError("trip check run disappeared before tracing stage")
        with self.telemetry.stage_span(run) as span:
            try:
                return await handler(state)
            except Exception as exc:
                self.telemetry.mark_failure(span, type(exc).__name__)
                raise

    async def _collect_evidence_traced(self, state: TripCheckExecutionState) -> TripCheckExecutionState:
        return await self._trace_stage(state, self._collect_evidence)

    async def _audit_traced(self, state: TripCheckExecutionState) -> TripCheckExecutionState:
        return await self._trace_stage(state, self._audit)

    async def _build_advice_traced(self, state: TripCheckExecutionState) -> TripCheckExecutionState:
        return await self._trace_stage(state, self._build_advice)

    async def _start(self, state: TripCheckExecutionState) -> TripCheckRun:
        run = await self.run_repository.get_run(state["run_id"])
        if run is None:
            raise RuntimeError("trip check run disappeared before stage execution")
        return await self.run_repository.start_stage(
            run.run_id,
            lease_owner=state["lease_owner"],
            stage_input_hash=_stage_input_hash(run),
            now=datetime.now(timezone.utc),
        )

    async def _collect_controlled_provider_evidence(
        self,
        run: TripCheckRun,
        revision: ItineraryRevision,
    ) -> ControlledProviderCollectionResult:
        observations: list[EvidenceObservation] = []
        provider_failures: list[ProviderFailure] = []
        partial_failures: list[RunPartialFailure] = []
        provider_attempt_count = 0
        fault_injected = False
        for day in revision.days:
            for left, right in zip(day.stops, day.stops[1:]):
                edge_id = f"{left.stop_id}->{right.stop_id}"
                fault = run.run_spec.fault_profile if not fault_injected else "none"
                if fault == "provider_timeout":
                    attempts = 1 + min(run.run_spec.budget.max_retries, 2)
                    for attempt in range(1, attempts + 1):
                        provider_attempt_count += 1
                        with self.telemetry.provider_attempt_span(run, attempt=attempt) as span:
                            self.telemetry.mark_failure(span, "PROVIDER_TIMEOUT")
                    category = "PROVIDER_TIMEOUT"
                elif fault == "partial_field_failure":
                    provider_attempt_count += 1
                    with self.telemetry.provider_attempt_span(run, attempt=1) as span:
                        self.telemetry.mark_failure(span, "PROVIDER_PARTIAL_FIELD_FAILURE")
                    category = "PROVIDER_PARTIAL_FIELD_FAILURE"
                else:
                    provider_attempt_count += 1
                    with self.telemetry.provider_attempt_span(run, attempt=1):
                        pass
                    observations.append(_controlled_route_observation(left, right, observed_at=run.created_at))
                    continue
                fault_injected = True
                observations.append(
                    _unavailable_route_observation(
                        left,
                        right,
                        observed_at=run.created_at,
                        category=category,
                    )
                )
                provider_failures.append(
                    ProviderFailure(
                        provider="controlled_route_fixture_v2",
                        error_category=category,
                        retryable=False,
                        detail=f"controlled fault for route edge {edge_id}",
                    )
                )
                partial_failures.append(
                    RunPartialFailure(
                        stage=TripCheckStage.COLLECT_EVIDENCE,
                        provider="controlled_route_fixture_v2",
                        category=category,
                        affected_fields=[f"route_edges.{edge_id}.duration_minutes"],
                        retryable=False,
                    )
                )
        return {
            "observations": observations,
            "provider_failures": provider_failures,
            "partial_failures": partial_failures,
            "provider_attempt_count": provider_attempt_count,
            "provider_receipts": [],
        }

    async def _collect_evidence(self, state: TripCheckExecutionState) -> TripCheckExecutionState:
        run = await self._start(state)
        revision = await self.itinerary_repository.get_revision(run.workspace_id, run.itinerary_revision)
        if revision is None:
            raise RuntimeError("trip check itinerary revision is missing")
        brief = await self.brief_repository.get_brief(run.workspace_id, run.brief_revision)
        if brief is None or brief.brief_id != run.brief_id or brief.status.value != "CONFIRMED":
            raise RuntimeError("trip check confirmed Brief binding is missing")
        if run.run_spec.provider_version == "p3-provider-integrity-v1":
            place_ids = sorted({stop.place_id for day in revision.days for stop in day.stops})
            place_records = await self.audit_repository.load_place_records(
                run.workspace_id,
                place_ids,
                target_itinerary_revision=revision.revision,
            )
            collected = await self.provider_integrity_collector.collect(run, revision, place_records)
            provider_result: ControlledProviderCollectionResult = {
                "observations": collected.observations,
                "provider_failures": collected.provider_failures,
                "partial_failures": collected.partial_failures,
                "provider_attempt_count": collected.provider_attempt_count,
                "provider_receipts": [item.model_dump(mode="json") for item in collected.provider_receipts],
            }
        else:
            provider_result = await self._collect_controlled_provider_evidence(run, revision)
        observations = [
            *provider_result["observations"],
            *confirmed_brief_observations(run, brief.traveler_count),
        ]
        _, snapshot, _ = await self.audit_service.prepare_current_evidence(
            run.workspace_id,
            provider_failures=provider_result["provider_failures"],
            extra_observations=observations,
            snapshot_id=str(uuid5(NAMESPACE_URL, f"breezetravel:{run.run_id}:evidence")),
            now=run.created_at,
        )
        snapshot = await self.audit_repository.save_snapshot(snapshot)
        response_hash = sha256_canonical(snapshot.model_dump(mode="json"))
        receipt = SideEffectReceipt(
            receipt_id=str(uuid5(NAMESPACE_URL, f"breezetravel:{run.run_id}:evidence-receipt")),
            run_id=run.run_id,
            stage=TripCheckStage.COLLECT_EVIDENCE,
            side_effect_key=_stage_side_effect_key(run, TripCheckStage.COLLECT_EVIDENCE),
            effect_type=(
                "P3_PROVIDER_INTEGRITY_EVIDENCE"
                if run.run_spec.provider_version == "p3-provider-integrity-v1"
                else "CONTROLLED_FIXTURE_EVIDENCE"
            ),
            request_hash=sha256_canonical(
                {
                    "run_id": run.run_id,
                    "revision": run.itinerary_revision,
                    "config_hash": run.config_hash,
                    "route_observations": [item.model_dump(mode="json") for item in observations],
                }
            ),
            response_hash=response_hash,
            provider=(
                "trip_check_provider_integrity"
                if run.run_spec.provider_version == "p3-provider-integrity-v1"
                else "controlled_fixture"
            ),
            status="PARTIAL" if provider_result["partial_failures"] else "SUCCEEDED",
            receipt={
                "execution_mode": run.run_spec.execution_mode,
                "provider_version": run.run_spec.provider_version,
                "snapshot_id": snapshot.snapshot_id,
                "fact_count": len(snapshot.facts),
                "route_fact_count": sum(item.subject_type == "ROUTE_EDGE" for item in observations),
                "brief_fact_count": sum(item.subject_type == "TRIP_BRIEF" for item in observations),
                "route_option_fact_count": sum(item.subject_type == "ROUTE_OPTION" for item in observations),
                "weather_fact_count": sum(item.fact_type == "WEATHER" for item in observations),
                "risk_fact_count": sum(item.fact_type == "RISK_SOURCE" for item in observations),
                "provider_attempt_count": provider_result["provider_attempt_count"],
                "provider_receipts": provider_result["provider_receipts"],
                "failure_categories": [item.category for item in provider_result["partial_failures"]],
                "affected_fields": [
                    field
                    for item in provider_result["partial_failures"]
                    for field in item.affected_fields
                ],
            },
            created_at=run.created_at,
        )
        updated, _ = await self.run_repository.complete_stage(
            run.run_id,
            lease_owner=state["lease_owner"],
            expected_stage=TripCheckStage.COLLECT_EVIDENCE,
            next_stage=TripCheckStage.AUDIT,
            status=TripCheckRunStatus.RUNNING,
            receipt=receipt,
            evidence_snapshot_id=snapshot.snapshot_id,
            partial_failures=provider_result["partial_failures"],
            now=datetime.now(timezone.utc),
        )
        terminated = run.run_spec.fault_profile == "terminate_after_evidence"
        return {
            **state,
            "stage": updated.stage.value,
            "terminated_after_evidence": terminated,
        }

    async def _audit(self, state: TripCheckExecutionState) -> TripCheckExecutionState:
        run = await self._start(state)
        if not run.evidence_snapshot_id:
            raise RuntimeError("Audit stage has no Evidence snapshot binding")
        snapshot = await self.audit_repository.get_snapshot(run.evidence_snapshot_id)
        if snapshot is None:
            raise RuntimeError("Audit stage Evidence snapshot is missing")
        report, _ = await self.audit_service.prepare_report_from_snapshot(
            run.workspace_id,
            snapshot,
            now=run.created_at,
        )
        report = await self.audit_repository.save_report(report)
        response_hash = sha256_canonical(report.model_dump(mode="json"))
        receipt = SideEffectReceipt(
            receipt_id=str(uuid5(NAMESPACE_URL, f"breezetravel:{run.run_id}:audit-receipt")),
            run_id=run.run_id,
            stage=TripCheckStage.AUDIT,
            side_effect_key=_stage_side_effect_key(run, TripCheckStage.AUDIT),
            effect_type="AUTHORITATIVE_AUDIT",
            request_hash=sha256_canonical(
                {
                    "run_id": run.run_id,
                    "snapshot_id": snapshot.snapshot_id,
                    "rule_set_version": run.run_spec.rule_set_version,
                }
            ),
            response_hash=response_hash,
            provider=None,
            status="SUCCEEDED",
            receipt={
                "report_id": report.report_id,
                "report_input_hash": report.report_input_hash,
                "finding_count": len(report.findings),
                "overall_status": report.overall_status.value,
            },
            created_at=run.created_at,
        )
        updated, _ = await self.run_repository.complete_stage(
            run.run_id,
            lease_owner=state["lease_owner"],
            expected_stage=TripCheckStage.AUDIT,
            next_stage=TripCheckStage.BUILD_ADVICE,
            status=TripCheckRunStatus.RUNNING,
            receipt=receipt,
            report_id=report.report_id,
            now=datetime.now(timezone.utc),
        )
        return {**state, "stage": updated.stage.value}


    async def _build_advice(self, state: TripCheckExecutionState) -> TripCheckExecutionState:
        run = await self._start(state)
        if not run.report_id or not run.evidence_snapshot_id:
            raise RuntimeError("Advice stage lacks Audit or Evidence binding")
        report = await self.audit_repository.get_report(run.report_id)
        snapshot = await self.audit_repository.get_snapshot(run.evidence_snapshot_id)
        if report is None or snapshot is None:
            raise RuntimeError("Advice stage inputs are missing")
        repairs: list[RepairOption] = []
        if any(
            finding.status == AuditStatus.VIOLATED
            and finding.repairable
            and finding.severity.value in {"BLOCKER", "HIGH"}
            for finding in report.findings
        ):
            try:
                repairs, _ = await self.repair_search.propose_idempotent(
                    report.report_id,
                    actor_user_id=run.created_by,
                    idempotency_key=f"trip-check:{run.run_id}:propose-repairs",
                    command_repository=self.command_repository,
                    now=run.created_at,
                )
            except RepairNoFeasibleOptionError:
                repairs = []
        evidence_receipt = await self.run_repository.get_receipt(
            run.run_id,
            _stage_side_effect_key(run, TripCheckStage.COLLECT_EVIDENCE),
        )
        if evidence_receipt is None:
            raise RuntimeError("Advice stage cannot trace the Evidence receipt")
        bundle = build_advice_bundle(
            run=run,
            report=report,
            snapshot=snapshot,
            repairs=repairs,
            evidence_receipt_id=evidence_receipt.receipt_id,
        )
        bundle = await self.advice_repository.save_bundle(bundle, brief_id=run.brief_id)
        response_hash = sha256_canonical(bundle.model_dump(mode="json"))
        receipt = SideEffectReceipt(
            receipt_id=str(uuid5(NAMESPACE_URL, f"breezetravel:{run.run_id}:advice-receipt")),
            run_id=run.run_id,
            stage=TripCheckStage.BUILD_ADVICE,
            side_effect_key=_stage_side_effect_key(run, TripCheckStage.BUILD_ADVICE),
            effect_type="DETERMINISTIC_ADVICE",
            request_hash=sha256_canonical(
                {
                    "run_id": run.run_id,
                    "report_id": report.report_id,
                    "repair_ids": [item.repair_id for item in repairs],
                }
            ),
            response_hash=response_hash,
            provider=None,
            status="SUCCEEDED",
            receipt={
                "advice_bundle_id": bundle.advice_bundle_id,
                "action_count": len(bundle.actions),
                "repair_ids": [item.repair_id for item in repairs],
            },
            created_at=run.created_at,
        )
        updated, _ = await self.run_repository.complete_stage(
            run.run_id,
            lease_owner=state["lease_owner"],
            expected_stage=TripCheckStage.BUILD_ADVICE,
            next_stage=TripCheckStage.WAIT_ADOPTION,
            status=(
                TripCheckRunStatus.PARTIAL
                if run.partial_failures
                else TripCheckRunStatus.WAITING
            ),
            receipt=receipt,
            advice_bundle_id=bundle.advice_bundle_id,
            now=datetime.now(timezone.utc),
        )
        return {**state, "stage": updated.stage.value}


class TripCheckAdoptionReconciler:
    """Bind an existing Repair/EditCommand result back to its waiting TripCheck run."""

    def __init__(
        self,
        *,
        run_repository: TripCheckRunRepository,
        audit_repository: AuditRepository,
        advice_repository: AdviceRepository,
    ):
        self.run_repository = run_repository
        self.audit_repository = audit_repository
        self.advice_repository = advice_repository

    async def reconcile(self, result: RepairApplyResult) -> TripCheckRun | None:
        bundle = await self.advice_repository.get_bundle_for_repair(result.repair.repair_id)
        if bundle is None:
            return None
        run = await self.run_repository.get_run(bundle.run_id)
        source = await self.audit_repository.get_report(bundle.report_id)
        postcheck = await self.audit_repository.get_report(result.postcheck_report_id)
        if run is None or source is None or postcheck is None:
            raise RuntimeError("TripCheck adoption lineage inputs are missing")
        if result.new_revision != postcheck.itinerary_revision:
            raise RuntimeError("TripCheck adoption result does not bind the postcheck revision")
        if introduces_new_high_violation(source, postcheck) or new_unknown_count(source, postcheck) > 0:
            from app.trip_check.errors import TripCheckRunConflictError

            raise TripCheckRunConflictError(
                "postcheck introduced a new HIGH or UNKNOWN finding",
                context={"run_id": run.run_id, "repair_id": result.repair.repair_id},
            )
        lineage = TripCheckPostcheckLineage(
            lineage_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"breezetravel:{run.run_id}:postcheck:{result.repair.repair_id}",
                )
            ),
            run_id=run.run_id,
            advice_bundle_id=bundle.advice_bundle_id,
            repair_id=result.repair.repair_id,
            source_report_id=source.report_id,
            source_itinerary_revision=source.itinerary_revision,
            result_itinerary_revision=result.new_revision,
            postcheck_report_id=postcheck.report_id,
            postcheck_snapshot_id=postcheck.evidence_snapshot_id,
            created_at=postcheck.created_at,
        )
        await self.advice_repository.save_postcheck_lineage(lineage)
        request_hash = sha256_canonical(
            {
                "run_id": run.run_id,
                "repair_id": result.repair.repair_id,
                "source_report_id": source.report_id,
                "result_revision": result.new_revision,
                "postcheck_report_id": postcheck.report_id,
                "postcheck_snapshot_id": postcheck.evidence_snapshot_id,
            }
        )
        response_hash = sha256_canonical(postcheck.model_dump(mode="json"))
        receipt = SideEffectReceipt(
            receipt_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"breezetravel:{run.run_id}:postcheck-receipt:{result.repair.repair_id}",
                )
            ),
            run_id=run.run_id,
            stage=TripCheckStage.POSTCHECK,
            side_effect_key=f"trip-check:{run.run_id}:POSTCHECK:{result.repair.repair_id}",
            effect_type="REPAIR_ADOPTION_POSTCHECK",
            request_hash=request_hash,
            response_hash=response_hash,
            provider=None,
            status="SUCCEEDED",
            receipt={
                "repair_id": result.repair.repair_id,
                "source_report_id": source.report_id,
                "result_itinerary_revision": result.new_revision,
                "postcheck_report_id": postcheck.report_id,
                "postcheck_snapshot_id": postcheck.evidence_snapshot_id,
                "new_high_count": 0,
                "new_unknown_count": 0,
            },
            created_at=postcheck.created_at,
        )
        completed, _ = await self.run_repository.complete_postcheck(
            run.run_id,
            repair_id=result.repair.repair_id,
            receipt=receipt,
            evidence_snapshot_id=postcheck.evidence_snapshot_id,
            report_id=postcheck.report_id,
            now=datetime.now(timezone.utc),
        )
        return completed
