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


FORMAL_COMMITMENT_FIELDS_V2 = frozenset(
    {
        "active_contract_file_sha256",
        "blind_seal_sha256",
        "external_bundle_sha256",
        "labels_canonical_sha256",
        "review_receipt_sha256",
    }
)
ACTIVE_READY_FIELDS_V2 = frozenset(
    {
        "schema_version",
        "active_contract",
        "formal_evidence_status",
        "candidate_freeze_commit",
        "blind_seal_v2_sha256",
        "dataset_manifest_hash",
        "deprecated_contracts",
    }
)
BLIND_SEAL_FIELDS_V2 = frozenset(
    {
        "schema_version",
        "split",
        "case_count",
        "case_ids_sha256",
        "inputs_file_sha256",
        "inputs_content_sha256",
        "materializations_file_sha256",
        "materializations_content_sha256",
        "schema_contract_sha256",
        "labels_canonical_sha256",
        "external_bundle_sha256",
        "rubric_sha256",
        "run_spec_template_sha256",
        "variant_ids_sha256",
        "review_receipt_sha256",
        "label_storage",
        "label_access",
        "scoring_payload_present",
        "human_evidence",
    }
)


def _is_lower_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def not_applicable_commitments_v2() -> dict[str, str]:
    return {field: "NOT_APPLICABLE" for field in sorted(FORMAL_COMMITMENT_FIELDS_V2)}


def build_formal_commitments_v2(
    *,
    active: Mapping[str, Any],
    active_contract_file_sha256: str,
    seal: Mapping[str, Any],
    blind_seal_sha256: str,
    dataset_manifest_hash: str,
) -> dict[str, str]:
    """Validate the READY active contract and copy its sealed commitments."""

    if set(active) != ACTIVE_READY_FIELDS_V2:
        raise ValueError("formal active contract fields differ from v2 READY contract")
    if (
        active.get("schema_version") != "trip-check-p5-active-contract-v1"
        or active.get("active_contract") != "trip-check-p5-v2"
        or active.get("formal_evidence_status") != "READY"
    ):
        raise ValueError("formal active contract is not v2 READY")
    if not isinstance(active.get("candidate_freeze_commit"), str) or len(active["candidate_freeze_commit"]) != 40:
        raise ValueError("formal active contract candidate commit is invalid")
    if any(character not in "0123456789abcdef" for character in active["candidate_freeze_commit"]):
        raise ValueError("formal active contract candidate commit is invalid")
    if active.get("deprecated_contracts") != [
        {
            "contract_id": "trip-check-p5-v1",
            "formal_evidence_eligible": False,
            "reason": "SUPERSEDED_BY_USER_APPROVED_P5_V2",
        }
    ]:
        raise ValueError("formal active contract does not permanently supersede v1")
    if set(seal) != BLIND_SEAL_FIELDS_V2:
        raise ValueError("formal blind seal fields differ from v2 contract")
    if (
        seal.get("schema_version") != "trip-check-p5-blind-seal-v2"
        or seal.get("split") != "frozen_blind"
        or seal.get("case_count") != 90
        or seal.get("label_storage") != "external_bundle_only"
        or seal.get("label_access") != "isolated_scorer_only"
        or seal.get("scoring_payload_present") is not False
        or seal.get("human_evidence") is not False
    ):
        raise ValueError("formal blind seal contract is invalid")
    for field, value in (
        ("active_contract_file_sha256", active_contract_file_sha256),
        ("blind_seal_sha256", blind_seal_sha256),
        ("dataset_manifest_hash", dataset_manifest_hash),
        ("blind_seal_v2_sha256", active.get("blind_seal_v2_sha256")),
        ("active_dataset_manifest_hash", active.get("dataset_manifest_hash")),
        ("external_bundle_sha256", seal.get("external_bundle_sha256")),
        ("labels_canonical_sha256", seal.get("labels_canonical_sha256")),
        ("review_receipt_sha256", seal.get("review_receipt_sha256")),
    ):
        if not _is_lower_sha256(value):
            raise ValueError(f"formal commitment is not a lowercase SHA-256: {field}")
    if active["blind_seal_v2_sha256"] != blind_seal_sha256:
        raise ValueError("formal active contract seal hash mismatch")
    if active["dataset_manifest_hash"] != dataset_manifest_hash:
        raise ValueError("formal active contract dataset hash mismatch")
    commitments = {
        "active_contract_file_sha256": active_contract_file_sha256,
        "blind_seal_sha256": blind_seal_sha256,
        "external_bundle_sha256": str(seal["external_bundle_sha256"]),
        "labels_canonical_sha256": str(seal["labels_canonical_sha256"]),
        "review_receipt_sha256": str(seal["review_receipt_sha256"]),
    }
    if set(commitments) != FORMAL_COMMITMENT_FIELDS_V2:
        raise AssertionError("internal formal commitment field drift")
    return commitments


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
    "ACTIVE_READY_FIELDS_V2",
    "BLIND_SEAL_FIELDS_V2",
    "FORMAL_COMMITMENT_FIELDS_V2",
    "build_formal_commitments_v2",
    "execute_terminal_v2",
    "not_applicable_commitments_v2",
    "validate_exact_terminal_set_v2",
    "validate_run_spec_whitelist_v2",
    "write_jsonl_atomic_v2",
]
