"""Fail-closed P5 comparison runner and exact terminal-row writer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from time import perf_counter
from typing import Protocol

from evals.trip_check_v1.p5.contracts import (
    P5AdapterInput,
    P5AdapterResult,
    P5TerminalOutput,
    P5VariantRunSpec,
    TerminalStatus,
    semantic_projection,
)
from evals.trip_check_v1.p5.data_contract import digest


class TripCheckVariantAdapter(Protocol):
    variant_id: str
    adapter_version: str
    repair_strategy: str

    async def execute(
        self,
        adapter_input: P5AdapterInput,
        run_spec: P5VariantRunSpec,
    ) -> P5AdapterResult: ...


InfrastructureHook = Callable[[str, BaseException], Awaitable[None]]


async def execute_terminal(
    *,
    case: dict,
    run_spec: P5VariantRunSpec,
    adapter: TripCheckVariantAdapter,
) -> P5TerminalOutput:
    if adapter.variant_id != run_spec.variant_id:
        raise ValueError("adapter variant does not match RunSpec")
    if adapter.adapter_version != run_spec.adapter_version:
        raise ValueError("adapter version does not match RunSpec")
    if adapter.repair_strategy != run_spec.repair_strategy:
        raise ValueError("adapter strategy does not match RunSpec")

    adapter_input = P5AdapterInput.from_case(case)
    started = perf_counter()
    try:
        result = await asyncio.wait_for(
            adapter.execute(adapter_input, run_spec),
            timeout=float(run_spec.budget["timeout_seconds"]),
        )
    except TimeoutError:
        result = P5AdapterResult(
            terminal_status=TerminalStatus.TIMEOUT,
            error_category="ADAPTER_DEADLINE_EXCEEDED",
            trace=[{"event": "timeout", "source": "p5_runner"}],
        )
    except Exception as exc:  # terminal rows are mandatory even on adapter failure
        result = P5AdapterResult(
            terminal_status=TerminalStatus.ERROR,
            error_category=type(exc).__name__,
            trace=[{"event": "adapter_error", "source": "p5_runner"}],
        )
    latency_ms = (perf_counter() - started) * 1000
    projection = semantic_projection(
        adapter_input=adapter_input,
        run_spec=run_spec,
        result=result,
    )
    semantic_hash = digest(projection)
    return P5TerminalOutput(
        case_id=adapter_input.case_id,
        split=adapter_input.split,
        city=adapter_input.city,
        input_kind=adapter_input.input_kind,
        input_hash=adapter_input.normalized_input_sha256,
        provider_snapshot_id=str(adapter_input.runner_control["provider_snapshot_id"]),
        fault_profile_id=str(adapter_input.runner_control["fault_profile_id"]),
        case_seed=int(adapter_input.runner_control["seed"]),
        run_spec_hash=run_spec.run_spec_hash,
        variant_id=run_spec.variant_id,
        adapter_version=run_spec.adapter_version,
        repair_strategy=run_spec.repair_strategy,
        terminal_status=result.terminal_status,
        capability_outcomes=result.capability_outcomes,
        native_output=result.native_output,
        evaluation_projection=result.evaluation_projection,
        findings=result.findings,
        advice=result.advice,
        postcheck=result.postcheck,
        trace=result.trace,
        receipts=result.receipts,
        latency_ms=latency_ms,
        token_count=result.token_count,
        cost_usd=result.cost_usd,
        error_category=result.error_category,
        raw_artifact_hash=digest(result.raw_artifact),
        semantic_output_hash=semantic_hash,
        replay_hash=semantic_hash,
    )


def validate_exact_terminal_set(
    outputs: Sequence[P5TerminalOutput],
    *,
    case_ids: set[str],
    variant_ids: set[str],
) -> None:
    expected = {(case_id, variant_id) for case_id in case_ids for variant_id in variant_ids}
    actual = [(item.case_id, item.variant_id) for item in outputs]
    if len(actual) != len(set(actual)):
        raise ValueError("duplicate case/variant terminal row")
    missing = expected - set(actual)
    extra = set(actual) - expected
    if missing or extra:
        raise ValueError(f"terminal set mismatch: missing={len(missing)} extra={len(extra)}")


def write_jsonl_atomic(path: Path, outputs: Sequence[P5TerminalOutput]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    sorted_outputs = sorted(outputs, key=lambda value: (value.case_id, value.variant_id))
    lines = [
        json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        for item in sorted_outputs
    ]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    temp.write_bytes(payload)
    temp.replace(path)
    return digest([item.model_dump(mode="json") for item in sorted_outputs])
