"""Fail-closed P5 v2 terminal runner and atomic output writer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from evals.trip_check_v1.p5.adapters_v2 import _HarnessResult, validate_materialization_v2
from evals.trip_check_v1.p5.contracts_v2 import (
    P5CaseV2,
    P5TerminalOutputV2,
    P5VariantRunSpecV2,
    TerminalStatusV2,
)
from evals.trip_check_v1.p5.data_contract import digest


class VariantAdapterV2(Protocol):
    variant_id: str
    adapter_version: str
    repair_strategy: str

    async def execute(
        self,
        case: P5CaseV2,
        materialization: Mapping[str, Any],
        run_spec: P5VariantRunSpecV2,
    ) -> _HarnessResult: ...


def _semantic_payload(
    *,
    case: P5CaseV2,
    run_spec: P5VariantRunSpecV2,
    result: _HarnessResult,
) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "input_hash": case.normalized_input_sha256,
        "materialization_hash": case.materialization.materialization_sha256,
        "run_spec_hash": run_spec.run_spec_hash,
        "variant_id": run_spec.variant_id,
        "adapter_version": run_spec.adapter_version,
        "repair_strategy": run_spec.repair_strategy,
        "terminal_status": result.terminal_status.value,
        "capability_outcomes": {
            "authoritative_oracle_access": "DENIED",
            "external_api_calls": "0",
            "product_import": (
                "UNSUPPORTED" if result.native_output.get("product_import") is None else "PRODUCTION_SERVICE"
            ),
        },
        "native_output": result.native_output,
        "evaluation_projection": result.evaluation_projection,
        "findings": result.findings,
        "advice": result.advice,
        "postcheck": result.postcheck,
        "receipts": result.receipts,
        "token_count": 0,
        "cost_usd": 0.0,
        "error_category": None,
    }


async def execute_terminal_v2(
    *,
    case: P5CaseV2 | Mapping[str, Any],
    materialization: Mapping[str, Any],
    run_spec: P5VariantRunSpecV2,
    adapter: VariantAdapterV2,
) -> P5TerminalOutputV2:
    validated_case = case if isinstance(case, P5CaseV2) else P5CaseV2.model_validate(case)
    if adapter.variant_id != run_spec.variant_id:
        raise ValueError("adapter variant does not match RunSpec")
    if adapter.adapter_version != run_spec.adapter_version:
        raise ValueError("adapter version does not match RunSpec")
    if adapter.repair_strategy != run_spec.repair_strategy:
        raise ValueError("adapter strategy does not match RunSpec")
    if materialization.get("case_id") != validated_case.case_id:
        raise ValueError("case/materialization ID mismatch")

    started = perf_counter()
    error_category = None
    try:
        validated_materialization = validate_materialization_v2(validated_case, materialization)
        result = await asyncio.wait_for(
            adapter.execute(validated_case, validated_materialization, run_spec),
            timeout=float(run_spec.budget["timeout_seconds"]),
        )
    except TimeoutError:
        error_category = "ADAPTER_DEADLINE_EXCEEDED"
        result = _HarnessResult(
            terminal_status=TerminalStatusV2.TIMEOUT,
            native_output={},
            evaluation_projection={},
            findings=[],
            advice=[],
            postcheck=None,
            receipts=[{"type": "runner_timeout"}],
            raw_artifact={},
        )
    except Exception as exc:  # every attempted case must produce a terminal row
        error_category = type(exc).__name__
        result = _HarnessResult(
            terminal_status=TerminalStatusV2.ERROR,
            native_output={},
            evaluation_projection={},
            findings=[],
            advice=[],
            postcheck=None,
            receipts=[{"type": "runner_error", "category": type(exc).__name__}],
            raw_artifact={},
        )
    latency_ms = (perf_counter() - started) * 1000
    semantic = _semantic_payload(case=validated_case, run_spec=run_spec, result=result)
    semantic["error_category"] = error_category
    semantic_hash = digest(semantic)
    binding = validated_case.materialization
    return P5TerminalOutputV2(
        case_id=validated_case.case_id,
        split=validated_case.split,
        city=validated_case.city,
        input_kind=validated_case.input_kind,
        input_hash=validated_case.normalized_input_sha256,
        materialization_hash=binding.materialization_sha256,
        render_receipt_hash=(binding.render_receipt.content_sha256 if binding.render_receipt else None),
        ocr_receipt_hash=(binding.ocr_baseline_receipt.content_sha256 if binding.ocr_baseline_receipt else None),
        provider_snapshot_hash=binding.provider_snapshot.content_sha256,
        evidence_snapshot_hash=binding.evidence_snapshot.content_sha256,
        candidate_set_hashes=[item.content_sha256 for item in binding.candidate_sets],
        fault_script_hash=binding.fault_script.content_sha256,
        run_spec_hash=run_spec.run_spec_hash,
        variant_id=run_spec.variant_id,
        adapter_version=run_spec.adapter_version,
        repair_strategy=run_spec.repair_strategy,
        terminal_status=result.terminal_status,
        capability_outcomes=semantic["capability_outcomes"],
        native_output=result.native_output,
        evaluation_projection=result.evaluation_projection,
        findings=result.findings,
        advice=result.advice,
        postcheck=result.postcheck,
        receipts=result.receipts,
        latency_ms=latency_ms,
        token_count=0,
        cost_usd=0.0,
        error_category=error_category,
        raw_artifact_hash=digest(result.raw_artifact),
        semantic_output_hash=semantic_hash,
        replay_hash=semantic_hash,
    )


def validate_exact_terminal_set_v2(
    outputs: Sequence[P5TerminalOutputV2],
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


def validate_run_spec_whitelist_v2(specs: Sequence[P5VariantRunSpecV2]) -> None:
    if not specs:
        raise ValueError("RunSpec comparison requires at least one variant")
    allowed = {"variant_id", "adapter_version", "repair_strategy"}
    common = {key: value for key, value in specs[0].model_dump(mode="json").items() if key not in allowed}
    for spec in specs[1:]:
        candidate = {key: value for key, value in spec.model_dump(mode="json").items() if key not in allowed}
        if candidate != common:
            raise ValueError("RunSpecs differ outside the P5 variant whitelist")


def write_jsonl_atomic_v2(path: Path, outputs: Sequence[P5TerminalOutputV2]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    ordered = sorted(outputs, key=lambda item: (item.case_id, item.variant_id))
    payloads = [item.model_dump(mode="json") for item in ordered]
    data = ("\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in payloads) + "\n").encode("utf-8")
    temporary.write_bytes(data)
    temporary.replace(path)
    return digest(payloads)


__all__ = [
    "execute_terminal_v2",
    "validate_exact_terminal_set_v2",
    "validate_run_spec_whitelist_v2",
    "write_jsonl_atomic_v2",
]
