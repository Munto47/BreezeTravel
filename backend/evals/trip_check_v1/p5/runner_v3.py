"""Fail-closed P5 v3 terminal runner and machine-readable result contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from evals.trip_check_v1.p5.adapters_v2 import _HarnessResult
from evals.trip_check_v1.p5.adapters_v3 import validate_materialization_v3
from evals.trip_check_v1.p5.contracts_v3 import (
    P5CaseResultV3,
    P5CaseV3,
    P5FailureRecordV3,
    P5TerminalOutputV3,
    P5VariantRunSpecV3,
    TerminalStatusV3,
)
from evals.trip_check_v1.p5.data_contract import digest


class VariantAdapterV3(Protocol):
    variant_id: str
    adapter_version: str
    repair_strategy: str

    async def execute(
        self,
        case: P5CaseV3,
        materialization: Mapping[str, Any],
        run_spec: P5VariantRunSpecV3,
    ) -> _HarnessResult: ...


def _semantic_payload_v3(
    *,
    case: P5CaseV3,
    run_spec: P5VariantRunSpecV3,
    result: _HarnessResult,
    error_category: str | None,
) -> dict[str, Any]:
    screenshot_mode = (
        "DENIED_LEGACY_BOUNDARY"
        if case.input_kind == "SYNTHETIC_SCREENSHOT" and run_spec.variant_id == "legacy_a"
        else "FROZEN_ACTUAL_OCR_RECEIPT_REPLAY"
        if case.input_kind == "SYNTHETIC_SCREENSHOT"
        else "NOT_APPLICABLE"
    )
    return {
        "replay_hash_policy": "p5-semantic-projection-v3",
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
            "blind_label_access": "DENIED",
            "external_api_calls": "0",
            "product_import": (
                "UNSUPPORTED"
                if result.native_output.get("product_import") is None
                else "PRODUCTION_SERVICE"
            ),
            "screenshot_execution": screenshot_mode,
        },
        "native_output": result.native_output,
        "evaluation_projection": result.evaluation_projection,
        "findings": result.findings,
        "advice": result.advice,
        "postcheck": result.postcheck,
        "receipts": result.receipts,
        "token_count": 0,
        "cost_usd": 0.0,
        "error_category": error_category,
    }


async def execute_terminal_v3(
    *,
    case: P5CaseV3 | Mapping[str, Any],
    materialization: Mapping[str, Any],
    run_spec: P5VariantRunSpecV3,
    adapter: VariantAdapterV3,
) -> P5TerminalOutputV3:
    validated_case = case if isinstance(case, P5CaseV3) else P5CaseV3.model_validate(case)
    if adapter.variant_id != run_spec.variant_id:
        raise ValueError("adapter variant does not match P5 v3 RunSpec")
    if adapter.adapter_version != run_spec.adapter_version:
        raise ValueError("adapter version does not match P5 v3 RunSpec")
    if adapter.repair_strategy != run_spec.repair_strategy:
        raise ValueError("adapter strategy does not match P5 v3 RunSpec")
    if materialization.get("case_id") != validated_case.case_id:
        raise ValueError("P5 v3 case/materialization ID mismatch")

    # This readback happens before every attempt, including replay.  A v3 run
    # cannot silently fall back to a v2 validator or a previously validated row.
    validated_materialization = validate_materialization_v3(validated_case, materialization)
    started = perf_counter()
    error_category: str | None = None
    try:
        result = await asyncio.wait_for(
            adapter.execute(validated_case, validated_materialization, run_spec),
            timeout=run_spec.budget.timeout_seconds,
        )
    except TimeoutError:
        error_category = "ADAPTER_DEADLINE_EXCEEDED"
        result = _HarnessResult(
            terminal_status=TerminalStatusV3.TIMEOUT,  # type: ignore[arg-type]
            native_output={},
            evaluation_projection={},
            findings=[],
            advice=[],
            postcheck=None,
            receipts=[{"type": "runner_timeout", "contract": "v3"}],
            raw_artifact={},
        )
    except Exception as exc:  # every attempted case still receives one terminal row
        error_category = type(exc).__name__
        result = _HarnessResult(
            terminal_status=TerminalStatusV3.ERROR,  # type: ignore[arg-type]
            native_output={},
            evaluation_projection={},
            findings=[],
            advice=[],
            postcheck=None,
            receipts=[
                {
                    "type": "runner_error",
                    "contract": "v3",
                    "category": type(exc).__name__,
                }
            ],
            raw_artifact={},
        )
    latency_ms = (perf_counter() - started) * 1000
    semantic = _semantic_payload_v3(
        case=validated_case,
        run_spec=run_spec,
        result=result,
        error_category=error_category,
    )
    semantic_hash = digest(semantic)
    binding = validated_case.materialization
    return P5TerminalOutputV3(
        case_id=validated_case.case_id,
        split=validated_case.split,
        city=validated_case.city,
        input_kind=validated_case.input_kind,
        input_hash=validated_case.normalized_input_sha256,
        materialization_hash=binding.materialization_sha256,
        render_receipt_hash=(
            binding.render_receipt.content_sha256 if binding.render_receipt else None
        ),
        ocr_receipt_hash=(
            binding.ocr_baseline_receipt.content_sha256
            if binding.ocr_baseline_receipt
            else None
        ),
        provider_snapshot_hash=binding.provider_snapshot.content_sha256,
        evidence_snapshot_hash=binding.evidence_snapshot.content_sha256,
        candidate_set_hashes=[item.content_sha256 for item in binding.candidate_sets],
        fault_script_hash=binding.fault_script.content_sha256,
        run_spec_hash=run_spec.run_spec_hash,
        variant_id=run_spec.variant_id,
        adapter_version=run_spec.adapter_version,
        repair_strategy=run_spec.repair_strategy,
        terminal_status=TerminalStatusV3(result.terminal_status.value),
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


def revision_lineage_v3(
    *, case: P5CaseV3, terminal: P5TerminalOutputV3
) -> dict[str, Any]:
    repair_attempted = bool(
        terminal.evaluation_projection.get("repair_adoption_attempted", False)
    )
    postcheck = terminal.postcheck
    return {
        "schema_version": "trip-check-p5-revision-lineage-v3",
        "case_id": case.case_id,
        "source_case_hash": case.case_hash,
        "source_materialization_hash": case.materialization.materialization_sha256,
        "initial_revision": (
            1 if terminal.native_output.get("product_import") is not None else None
        ),
        "repair_adoption_attempted": repair_attempted,
        "resulting_revision": 2 if repair_attempted and postcheck is not None else None,
        "postcheck": {
            "status": "COMPLETE" if postcheck is not None else "NOT_RUN_OR_NOT_APPLICABLE",
            "report_id": postcheck.get("report_id") if postcheck else None,
            "overall_status": postcheck.get("overall_status") if postcheck else None,
        },
        "terminal_replay_hash": terminal.replay_hash,
    }


def build_case_result_v3(
    *, run_id: str, case: P5CaseV3, terminal: P5TerminalOutputV3
) -> P5CaseResultV3:
    payload = {
        "schema_version": "trip-check-p5-case-result-v3",
        "case_result_id": f"{run_id}:{case.case_id}:{terminal.variant_id}",
        "run_id": run_id,
        "terminal_output": terminal.model_dump(mode="json"),
        "revision_lineage": revision_lineage_v3(case=case, terminal=terminal),
    }
    payload["case_result_hash"] = digest(payload)
    return P5CaseResultV3.model_validate(payload)


def build_failure_record_v3(
    *, run_id: str, lane: str, terminal: P5TerminalOutputV3
) -> P5FailureRecordV3 | None:
    if terminal.terminal_status not in {
        TerminalStatusV3.ERROR,
        TerminalStatusV3.TIMEOUT,
        TerminalStatusV3.UNSUPPORTED_CAPABILITY,
    }:
        return None
    first_receipt_hash = digest(terminal.receipts[0]) if terminal.receipts else None
    invalid_evidence_categories = {
        "OcrProcessingError",
        "ValidationError",
        "ValueError",
    }
    payload: dict[str, Any] = {
        "schema_version": "trip-check-p5-failure-record-v3",
        "run_id": run_id,
        "case_id": terminal.case_id,
        "lane": lane,
        "variant_id": terminal.variant_id,
        "failure_status": (
            "INVALID_EVIDENCE"
            if terminal.error_category in invalid_evidence_categories
            else "REJECT"
        ),
        "failure_category": terminal.error_category or terminal.terminal_status.value,
        "terminal_status": terminal.terminal_status.value,
        "first_attempt_receipt_hash": first_receipt_hash,
        "reproduction_command": (
            "REPRODUCTION_RESTRICTED_TO_BLIND_CUSTODIAN"
            if lane == "frozen_blind"
            else "python -m scripts.run_trip_check_p5_v3_eval --lane nonblind "
            f"--variants {terminal.variant_id} --case-id {terminal.case_id} "
            "--replay --output-dir <EXTERNAL_OUTPUT_DIR>"
        ),
        "retry_allowed": False,
        "retry_count": 0,
    }
    if first_receipt_hash is None:
        payload.pop("first_attempt_receipt_hash")
    payload["failure_record_hash"] = digest(payload)
    return P5FailureRecordV3.model_validate(payload)


def validate_exact_terminal_set_v3(
    outputs: Sequence[P5TerminalOutputV3],
    *,
    case_ids: set[str],
    variant_ids: set[str],
) -> None:
    expected = {
        (case_id, variant_id)
        for case_id in case_ids
        for variant_id in variant_ids
    }
    actual = [(item.case_id, item.variant_id) for item in outputs]
    if len(actual) != len(set(actual)):
        raise ValueError("duplicate P5 v3 case/variant terminal row")
    missing = expected - set(actual)
    extra = set(actual) - expected
    if missing or extra:
        raise ValueError(
            f"P5 v3 terminal set mismatch: missing={len(missing)} extra={len(extra)}"
        )


def validate_run_spec_whitelist_v3(
    specs: Sequence[P5VariantRunSpecV3],
) -> None:
    if not specs:
        raise ValueError("P5 v3 RunSpec comparison requires at least one variant")
    allowed = {"variant_id", "adapter_version", "repair_strategy"}
    common = {
        key: value
        for key, value in specs[0].model_dump(mode="json").items()
        if key not in allowed
    }
    for spec in specs[1:]:
        candidate = {
            key: value
            for key, value in spec.model_dump(mode="json").items()
            if key not in allowed
        }
        if candidate != common:
            raise ValueError("P5 v3 RunSpecs differ outside the variant whitelist")


def write_models_jsonl_atomic_v3(
    path: Path, rows: Sequence[P5CaseResultV3 | P5FailureRecordV3]
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    # Optional terminal fields are required contract keys whose value may be
    # null.  Do not erase them while serializing the strict CaseResult row.
    payloads = [row.model_dump(mode="json") for row in rows]
    data = (
        "\n".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in payloads
        )
        + ("\n" if payloads else "")
    ).encode("utf-8")
    temporary.write_bytes(data)
    temporary.replace(path)
    return digest(payloads)


__all__ = [
    "build_case_result_v3",
    "build_failure_record_v3",
    "execute_terminal_v3",
    "revision_lineage_v3",
    "validate_exact_terminal_set_v3",
    "validate_run_spec_whitelist_v3",
    "write_models_jsonl_atomic_v3",
]
