"""Controlled-snapshot adapters for the P5 Legacy/Core/Solver comparison."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from app.agents.graph import build_graph
from app.agents.nodes.task_parser import parse_task_spec
from app.agents.planner.graph import run_planner
from app.agents.state import default_working_context
from app.repairs.strategies import (
    BoundedRepairStrategy,
    CpSatRepairStrategy,
    RepairProblem,
    RepairProblemStop,
    StrategyStatus,
    execute_strategy,
)
from app.schemas.place import (
    Coordinates,
    Place,
    PlaceCategory,
    PlaceSource,
    RetrievalExecutionMode,
)
from evals.trip_check_v1.p5.contracts import (
    P5AdapterInput,
    P5AdapterResult,
    P5VariantRunSpec,
    TerminalStatus,
)
from evals.trip_check_v1.pilot_runner import (
    _PLACE_IDENTITIES,
    execute_case as execute_core_case,
)


def _raw_text(adapter_input: P5AdapterInput) -> str:
    key = "raw_text" if adapter_input.input_kind == "TEXT" else "ocr_text"
    value = adapter_input.product_input.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing controlled product text: {key}")
    return value


def _fixture_profile(adapter_input: P5AdapterInput) -> str:
    return (
        "ambiguous_alias_fixture_v1"
        if adapter_input.runner_control.get("fault_profile_id") == "empty_candidate_set"
        else "amap_mock_v1"
    )


def _controlled_places(adapter_input: P5AdapterInput) -> list[Place]:
    text = _raw_text(adapter_input)
    selected: dict[str, tuple[str, str, float, float]] = {}
    # Prefer the longest alias so ``故宫博物院`` is not reduced to ``故宫``.
    for name in sorted(_PLACE_IDENTITIES, key=len, reverse=True):
        identity = _PLACE_IDENTITIES[name]
        if name in text and identity[1] == adapter_input.city:
            selected.setdefault(identity[0], (name, *identity[1:]))
    places = []
    for place_id, (name, city, lng, lat) in selected.items():
        places.append(
            Place(
                place_id=place_id,
                name=name,
                category=PlaceCategory.ATTRACTION,
                address="controlled-fixture",
                coords=Coordinates(lng=lng, lat=lat),
                city=city,
                source=PlaceSource.AMAP_POI,
                execution_mode=RetrievalExecutionMode.FIXTURE,
                retrieval_provider="trip-check-p5-controlled-snapshot-v1",
                retrieval_request_hash=adapter_input.normalized_input_sha256,
                retrieval_response_hash=adapter_input.normalized_input_sha256,
                opening_hours="07:00-22:00",
            )
        )
    return places


def _base_capabilities(adapter_input: P5AdapterInput) -> dict[str, str]:
    return {
        "input_stage": (
            "NATIVE_TEXT"
            if adapter_input.input_kind == "TEXT"
            else "POST_OCR_CONTROLLED_TEXT"
        ),
        "external_api_calls": "0",
        "authoritative_oracle_access": "DENIED",
    }


def _stable_verification(report: Any) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "overall_status": report.overall_status.value,
        "checks": [
            {
                "rule_id": item.rule_id,
                "status": item.status.value,
                "repairable": item.repairable,
            }
            for item in report.checks
        ],
    }


class LegacyAdapter:
    """Frozen recommendation graph + independent Planner, with no persistence."""

    variant_id = "legacy_a"
    adapter_version = "legacy-a-v1"
    repair_strategy = "legacy_native_only"

    async def execute(
        self,
        adapter_input: P5AdapterInput,
        run_spec: P5VariantRunSpec,
    ) -> P5AdapterResult:
        capabilities = _base_capabilities(adapter_input)
        capabilities.update(
            {
                "authoritative_revision_write": "DENIED",
                "native_audit_advice_repair_postcheck": "UNSUPPORTED",
                "recommendation_graph": "SUPPORTED",
                "planner": "SUPPORTED",
            }
        )
        fault_profile = str(adapter_input.runner_control.get("fault_profile_id", "none"))
        if fault_profile in {
            "empty_candidate_set",
            "candidate_receipt_missing",
            "duplicate_apply",
            "concurrent_apply",
            "solver_unsat",
            "solver_timeout",
            "solver_fallback",
        }:
            return P5AdapterResult(
                terminal_status=TerminalStatus.UNSUPPORTED_CAPABILITY,
                capability_outcomes=capabilities,
                error_category=f"LEGACY_UNSUPPORTED_{fault_profile.upper()}",
            )

        places = _controlled_places(adapter_input)
        if len(places) < 2:
            return P5AdapterResult(
                terminal_status=TerminalStatus.UNSUPPORTED_CAPABILITY,
                capability_outcomes=capabilities,
                error_category="LEGACY_CONTROLLED_PLACE_SET_INSUFFICIENT",
            )
        raw_text = _raw_text(adapter_input)
        initial_state = {
            "messages": [HumanMessage(content=raw_text)],
            "thread_id": f"p5-{adapter_input.case_id}",
            "user_id": "p5-controlled-runner",
            "long_term_memory_enabled": False,
            "room_id": None,
            "trace_id": adapter_input.case_id,
            "deadline_monotonic": time.monotonic() + float(run_spec.budget["timeout_seconds"]),
            "trip_city": adapter_input.city,
            "trip_district": None,
            "intent": None,
            "query_rewrite": None,
            "react_iterations": 1,
            "routing_signals": [],
            "recommendation_plan": None,
            "slot_coverage": {},
            "missing_slot_ids": [],
            "search_anchor": None,
            "search_radius_m": None,
            "search_typecodes": [],
            "working_context": default_working_context(),
            "user_long_term_prefs": None,
            "amap_places": places,
            "eligible_amap_places": places,
            "eligible_candidates_computed": True,
            "rag_chunks": [],
            "citations": [],
            "tool_failures": [],
            "tool_receipts": [],
            "retrieval_audits": [],
            "retrieval_snapshots": [],
            "synthesized_places": [],
            "final_response": None,
            "recommendations": [],
            "itinerary": None,
            "selected_place_ids": [],
            "critic_retry": False,
            "critic_reason": None,
            "critic_iterations": 0,
            "critic_exhausted": False,
        }

        async def controlled_matrix(session, matrix_places):
            del session
            return {
                (left.place_id, right.place_id): (20, 3.0)
                for left in matrix_places
                for right in matrix_places
                if left.place_id != right.place_id
            }

        async def no_place_meta(place_ids):
            del place_ids
            return {}

        # Explicit patches are the zero-API controlled-snapshot boundary. They
        # also prevent local DB/Redis metadata lookups from becoming side effects.
        with (
            patch("app.agents.nodes.router._get_llm_with_tools", return_value=None),
            patch("app.agents.nodes.synthesizer._get_llm", return_value=None),
            patch(
                "app.agents.planner.nodes.distance._build_time_matrix",
                new=controlled_matrix,
            ),
            patch(
                "app.agents.planner.nodes.scheduler_v2._load_place_meta",
                new=no_place_meta,
            ),
        ):
            recommendation_state = await build_graph(None).ainvoke(initial_state)
            if recommendation_state.get("critic_exhausted"):
                return P5AdapterResult(
                    terminal_status=TerminalStatus.ERROR,
                    capability_outcomes=capabilities,
                    native_output={"critic_exhausted": True},
                    error_category="LEGACY_CRITIC_EXHAUSTED",
                )
            synthesized_places = recommendation_state.get("synthesized_places", [])
            task_spec = parse_task_spec(
                raw_text,
                room_id=f"p5-{adapter_input.case_id}",
                default_city=adapter_input.city,
                default_days=adapter_input.trip_days,
            ).task_spec
            planner = await run_planner(
                synthesized_places,
                adapter_input.trip_days,
                f"p5-{adapter_input.case_id}",
                task_spec=task_spec,
                planning_input_hash=adapter_input.normalized_input_sha256,
                defer_tips=True,
            )

        itinerary = {
            "city": planner.itinerary.city,
            "days": [
                {
                    "day_index": day.day_index,
                    "slots": [
                        {
                            "place_id": slot.place_id,
                            "place_name": slot.place.name,
                            "start_time": slot.start_time,
                            "end_time": slot.end_time,
                        }
                        for slot in day.slots
                    ],
                }
                for day in planner.itinerary.days
            ],
        }
        verification = _stable_verification(planner.verification_report)
        status = (
            TerminalStatus.SUCCEEDED
            if verification and verification["overall_status"] == "SATISFIED"
            else TerminalStatus.NEEDS_USER_RESOLUTION
        )
        return P5AdapterResult(
            terminal_status=status,
            capability_outcomes=capabilities,
            native_output={
                "recommendation_text": recommendation_state.get("final_response"),
                "itinerary": itinerary,
                "verification": verification,
                "critic_exhausted": False,
            },
            evaluation_projection={
                "projection_authority": "READ_ONLY_EVALUATOR_NOT_LEGACY_NATIVE",
                "selected_place_ids": sorted(
                    slot["place_id"] for day in itinerary["days"] for slot in day["slots"]
                ),
            },
            raw_artifact={
                "recommendation_state": {
                    "final_response": recommendation_state.get("final_response"),
                    "critic_reason": recommendation_state.get("critic_reason"),
                },
                "itinerary": planner.itinerary.model_dump(mode="json"),
                "verification": (
                    planner.verification_report.model_dump(mode="json")
                    if planner.verification_report
                    else None
                ),
            },
            receipts=[
                {
                    "type": "legacy_isolation",
                    "persistence": False,
                    "long_term_memory": False,
                    "llm_calls": 0,
                    "provider_calls": 0,
                }
            ],
        )


class CoreAdapter:
    variant_id = "core_b"
    adapter_version = "core-b-v1"
    repair_strategy = "bounded_repair_v1"

    async def execute(
        self,
        adapter_input: P5AdapterInput,
        run_spec: P5VariantRunSpec,
    ) -> P5AdapterResult:
        surrogate = {
            "case_id": adapter_input.case_id,
            "city": adapter_input.city,
            "days": adapter_input.trip_days,
            "raw_text": _raw_text(adapter_input),
            "fixture_profile": _fixture_profile(adapter_input),
            # execute_core_case only reads this after all product artifacts have
            # been produced. It is a constant runner sentinel, never the oracle.
            "expected": {
                "required_reason_codes": [],
                "requires_user_resolution": False,
                "wrong_poi_auto_accept_max": 1_000_000,
                "repair_new_high_max": 1_000_000,
                "repair_new_unknown_max": 1_000_000,
            },
        }
        core = await execute_core_case(
            surrogate,
            commit_sha=run_spec.subject_commit,
            dataset_hash=run_spec.dataset_manifest_hash,
        )
        if core["requires_user_resolution"]:
            status = TerminalStatus.NEEDS_USER_RESOLUTION
        elif core["terminal_state"] == "SUCCEEDED":
            status = TerminalStatus.SUCCEEDED
        elif core["terminal_state"].startswith("WAITING"):
            status = TerminalStatus.NEEDS_USER_RESOLUTION
        else:
            status = TerminalStatus.ERROR
        artifacts = core["artifacts"]
        reports = artifacts.get("reports", [])
        finding_id_to_code = {
            finding["finding_id"]: finding["reason_code"]
            for report in reports
            for finding in report.get("findings", [])
        }
        findings = [
            {
                "reason_code": finding["reason_code"],
                "severity": finding["severity"],
                "status": finding["status"],
            }
            for report in reports
            for finding in report.get("findings", [])
        ]
        advice_bundle = artifacts.get("advice")
        advice = []
        if advice_bundle:
            advice = [
                {
                    "finding_reason_code": finding_id_to_code.get(action.get("finding_id")),
                    "action_type": action.get("action_type"),
                    "requires_user_confirmation": action.get("requires_user_confirmation"),
                    "has_repair": action.get("repair_id") is not None,
                }
                for action in advice_bundle.get("actions", [])
            ]
        return P5AdapterResult(
            terminal_status=status,
            capability_outcomes={
                **_base_capabilities(adapter_input),
                "authoritative_trip_check_chain": "SUPPORTED",
                "candidate_set_replacement": "NOT_WIRED_IN_EXECUTOR",
                "bounded_repair": "SUPPORTED_FOR_TIME_DUPLICATE_ROUTE_ONLY",
            },
            native_output={
                "terminal_state": core["terminal_state"],
                "requires_user_resolution": core["requires_user_resolution"],
                "observed_reason_codes": core["observed_reason_codes"],
                "wrong_poi_auto_accept_count": core["wrong_poi_auto_accept_count"],
                "repair_new_high_count": core["repair_new_high_count"],
                "repair_new_unknown_count": core["repair_new_unknown_count"],
                "replay_side_effect_counts_equal": artifacts.get("replay", {}).get(
                    "side_effect_counts_equal"
                ),
            },
            evaluation_projection={
                "finding_reason_codes": sorted({item["reason_code"] for item in findings}),
                "advice_action_count": len(advice),
            },
            findings=findings,
            advice=advice,
            postcheck={
                "report_count": len(reports),
                "new_high_count": core["repair_new_high_count"],
                "new_unknown_count": core["repair_new_unknown_count"],
                "replay_side_effect_counts_equal": artifacts.get("replay", {}).get(
                    "side_effect_counts_equal"
                ),
            },
            receipts=[
                {
                    "type": "controlled_core_execution",
                    "run_receipt_count": len(artifacts.get("receipts", [])),
                    "provider_receipt_count": len(artifacts.get("provider_receipts", [])),
                    "llm_calls": 0,
                    "external_api_calls": 0,
                }
            ],
            raw_artifact=artifacts,
        )


def _solver_problem(adapter_input: P5AdapterInput) -> RepairProblem:
    fault_profile_id = str(adapter_input.runner_control.get("fault_profile_id", "none"))
    stop_count = max(3, min(8, adapter_input.trip_days * 2))
    stops = []
    for index in range(stop_count):
        day_index = index % adapter_input.trip_days
        position = index // adapter_input.trip_days
        original_start = 9 * 60 + position * 120
        locked_start = None
        if fault_profile_id == "solver_unsat" and index < 2:
            day_index = 0
            original_start = 9 * 60
            locked_start = original_start
        stops.append(
            RepairProblemStop(
                stop_id=f"{adapter_input.case_id}:stop-{index:02d}",
                day_index=day_index,
                duration_minutes=90,
                earliest_start=8 * 60,
                latest_end=21 * 60,
                original_start=original_start,
                locked_start=locked_start,
            )
        )
    fault_profile = (
        "forced_timeout"
        if fault_profile_id == "solver_timeout"
        else "forced_exception"
        if fault_profile_id == "solver_fallback"
        else "none"
    )
    return RepairProblem(
        case_id=adapter_input.case_id,
        case_hash=adapter_input.normalized_input_sha256,
        city=adapter_input.city,
        day_count=adapter_input.trip_days,
        stops=tuple(stops),
        travel_minutes=20,
        evidence_ready=fault_profile_id
        not in {"empty_candidate_set", "candidate_receipt_missing"},
        fault_profile=fault_profile,
    )


class SolverAdapter:
    variant_id = "solver_c"
    adapter_version = "solver-c-v1"
    repair_strategy = "cp_sat_v1"

    async def execute(
        self,
        adapter_input: P5AdapterInput,
        run_spec: P5VariantRunSpec,
    ) -> P5AdapterResult:
        core_spec = run_spec.model_copy(
            update={
                "variant_id": "core_b",
                "adapter_version": "core-b-v1",
                "repair_strategy": "bounded_repair_v1",
            }
        )
        core_result = await CoreAdapter().execute(adapter_input, core_spec)
        problem = _solver_problem(adapter_input)
        execution = execute_strategy(
            CpSatRepairStrategy(),
            problem,
            timeout_ms=int(float(run_spec.budget["timeout_seconds"]) * 1000),
            fallback=BoundedRepairStrategy(),
        )
        primary = execution.primary.model_dump(mode="json")
        effective = execution.effective.model_dump(mode="json")
        fault_profile = str(adapter_input.runner_control.get("fault_profile_id", "none"))
        native_solver_applicable = fault_profile in {
            "route_conflict",
            "solver_unsat",
            "solver_timeout",
            "solver_fallback",
        }
        if not native_solver_applicable:
            solver_capability = "NOT_APPLICABLE"
            status = core_result.terminal_status
        elif execution.primary.status is StrategyStatus.SUCCESS:
            solver_capability = "SUPPORTED"
            status = core_result.terminal_status
        elif execution.primary.status is StrategyStatus.TIMEOUT:
            solver_capability = "TIMEOUT"
            status = TerminalStatus.TIMEOUT
        elif execution.primary.status is StrategyStatus.ERROR:
            solver_capability = "ERROR"
            status = TerminalStatus.ERROR
        else:
            solver_capability = "UNSAT"
            status = TerminalStatus.NEEDS_USER_RESOLUTION
        return P5AdapterResult(
            terminal_status=status,
            capability_outcomes={
                **core_result.capability_outcomes,
                "repair_provider": "CP_SAT_ISOLATED_SUBPROCESS",
                "cp_sat_native_scope": solver_capability,
                "candidate_place_replacement": "UNSUPPORTED",
                "unknown_fact_resolution": "UNSUPPORTED",
            },
            native_output={
                "core_shell": core_result.native_output,
                "solver_applicable": native_solver_applicable,
                "solver_primary": primary,
                "solver_effective": effective,
                "fallback_used": execution.receipt.fallback_status is not None,
                "p4_admission": "REJECT",
            },
            evaluation_projection={
                **core_result.evaluation_projection,
                "solver_primary_status": execution.primary.status.value,
                "solver_effective_status": execution.effective.status.value,
                "solver_schedule_count": len(execution.primary.schedule),
            },
            findings=core_result.findings,
            advice=core_result.advice,
            postcheck={
                **(core_result.postcheck or {}),
                "solver_projection_only": True,
                "solver_primary_status": execution.primary.status.value,
            },
            receipts=[
                *core_result.receipts,
                {
                    **execution.receipt.model_dump(mode="json"),
                    # duration is measured but deliberately excluded from the
                    # semantic projection by keeping it in receipts.
                },
            ],
            raw_artifact={
                "core": core_result.raw_artifact,
                "problem": problem.model_dump(mode="json"),
                "strategy_execution": execution.model_dump(mode="json"),
            },
            error_category=(
                None
                if status not in {TerminalStatus.ERROR, TerminalStatus.TIMEOUT}
                else execution.primary.failure_reason
            ),
        )


ADAPTERS = {
    "legacy_a": LegacyAdapter,
    "core_b": CoreAdapter,
    "solver_c": SolverAdapter,
}
