"""Offline custody and independent review tooling for the P5 v2 blind oracle.

This eval-only module has no candidate-output input.  Its authority is limited
to the frozen blind cases, their materializations, and the frozen P5 contracts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

from evals.trip_check_v1.p5.contracts_v2 import P5CaseV2, P5OracleV2, VARIANT_IDS_V2
from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest, file_sha256, load_jsonl
from evals.trip_check_v1.p5.final_blind_scorer_v2 import (
    canonical_labels_hash_v2,
    schema_contract_sha256_v2,
)


BLIND_INPUT_RELATIVE = Path("backend/evals/trip_check_v1/p5/frozen_blind.v2.inputs.jsonl")
BLIND_MATERIALIZATIONS_RELATIVE = Path("backend/evals/trip_check_v1/p5/frozen_blind.v2.materializations.jsonl")
DATASET_MANIFEST_RELATIVE = Path("backend/evals/trip_check_v1/p5/dataset_v2.manifest.json")
RUN_SPEC_RELATIVE = Path("backend/evals/trip_check_v1/p5/run_spec_template_v2.json")
RUBRIC_RELATIVE = Path("backend/evals/trip_check_v1/p5/judge_rubric_v2.json")
BUNDLE_SCHEMA_RELATIVE = Path("backend/evals/trip_check_v1/p5/blind_bundle_v2.schema.json")
REVIEW_SCHEMA_RELATIVE = Path("backend/evals/trip_check_v1/p5/blind_review_receipt_v2.schema.json")

_REASON_BY_FAULT = {
    "advice_completeness": "P4_ADVICE_COMPLETENESS",
    "empty_candidate_set": "P4_EMPTY_CANDIDATE_SET",
    "candidate_receipt_missing": "P4_CANDIDATE_RECEIPT_MISSING",
    "route_conflict": "P4_ROUTE_CONFLICT",
    "duplicate_apply": "P4_DUPLICATE_APPLY",
    "concurrent_apply": "P4_CONCURRENT_APPLY",
    "solver_unsat": "P4_SOLVER_UNSAT",
    "solver_timeout": "P4_SOLVER_TIMEOUT",
    "solver_fallback": "P4_SOLVER_FALLBACK",
}
_STRATEGY_BY_FAULT = {
    "solver_unsat": "UNSAT",
    "solver_timeout": "TIMEOUT",
    "solver_fallback": "FALLBACK",
}
_CANDIDATE_MODE_TO_ORACLE = {
    "VALID": "REQUIRED",
    "EMPTY": "FORBIDDEN",
    "MISSING_RECEIPT": "FORBIDDEN",
    "NOT_APPLICABLE": "NOT_APPLICABLE",
}
_CANDIDATE_BY_FAULT = {
    "advice_completeness": "VALID",
    "empty_candidate_set": "EMPTY",
    "candidate_receipt_missing": "MISSING_RECEIPT",
    "route_conflict": "NOT_APPLICABLE",
    "duplicate_apply": "NOT_APPLICABLE",
    "concurrent_apply": "NOT_APPLICABLE",
    "solver_unsat": "NOT_APPLICABLE",
    "solver_timeout": "NOT_APPLICABLE",
    "solver_fallback": "NOT_APPLICABLE",
}
_CONCURRENCY_BY_FAULT = {
    "duplicate_apply": "IDEMPOTENT_REPLAY",
    "concurrent_apply": "SINGLE_WINNER",
}
_PRIVATE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|secret)\s*[:=]\s*\S+"),
    re.compile(r"\b1[3-9]\d{9}\b"),
    re.compile(r"\b\d{17}[0-9Xx]\b"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)


class BlindLabelV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-blind-label-v2"] = "trip-check-p5-blind-label-v2"
    case_id: str = Field(min_length=1)
    oracle: P5OracleV2


class BlindDatasetBindingV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_count: Literal[90] = 90
    case_ids_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inputs_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inputs_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    materializations_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    materializations_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_spec_template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant_ids_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BlindLabelBundleV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-blind-label-bundle-v2"] = "trip-check-p5-blind-label-bundle-v2"
    evidence_class: Literal["controlled_blind_oracle"] = "controlled_blind_oracle"
    human_evidence: Literal[False] = False
    dataset_binding: BlindDatasetBindingV2
    labels: list[BlindLabelV2] = Field(min_length=90, max_length=90)


class CandidateModeCountsV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    VALID: Literal[10] = 10
    EMPTY: Literal[10] = 10
    MISSING_RECEIPT: Literal[10] = 10
    NOT_APPLICABLE: Literal[60] = 60


class BlindReviewChecksV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_count: Literal[90] = 90
    case_set_exact: Literal[True] = True
    binding_recomputed: Literal[True] = True
    oracle_exact: Literal[True] = True
    privacy_findings_count: Literal[0] = 0
    candidate_output_dependency_count: Literal[0] = 0
    network_api_calls: Literal[0] = 0
    unknown_preserved_count: Literal[18] = 18
    candidate_set_mode_counts: CandidateModeCountsV2
    concurrency_case_count: Literal[20] = 20


class BlindReviewReceiptV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-blind-review-receipt-v2"] = "trip-check-p5-blind-review-receipt-v2"
    review_status: Literal["PASS"] = "PASS"
    candidate_subject_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    bundle_byte_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    labels_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    oracle_derivation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_binding: BlindDatasetBindingV2
    checks: BlindReviewChecksV2
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_link_or_junction(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def _contains_link_or_junction(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists() and _is_link_or_junction(current):
            return True
    return False


def _safe_repo_file(repo_root: Path, relative: Path) -> Path:
    root = repo_root.resolve(strict=True)
    path = (root / relative).absolute()
    if relative.is_absolute() or ".." in relative.parts or _contains_link_or_junction(path):
        raise ValueError(f"repository source path is unsafe: {relative.as_posix()}")
    resolved = path.resolve(strict=True)
    if not _inside(resolved, root) or not resolved.is_file():
        raise ValueError(f"repository source path escaped: {relative.as_posix()}")
    return resolved


def validate_external_output_path(repo_root: Path, output_path: Path) -> Path:
    """Require a new, absolute, non-reparse file path outside the repository."""

    if not output_path.is_absolute() or ".." in output_path.parts:
        raise ValueError("external output path must be absolute without parent traversal")
    absolute = output_path.absolute()
    if _contains_link_or_junction(absolute):
        raise ValueError("external output path cannot contain a symlink or junction")
    parent = absolute.parent.resolve(strict=True)
    if _contains_link_or_junction(parent):
        raise ValueError("external output parent cannot contain a symlink or junction")
    if _inside(parent, repo_root.resolve(strict=True)):
        raise ValueError("external output must be outside the repository")
    if absolute.exists():
        raise ValueError("external output already exists")
    return absolute


def _artifact_hash_is_valid(artifact: Mapping[str, Any]) -> bool:
    return artifact.get("content_sha256") == digest(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )


def _validate_candidate_path(case: Mapping[str, Any], materialization: Mapping[str, Any]) -> str:
    control = case["runner_control"]
    mode = str(control["candidate_set_mode"])
    if mode not in _CANDIDATE_MODE_TO_ORACLE:
        raise ValueError("candidate set mode is outside the frozen mapping")
    fault = str(control.get("fault_profile_id"))
    if _CANDIDATE_BY_FAULT.get(fault) != mode:
        raise ValueError("candidate set mode conflicts with the frozen fault mapping")
    candidate_sets = materialization.get("candidate_sets")
    receipts = materialization.get("receipts")
    if not isinstance(candidate_sets, list) or not isinstance(receipts, list):
        raise ValueError("candidate materialization fields are invalid")
    if mode == "VALID":
        if len(candidate_sets) != 1:
            raise ValueError("VALID candidate path requires exactly one frozen set")
        receipt_by_id = {
            item.get("receipt_id"): item
            for item in receipts
            if isinstance(item, Mapping) and isinstance(item.get("receipt_id"), str)
        }
        candidates = candidate_sets[0].get("candidate_set", {}).get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("VALID candidate set cannot be empty")
        for candidate in candidates:
            place = receipt_by_id.get(candidate.get("place_receipt_id"))
            routes = [receipt_by_id.get(value) for value in candidate.get("route_receipt_ids", [])]
            if (
                not isinstance(place, Mapping)
                or place.get("operation") != "place.resolve"
                or not routes
                or any(
                    not isinstance(route, Mapping) or route.get("operation") != "route.candidate" for route in routes
                )
            ):
                raise ValueError("candidate is not closed by place and route receipts")
    elif candidate_sets:
        raise ValueError(f"{mode} candidate path must not materialize a candidate set")
    return mode


def _validate_fault(case: Mapping[str, Any], materialization: Mapping[str, Any]) -> str:
    case_id = str(case["case_id"])
    fault = str(case["runner_control"]["fault_profile_id"])
    if fault not in _REASON_BY_FAULT:
        raise ValueError("fault profile is outside the frozen mapping")
    artifact = materialization.get("fault_script")
    if not isinstance(artifact, Mapping) or not _artifact_hash_is_valid(artifact):
        raise ValueError("fault artifact hash mismatch")
    script = artifact.get("script")
    if (
        artifact.get("fault_profile_id") != fault
        or not isinstance(script, Mapping)
        or script.get("case_id") != case_id
        or script.get("fault_profile_id") != fault
        or script.get("script_sha256")
        != digest({key: value for key, value in script.items() if key != "script_sha256"})
    ):
        raise ValueError("fault script binding mismatch")
    if fault in _CONCURRENCY_BY_FAULT:
        attempts = script.get("attempts")
        barrier = script.get("barrier")
        if not isinstance(attempts, list) or len(attempts) != 2 or not isinstance(barrier, Mapping):
            raise ValueError("concurrency script must contain two barrier-bound attempts")
        first, second = sorted(attempts, key=lambda item: item.get("ordinal"))
        if [first.get("ordinal"), second.get("ordinal")] != [0, 1]:
            raise ValueError("concurrency attempt ordinals are invalid")
        if fault == "duplicate_apply":
            if (
                first.get("idempotency_key") != second.get("idempotency_key")
                or first.get("repair_id") != second.get("repair_id")
                or barrier.get("mode") != "SEQUENTIAL_REPLAY"
            ):
                raise ValueError("duplicate apply script is not an idempotent replay")
        elif (
            first.get("idempotency_key") == second.get("idempotency_key")
            or first.get("base_revision") != second.get("base_revision")
            or barrier.get("mode") != "ARRIVE_ALL_THEN_ORDERED_RELEASE"
        ):
            raise ValueError("concurrent apply script is not a single-base-revision race")
    elif script.get("concurrency_expectation") != "NONE":
        raise ValueError("non-concurrent fault declared a concurrency expectation")
    return fault


def _validate_evidence_unknown(case: Mapping[str, Any], materialization: Mapping[str, Any]) -> bool:
    evidence = materialization.get("evidence_snapshot")
    if not isinstance(evidence, Mapping) or not _artifact_hash_is_valid(evidence):
        raise ValueError("evidence snapshot hash mismatch")
    snapshot = evidence.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("evidence snapshot payload is missing")
    facts = snapshot.get("facts")
    failures = snapshot.get("provider_failures")
    if not isinstance(facts, list) or not isinstance(failures, list):
        raise ValueError("evidence status fields are invalid")
    has_unavailable = any(
        isinstance(fact, Mapping)
        and (
            fact.get("freshness_status") in {"UNKNOWN", "UNAVAILABLE"}
            or (isinstance(fact.get("value"), str) and fact.get("value") in {"UNKNOWN", "UNAVAILABLE"})
        )
        for fact in facts
    ) or any(
        isinstance(failure, Mapping)
        and failure.get("error_category") in {"UNKNOWN", "UNAVAILABLE", "PROVIDER_ROUTE_UNAVAILABLE"}
        for failure in failures
    )
    frozen_required = case["runner_control"].get("unknown_required") is True
    if frozen_required != has_unavailable:
        raise ValueError("unknown_required does not match actual UNKNOWN/UNAVAILABLE evidence")
    return frozen_required and has_unavailable


def _validate_ocr(case: Mapping[str, Any], materialization: Mapping[str, Any]) -> bool:
    required = case.get("input_kind") == "SYNTHETIC_SCREENSHOT"
    ocr = materialization.get("ocr_baseline_receipt")
    render = materialization.get("render_receipt")
    if not required:
        if ocr is not None or render is not None:
            raise ValueError("text case cannot carry OCR materialization")
        return False
    if not isinstance(ocr, Mapping) or not isinstance(render, Mapping):
        raise ValueError("screenshot case requires render and OCR receipts")
    if (ocr.get("engine"), ocr.get("engine_version")) != ("paddleocr", "3.7.0"):
        raise ValueError("screenshot OCR is not the frozen production engine")
    if ocr.get("asset_hash") != render.get("image_sha256"):
        raise ValueError("OCR and render receipts bind different screenshot bytes")
    cleanup = [
        item
        for item in materialization.get("receipts", [])
        if isinstance(item, Mapping) and item.get("schema_version") == "trip-check-p5-cleanup-receipt-v2"
    ]
    if (
        len(cleanup) != 1
        or cleanup[0].get("cleanup_status") != "DELETED"
        or cleanup[0].get("original_removed") is not True
        or cleanup[0].get("asset_hash") != ocr.get("asset_hash")
    ):
        raise ValueError("screenshot cleanup receipt is not closed")
    return True


def derive_blind_oracle_v2(case: Mapping[str, Any], materialization: Mapping[str, Any]) -> P5OracleV2:
    """Derive one oracle without access to labels or candidate outputs."""

    validated = P5CaseV2.model_validate(case)
    if (
        validated.split != "frozen_blind"
        or validated.oracle is not None
        or {"oracle", "oracle_sha256", "expected", "label"}.intersection(case)
    ):
        raise ValueError("custody derivation only accepts label-free frozen blind cases")
    if case.get("provenance", {}).get("contains_human_data") is not False:
        raise ValueError("blind custody inputs must be synthetic and contain no human data")
    if case.get("case_hash") != digest({key: value for key, value in case.items() if key != "case_hash"}):
        raise ValueError("blind case hash mismatch")
    if materialization.get("case_id") != case.get("case_id"):
        raise ValueError("case/materialization ID mismatch")
    if materialization.get("materialization_hash") != digest(
        {key: value for key, value in materialization.items() if key != "materialization_hash"}
    ):
        raise ValueError("materialization hash mismatch")
    if case["materialization"].get("materialization_sha256") != materialization.get("materialization_hash"):
        raise ValueError("case materialization binding is stale")
    for field in ("source_payload", "provider_snapshot", "evidence_snapshot", "fault_script"):
        artifact = materialization.get(field)
        binding = case["materialization"].get(field)
        if (
            not isinstance(artifact, Mapping)
            or not isinstance(binding, Mapping)
            or not _artifact_hash_is_valid(artifact)
            or binding.get("artifact_id") != artifact.get("artifact_id")
            or binding.get("schema_version") != artifact.get("schema_version")
            or binding.get("content_sha256") != artifact.get("content_sha256")
        ):
            raise ValueError(f"{field} artifact binding mismatch")
    candidate_artifacts = materialization.get("candidate_sets")
    candidate_bindings = case["materialization"].get("candidate_sets")
    if not isinstance(candidate_artifacts, list) or not isinstance(candidate_bindings, list):
        raise ValueError("candidate set binding is invalid")
    expected_candidate_bindings = [
        {
            "artifact_id": artifact.get("artifact_id"),
            "schema_version": artifact.get("schema_version"),
            "content_sha256": artifact.get("content_sha256"),
        }
        for artifact in candidate_artifacts
        if isinstance(artifact, Mapping) and _artifact_hash_is_valid(artifact)
    ]
    if (
        len(expected_candidate_bindings) != len(candidate_artifacts)
        or candidate_bindings != expected_candidate_bindings
    ):
        raise ValueError("candidate set artifact binding mismatch")
    for receipt_field, materialization_field in (
        ("render_receipt", "render_receipt"),
        ("ocr_baseline_receipt", "ocr_baseline_receipt"),
    ):
        receipt = materialization.get(materialization_field)
        receipt_binding = case["materialization"].get(receipt_field)
        if (receipt is None) != (receipt_binding is None):
            raise ValueError(f"{receipt_field} presence binding mismatch")
        if receipt is not None and (
            not isinstance(receipt, Mapping)
            or not isinstance(receipt_binding, Mapping)
            or receipt_binding.get("schema_version") != receipt.get("schema_version")
            or receipt_binding.get("content_sha256") != digest(receipt)
        ):
            raise ValueError(f"{receipt_field} hash binding mismatch")
    if materialization["source_payload"].get("product_input") != case.get("product_input"):
        raise ValueError("source payload does not bind the frozen product input")
    control = case["runner_control"]
    provider = materialization["provider_snapshot"]
    if (
        provider.get("artifact_id") != control.get("provider_snapshot_id")
        or provider.get("fault_profile_id") != control.get("fault_profile_id")
        or provider.get("evidence_freshness") != control.get("evidence_freshness")
    ):
        raise ValueError("provider snapshot does not bind the frozen runner control")
    fault = _validate_fault(case, materialization)
    mode = _validate_candidate_path(case, materialization)
    unknown = _validate_evidence_unknown(case, materialization)
    ocr_required = _validate_ocr(case, materialization)
    expectation = _CONCURRENCY_BY_FAULT.get(fault, "NONE")
    return P5OracleV2(
        task_success_required=True,
        requires_user_resolution=mode in {"EMPTY", "MISSING_RECEIPT"},
        required_reason_codes=[_REASON_BY_FAULT[fault]],
        wrong_city_or_poi_max=0,
        max_new_blocker_high_unknown=0,
        unknown_must_be_preserved=unknown,
        advice_required=True,
        specific_place_allowed=mode not in {"EMPTY", "MISSING_RECEIPT"},
        candidate_receipt_mode=_CANDIDATE_MODE_TO_ORACLE[mode],
        expected_strategy_outcome=_STRATEGY_BY_FAULT.get(fault, "FEASIBLE"),
        concurrency_expectation=expectation,
        ocr_required=ocr_required,
    )


def _paths(repo_root: Path) -> dict[str, Path]:
    return {
        "inputs": _safe_repo_file(repo_root, BLIND_INPUT_RELATIVE),
        "materializations": _safe_repo_file(repo_root, BLIND_MATERIALIZATIONS_RELATIVE),
        "manifest": _safe_repo_file(repo_root, DATASET_MANIFEST_RELATIVE),
        "run_spec": _safe_repo_file(repo_root, RUN_SPEC_RELATIVE),
        "rubric": _safe_repo_file(repo_root, RUBRIC_RELATIVE),
        "bundle_schema": _safe_repo_file(repo_root, BUNDLE_SCHEMA_RELATIVE),
    }


def _load_and_bind(repo_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], BlindDatasetBindingV2]:
    paths = _paths(repo_root)
    inputs = load_jsonl(paths["inputs"])
    materializations = load_jsonl(paths["materializations"])
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != "trip-check-p5-dataset-manifest-v2"
        or manifest.get("frozen") is not True
        or manifest.get("generation", {}).get("ocr_mode") != "actual"
        or manifest.get("generation", {}).get("formal_validation_eligible") is not True
        or manifest.get("evidence_boundary", {}).get("actual_ocr") != "PASS"
        or manifest.get("manifest_hash")
        != digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
    ):
        raise ValueError("dataset manifest is not a self-bound formal actual-OCR freeze")
    input_ids = [item.get("case_id") for item in inputs]
    materialization_ids = [item.get("case_id") for item in materializations]
    if len(inputs) != 90 or len(set(input_ids)) != 90 or set(input_ids) != set(materialization_ids):
        raise ValueError("blind inputs/materializations do not have exact 90-case coverage")
    blind_cases = manifest.get("files", {}).get("blind_cases", {})
    blind_materializations = manifest.get("files", {}).get("blind_materializations", {})
    expected_file_bindings = (
        (blind_cases, paths["inputs"], inputs),
        (blind_materializations, paths["materializations"], materializations),
    )
    for entry, path, rows in expected_file_bindings:
        if (
            entry.get("row_count") != 90
            or entry.get("file_sha256") != file_sha256(path)
            or entry.get("content_sha256") != digest(rows)
        ):
            raise ValueError("dataset manifest has a stale blind artifact binding")
    expected_case_set_hash = digest(
        sorted(
            ({"case_id": item["case_id"], "case_hash": item["case_hash"]} for item in inputs),
            key=lambda item: item["case_id"],
        )
    )
    expected_materialization_set_hash = digest(
        sorted(
            (
                {
                    "case_id": item["case_id"],
                    "materialization_id": item["materialization_id"],
                    "materialization_hash": item["materialization_hash"],
                }
                for item in materializations
            ),
            key=lambda item: item["case_id"],
        )
    )
    blind_lane = manifest.get("lanes", {}).get("frozen_blind", {})
    if (
        blind_lane.get("case_count") != 90
        or blind_lane.get("materialization_count") != 90
        or blind_lane.get("case_set_hash") != expected_case_set_hash
        or blind_lane.get("materialization_set_hash") != expected_materialization_set_hash
        or blind_lane.get("label_payload_present") is not False
        or blind_lane.get("label_storage") != "external_bundle_only"
    ):
        raise ValueError("dataset manifest blind lane binding is invalid")
    if manifest.get("contract_hashes") != {
        "judge_rubric_sha256": file_sha256(paths["rubric"]),
        "run_spec_template_sha256": file_sha256(paths["run_spec"]),
    }:
        raise ValueError("dataset manifest contract hashes are stale")
    binding = BlindDatasetBindingV2(
        case_ids_sha256=digest(sorted(str(value) for value in input_ids)),
        inputs_file_sha256=file_sha256(paths["inputs"]),
        inputs_content_sha256=digest(inputs),
        materializations_file_sha256=file_sha256(paths["materializations"]),
        materializations_content_sha256=digest(materializations),
        schema_contract_sha256=schema_contract_sha256_v2(repo_root),
        run_spec_template_sha256=file_sha256(paths["run_spec"]),
        rubric_sha256=file_sha256(paths["rubric"]),
        variant_ids_sha256=digest(list(VARIANT_IDS_V2)),
    )
    return inputs, materializations, binding


def derive_all_blind_labels_v2(
    inputs: Sequence[Mapping[str, Any]], materializations: Sequence[Mapping[str, Any]]
) -> list[BlindLabelV2]:
    materialization_by_id = {str(item["case_id"]): item for item in materializations}
    labels = [
        BlindLabelV2(
            case_id=str(case["case_id"]),
            oracle=derive_blind_oracle_v2(case, materialization_by_id[str(case["case_id"])]),
        )
        for case in sorted(inputs, key=lambda item: str(item["case_id"]))
    ]
    if len(labels) != 90 or len({item.case_id for item in labels}) != 90:
        raise ValueError("derived labels do not exactly cover the blind case set")
    case_by_id = {str(case["case_id"]): case for case in inputs}
    fault_counts = Counter(str(case_by_id[label.case_id]["runner_control"]["fault_profile_id"]) for label in labels)
    mode_counts = Counter(str(case_by_id[label.case_id]["runner_control"]["candidate_set_mode"]) for label in labels)
    if fault_counts != Counter({fault: 10 for fault in _REASON_BY_FAULT}):
        raise ValueError("blind fault profiles must each cover exactly 10 cases")
    if mode_counts != Counter({"VALID": 10, "EMPTY": 10, "MISSING_RECEIPT": 10, "NOT_APPLICABLE": 60}):
        raise ValueError("blind CandidateSet paths must keep the frozen 10/10/10 distribution")
    if sum(label.oracle.unknown_must_be_preserved for label in labels) != 18:
        raise ValueError("blind UNKNOWN preservation count must be exactly 18")
    if sum(label.oracle.concurrency_expectation != "NONE" for label in labels) != 20:
        raise ValueError("blind concurrency coverage must be exactly 20")
    return labels


def _validate_bundle_schema(repo_root: Path, bundle: Mapping[str, Any]) -> None:
    schema_path = _safe_repo_file(repo_root, BUNDLE_SCHEMA_RELATIVE)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(bundle)
    BlindLabelBundleV2.model_validate(bundle)


def _atomic_external_write(repo_root: Path, output_path: Path, payload: bytes) -> Path:
    destination = validate_external_output_path(repo_root, output_path)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="xb", dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        validate_external_output_path(repo_root, destination)
        os.replace(temporary, destination)
        temporary = None
        return destination
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def build_blind_label_bundle_v2(*, repo_root: Path, external_output_path: Path) -> dict[str, Any]:
    inputs, materializations, binding = _load_and_bind(repo_root)
    labels = derive_all_blind_labels_v2(inputs, materializations)
    bundle = BlindLabelBundleV2(dataset_binding=binding, labels=labels).model_dump(mode="json")
    _validate_bundle_schema(repo_root, bundle)
    payload = canonical_bytes(bundle) + b"\n"
    destination = _atomic_external_write(repo_root, external_output_path, payload)
    return {
        "path": str(destination),
        "bundle_byte_sha256": hashlib.sha256(payload).hexdigest(),
        "bundle_canonical_sha256": digest(bundle),
        "labels_canonical_sha256": canonical_labels_hash_v2(bundle["labels"]),
        "case_count": 90,
    }


def _privacy_findings(value: Any) -> list[str]:
    findings: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            findings.extend(pattern.pattern for pattern in _PRIVATE_PATTERNS if pattern.search(item))
        elif isinstance(item, Mapping):
            for key, nested in item.items():
                visit(key)
                visit(nested)
        elif isinstance(item, Sequence) and not isinstance(item, (bytes, bytearray)):
            for nested in item:
                visit(nested)

    visit(value)
    return findings


def review_blind_label_bundle_v2(
    *,
    repo_root: Path,
    external_bundle_path: Path,
    external_receipt_path: Path,
    candidate_subject_commit: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", candidate_subject_commit):
        raise ValueError("candidate subject commit must be a full lowercase git SHA")
    bundle_path = external_bundle_path
    if not bundle_path.is_absolute() or ".." in bundle_path.parts:
        raise ValueError("bundle path must be an absolute external path")
    absolute_bundle = bundle_path.absolute()
    if _contains_link_or_junction(absolute_bundle):
        raise ValueError("bundle path cannot contain a symlink or junction")
    resolved_bundle = absolute_bundle.resolve(strict=True)
    if _inside(resolved_bundle, repo_root.resolve(strict=True)):
        raise ValueError("bundle path must be outside the repository")
    bundle_bytes = resolved_bundle.read_bytes()
    bundle = json.loads(bundle_bytes.decode("utf-8"))
    _validate_bundle_schema(repo_root, bundle)
    inputs, materializations, binding = _load_and_bind(repo_root)
    expected_labels = derive_all_blind_labels_v2(inputs, materializations)
    expected_bundle = BlindLabelBundleV2(
        dataset_binding=binding,
        labels=expected_labels,
    ).model_dump(mode="json")
    if bundle != expected_bundle or bundle_bytes != canonical_bytes(expected_bundle) + b"\n":
        raise ValueError("bundle differs from independently derived canonical oracle")
    mode_counts = Counter(str(case["runner_control"]["candidate_set_mode"]) for case in inputs)
    unknown_count = sum(label.oracle.unknown_must_be_preserved for label in expected_labels)
    concurrency_count = sum(label.oracle.concurrency_expectation != "NONE" for label in expected_labels)
    privacy = _privacy_findings([inputs, bundle])
    checks = BlindReviewChecksV2(
        privacy_findings_count=len(privacy),
        unknown_preserved_count=unknown_count,
        candidate_set_mode_counts=CandidateModeCountsV2.model_validate(dict(mode_counts)),
        concurrency_case_count=concurrency_count,
    )
    receipt_payload: dict[str, Any] = {
        "schema_version": "trip-check-p5-blind-review-receipt-v2",
        "review_status": "PASS",
        "candidate_subject_commit": candidate_subject_commit,
        "bundle_byte_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
        "bundle_canonical_sha256": digest(bundle),
        "labels_canonical_sha256": canonical_labels_hash_v2(bundle["labels"]),
        "oracle_derivation_sha256": digest(
            [
                {"case_id": label.case_id, "oracle_sha256": digest(label.oracle.model_dump(mode="json"))}
                for label in expected_labels
            ]
        ),
        "dataset_binding": binding.model_dump(mode="json"),
        "checks": checks.model_dump(mode="json"),
    }
    receipt_payload["receipt_sha256"] = digest(receipt_payload)
    receipt = BlindReviewReceiptV2.model_validate(receipt_payload).model_dump(mode="json")
    review_schema = json.loads(_safe_repo_file(repo_root, REVIEW_SCHEMA_RELATIVE).read_text(encoding="utf-8"))
    Draft202012Validator(review_schema).validate(receipt)
    destination = _atomic_external_write(repo_root, external_receipt_path, canonical_bytes(receipt) + b"\n")
    return {
        "path": str(destination),
        "review_receipt_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "receipt_canonical_sha256": digest(receipt),
        "labels_canonical_sha256": receipt["labels_canonical_sha256"],
        "bundle_byte_sha256": receipt["bundle_byte_sha256"],
        "case_count": 90,
    }
