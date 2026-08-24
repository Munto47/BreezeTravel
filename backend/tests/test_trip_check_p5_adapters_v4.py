from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from evals.trip_check_v1.p5.adapters_v2 import _HarnessResult
from evals.trip_check_v1.p5.adapters_v3 import (
    CoreAdapterV3,
    LegacyAdapterV3,
    SolverAdapterV3,
)
from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3, TerminalStatusV3
from evals.trip_check_v1.p5.data_contract import load_jsonl
from evals.trip_check_v1.p5.data_contract_v4 import (
    NONBLIND_MATERIALIZATIONS_PATH_V4,
    NONBLIND_PATH_V4,
    validate_materialization_v4,
)
from evals.trip_check_v1.p5.runner_v4 import (
    P5VariantRunSpecV4,
    execute_terminal_v4,
)
from evals.trip_check_v1.p5.scorer_v3 import _semantic_reason_codes_v3
from evals.trip_check_v1.p5.adapters_v4 import (
    ADAPTERS_V4,
    ADAPTER_VERSIONS_V4,
    CoreAdapterV4,
    LegacyAdapterV4,
    SolverAdapterV4,
)


def test_v4_adapters_only_version_the_formal_adapter_identity() -> None:
    assert issubclass(LegacyAdapterV4, LegacyAdapterV3)
    assert issubclass(CoreAdapterV4, CoreAdapterV3)
    assert issubclass(SolverAdapterV4, SolverAdapterV3)
    assert ADAPTER_VERSIONS_V4 == {
        "legacy_a": ("legacy-a-v4", "legacy_native_only"),
        "core_b": ("core-b-v4", "bounded_repair_v1"),
        "solver_c": ("solver-c-v4", "cp_sat_v1"),
    }


def test_v4_adapter_registry_is_exact_and_does_not_promote_solver() -> None:
    assert ADAPTERS_V4 == {
        "legacy_a": LegacyAdapterV4,
        "core_b": CoreAdapterV4,
        "solver_c": SolverAdapterV4,
    }
    assert ADAPTERS_V4["core_b"].repair_strategy == "bounded_repair_v1"
    assert ADAPTERS_V4["solver_c"].repair_strategy == "cp_sat_v1"


def _repaired_case() -> tuple[P5CaseV3, dict]:
    case_id = "p5.pilot.bj.004"
    case = next(row for row in load_jsonl(NONBLIND_PATH_V4) if row["case_id"] == case_id)
    materialization = next(
        row
        for row in load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V4)
        if row["case_id"] == case_id
    )
    return P5CaseV3.model_validate(case), materialization


def _run_spec() -> P5VariantRunSpecV4:
    payload = {
        "schema_version": "trip-check-p5-variant-run-spec-v4",
        "subject_commit": "a" * 40,
        "dirty_tree": False,
        "lane": "nonblind",
        "dataset_manifest_hash": "b" * 64,
        "case_set_hash": "c" * 64,
        "materialization_set_hash": "d" * 64,
        "run_spec_template_hash": "e" * 64,
        "rubric_hash": "f" * 64,
        "renderer_version": "2.0.0",
        "ocr_engine_version": "3.7.0",
        "evidence_policy_version": "trip-check-p5-controlled-evidence-v4",
        "fault_registry_version": "trip-check-p5-fault-registry-v2",
        "random_seed": 20260824,
        "budget": {
            "timeout_seconds": 30,
            "max_retries": 1,
            "max_tokens": 0,
            "max_cost_usd": 0,
            "max_provider_queries": 0,
        },
        "replay_hash_policy": "p5-semantic-projection-v4",
        "variant_id": "core_b",
        "adapter_version": "core-b-v4",
        "repair_strategy": "bounded_repair_v1",
    }
    return P5VariantRunSpecV4.model_validate(payload)


@pytest.mark.asyncio
async def test_core_v4_accepts_authorized_route_repair_and_redacts_oracle() -> None:
    case, materialization = _repaired_case()
    result = _HarnessResult(
        terminal_status=TerminalStatusV3.SUCCEEDED,
        native_output={},
        evaluation_projection={},
        findings=[],
        advice=[],
        postcheck=None,
        receipts=[],
        raw_artifact={},
    )
    execute = AsyncMock(return_value=result)
    with patch(
        "evals.trip_check_v1.p5.adapters_v4._execute_product_harness", execute
    ):
        await CoreAdapterV4().execute(case, materialization, _run_spec())
    runtime_case = execute.await_args.args[0]
    assert case.oracle is not None
    assert runtime_case.oracle is None
    assert execute.await_args.args[1] == validate_materialization_v4(
        case, materialization
    )


def test_legacy_v4_binds_the_v4_materialization_validator() -> None:
    adapter = LegacyAdapterV4()
    assert adapter._delegate._materialization_validator is validate_materialization_v4
    assert adapter._delegate._contract_version == "v3"


@pytest.mark.asyncio
async def test_core_v4_product_path_emits_repaired_route_finding() -> None:
    case, materialization = _repaired_case()
    terminal = await execute_terminal_v4(
        case=case,
        materialization=materialization,
        run_spec=_run_spec(),
        adapter=CoreAdapterV4(),
    )
    assert terminal.terminal_status is TerminalStatusV3.SUCCEEDED, (
        terminal.error_category,
        terminal.receipts,
    )
    semantic_codes = _semantic_reason_codes_v3(terminal, materialization)
    assert "TRAVEL_TIME_GAP" in semantic_codes, sorted(semantic_codes)
    assert terminal.capability_outcomes["authoritative_oracle_access"] == "DENIED"
