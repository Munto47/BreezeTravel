from __future__ import annotations

import asyncio

import pytest

from evals.trip_check_v1.p5.adapters import CoreAdapter, SolverAdapter
from evals.trip_check_v1.p5.contracts import (
    P5AdapterResult,
    P5VariantRunSpec,
    TerminalStatus,
)
from evals.trip_check_v1.p5.runner import execute_terminal, validate_exact_terminal_set


def _case() -> dict:
    return {
        "case_id": "p5.dev.bj.001",
        "split": "dev",
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": "TEXT",
        "product_input": {
            "source_type": "MANUAL_TEXT",
            "raw_text": (
                "北京2人，2天。第1天 09:00-12:00 故宫博物院，"
                "13:00-15:00 天坛公园；第2天 09:00-12:00 颐和园。"
            ),
        },
        "normalized_input_sha256": "1" * 64,
        "runner_control": {
            "fault_profile_id": "none",
            "provider_snapshot_id": "fixture-v1",
            "seed": 7,
        },
        "oracle": {"secret": "must-not-reach-adapter"},
        "oracle_sha256": "2" * 64,
    }


def _run_spec(variant_id: str = "core_b") -> P5VariantRunSpec:
    versions = {
        "legacy_a": ("legacy-a-v1", "legacy_native_only"),
        "core_b": ("core-b-v1", "bounded_repair_v1"),
        "solver_c": ("solver-c-v1", "cp_sat_v1"),
    }
    adapter_version, strategy = versions[variant_id]
    return P5VariantRunSpec(
        subject_commit="a" * 40,
        dirty_tree=False,
        lane="nonblind",
        dataset_manifest_hash="b" * 64,
        case_set_hash="c" * 64,
        run_spec_template_hash="d" * 64,
        provider_snapshot_id="fixture-v1",
        random_seed=7,
        budget={
            "max_cost_usd": 0,
            "max_provider_queries": 0,
            "max_retries": 1,
            "max_tokens": 0,
            "timeout_seconds": 1,
        },
        variant_id=variant_id,
        adapter_version=adapter_version,
        repair_strategy=strategy,
    )


class _CapturingAdapter:
    variant_id = "core_b"
    adapter_version = "core-b-v1"
    repair_strategy = "bounded_repair_v1"

    def __init__(self) -> None:
        self.seen = None

    async def execute(self, adapter_input, run_spec):
        self.seen = adapter_input.model_dump(mode="json")
        return P5AdapterResult(
            terminal_status=TerminalStatus.SUCCEEDED,
            native_output={"stable": True},
            raw_artifact={"volatile_id": "one"},
        )


@pytest.mark.asyncio
async def test_runner_strips_oracle_and_replay_hash_ignores_raw_artifact() -> None:
    first_adapter = _CapturingAdapter()
    second_adapter = _CapturingAdapter()
    first = await execute_terminal(case=_case(), run_spec=_run_spec(), adapter=first_adapter)

    async def changed_execute(adapter_input, run_spec):
        second_adapter.seen = adapter_input.model_dump(mode="json")
        return P5AdapterResult(
            terminal_status=TerminalStatus.SUCCEEDED,
            native_output={"stable": True},
            raw_artifact={"volatile_id": "two"},
        )

    second_adapter.execute = changed_execute
    second = await execute_terminal(case=_case(), run_spec=_run_spec(), adapter=second_adapter)

    assert "oracle" not in first_adapter.seen
    assert "oracle_sha256" not in first_adapter.seen
    assert first.raw_artifact_hash != second.raw_artifact_hash
    assert first.semantic_output_hash == second.semantic_output_hash
    assert first.replay_hash == second.replay_hash


@pytest.mark.asyncio
async def test_runner_always_emits_timeout_and_error_rows() -> None:
    adapter = _CapturingAdapter()

    async def slow(adapter_input, run_spec):
        await asyncio.sleep(0.02)
        return P5AdapterResult(terminal_status=TerminalStatus.SUCCEEDED)

    spec = _run_spec().model_copy(update={"budget": {**_run_spec().budget, "timeout_seconds": 0.001}})
    adapter.execute = slow
    timeout = await execute_terminal(case=_case(), run_spec=spec, adapter=adapter)
    assert timeout.terminal_status == TerminalStatus.TIMEOUT

    async def broken(adapter_input, run_spec):
        raise RuntimeError("sensitive error detail")

    adapter.execute = broken
    error = await execute_terminal(case=_case(), run_spec=_run_spec(), adapter=adapter)
    assert error.terminal_status == TerminalStatus.ERROR
    assert error.error_category == "RuntimeError"
    assert "sensitive error detail" not in error.model_dump_json()


def test_exact_terminal_set_rejects_missing_duplicate_and_extra() -> None:
    async def build():
        adapter = _CapturingAdapter()
        return await execute_terminal(case=_case(), run_spec=_run_spec(), adapter=adapter)

    output = asyncio.run(build())
    validate_exact_terminal_set(
        [output], case_ids={"p5.dev.bj.001"}, variant_ids={"core_b"}
    )
    with pytest.raises(ValueError, match="duplicate"):
        validate_exact_terminal_set(
            [output, output], case_ids={"p5.dev.bj.001"}, variant_ids={"core_b"}
        )
    with pytest.raises(ValueError, match="mismatch"):
        validate_exact_terminal_set(
            [output], case_ids={"p5.dev.bj.001", "p5.dev.bj.002"}, variant_ids={"core_b"}
        )


@pytest.mark.asyncio
async def test_core_adapter_executes_real_isolated_trip_check_chain() -> None:
    adapter = CoreAdapter()
    output = await execute_terminal(case=_case(), run_spec=_run_spec(), adapter=adapter)

    assert output.terminal_status in {
        TerminalStatus.SUCCEEDED,
        TerminalStatus.NEEDS_USER_RESOLUTION,
    }
    assert output.capability_outcomes["authoritative_oracle_access"] == "DENIED"
    assert output.receipts[0]["llm_calls"] == 0
    assert output.receipts[0]["external_api_calls"] == 0
    assert output.native_output["replay_side_effect_counts_equal"] is True


@pytest.mark.asyncio
async def test_solver_adapter_binds_primary_and_fallback_without_promoting_cp_sat() -> None:
    case = _case()
    case["runner_control"]["fault_profile_id"] = "solver_fallback"
    adapter = SolverAdapter()
    output = await execute_terminal(
        case=case,
        run_spec=_run_spec("solver_c"),
        adapter=adapter,
    )

    assert output.terminal_status == TerminalStatus.ERROR
    assert output.native_output["solver_primary"]["status"] == "ERROR"
    assert output.native_output["solver_effective"]["status"] == "SUCCESS"
    assert output.native_output["fallback_used"] is True
    assert output.native_output["p4_admission"] == "REJECT"
