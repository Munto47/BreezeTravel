"""Validate repository-external P5 v2 blind custody artifacts.

This module is deliberately consumer-only.  It validates schemas and frozen
commitments supplied by an independent custodian/reviewer, but contains no
oracle construction rules and cannot create a blind label bundle or review
receipt from the tracked blind inputs.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from evals.trip_check_v1.p5.contracts_v2 import VARIANT_IDS_V2
from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest, file_sha256, load_jsonl
from evals.trip_check_v1.p5.final_blind_scorer_v2 import (
    canonical_labels_hash_v2,
    schema_contract_sha256_v2,
)


BLIND_INPUT_RELATIVE = Path("backend/evals/trip_check_v1/p5/frozen_blind.v2.inputs.jsonl")
BLIND_MATERIALIZATIONS_RELATIVE = Path(
    "backend/evals/trip_check_v1/p5/frozen_blind.v2.materializations.jsonl"
)
DATASET_MANIFEST_RELATIVE = Path("backend/evals/trip_check_v1/p5/dataset_v2.manifest.json")
RUN_SPEC_RELATIVE = Path("backend/evals/trip_check_v1/p5/run_spec_template_v2.json")
RUBRIC_RELATIVE = Path("backend/evals/trip_check_v1/p5/judge_rubric_v2.json")
BUNDLE_SCHEMA_RELATIVE = Path("backend/evals/trip_check_v1/p5/blind_bundle_v2.schema.json")
REVIEW_SCHEMA_RELATIVE = Path("backend/evals/trip_check_v1/p5/blind_review_receipt_v2.schema.json")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class P5ExternalCustodyContractError(ValueError):
    """Stable fail-closed error for external custody contract violations."""


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


def _repo_file(repo_root: Path, relative: Path) -> Path:
    root = repo_root.resolve(strict=True)
    candidate = (root / relative).absolute()
    if relative.is_absolute() or ".." in relative.parts or _contains_link_or_junction(candidate):
        raise P5ExternalCustodyContractError(f"unsafe repository contract path: {relative.as_posix()}")
    resolved = candidate.resolve(strict=True)
    if not _inside(resolved, root) or not resolved.is_file():
        raise P5ExternalCustodyContractError(f"repository contract path escaped: {relative.as_posix()}")
    return resolved


def _external_file(repo_root: Path, path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise P5ExternalCustodyContractError("custody artifact path must be absolute without traversal")
    absolute = path.absolute()
    if _contains_link_or_junction(absolute):
        raise P5ExternalCustodyContractError("custody artifact path cannot contain a symlink or junction")
    resolved = absolute.resolve(strict=True)
    if not resolved.is_file() or _inside(resolved, repo_root.resolve(strict=True)):
        raise P5ExternalCustodyContractError("custody artifact must be a file outside the repository")
    return resolved


def _require_sha256(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise P5ExternalCustodyContractError(f"invalid sha256 commitment: {field}")
    return value


def _read_schema(repo_root: Path, relative: Path) -> dict[str, Any]:
    try:
        value = json.loads(_repo_file(repo_root, relative).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5ExternalCustodyContractError("custody schema is unreadable") from exc
    if not isinstance(value, dict):
        raise P5ExternalCustodyContractError("custody schema must be a JSON object")
    return value


def _read_external_json(repo_root: Path, path: Path, expected_sha256: str) -> tuple[dict[str, Any], bytes]:
    resolved = _external_file(repo_root, path)
    payload = resolved.read_bytes()
    if hashlib.sha256(payload).hexdigest() != _require_sha256(expected_sha256, "artifact"):
        raise P5ExternalCustodyContractError("external custody artifact byte hash mismatch")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise P5ExternalCustodyContractError("external custody artifact is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise P5ExternalCustodyContractError("external custody artifact must be a JSON object")
    return value, payload


def expected_blind_dataset_binding_v2(repo_root: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Recompute only public dataset commitments; no oracle values are inspected."""

    inputs_path = _repo_file(repo_root, BLIND_INPUT_RELATIVE)
    materializations_path = _repo_file(repo_root, BLIND_MATERIALIZATIONS_RELATIVE)
    manifest_path = _repo_file(repo_root, DATASET_MANIFEST_RELATIVE)
    run_spec_path = _repo_file(repo_root, RUN_SPEC_RELATIVE)
    rubric_path = _repo_file(repo_root, RUBRIC_RELATIVE)
    inputs = load_jsonl(inputs_path)
    materializations = load_jsonl(materializations_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5ExternalCustodyContractError("dataset manifest is unreadable") from exc
    case_ids = tuple(str(row.get("case_id", "")) for row in inputs)
    materialization_ids = {str(row.get("case_id", "")) for row in materializations}
    if (
        len(inputs) != 90
        or len(materializations) != 90
        or len(set(case_ids)) != 90
        or materialization_ids != set(case_ids)
    ):
        raise P5ExternalCustodyContractError("blind dataset does not have exact 90-case coverage")
    files = manifest.get("files") if isinstance(manifest, Mapping) else None
    blind_cases = files.get("blind_cases") if isinstance(files, Mapping) else None
    blind_materializations = files.get("blind_materializations") if isinstance(files, Mapping) else None
    if not isinstance(blind_cases, Mapping) or not isinstance(blind_materializations, Mapping):
        raise P5ExternalCustodyContractError("dataset manifest blind bindings are missing")
    for entry, path, rows in (
        (blind_cases, inputs_path, inputs),
        (blind_materializations, materializations_path, materializations),
    ):
        if (
            entry.get("row_count") != 90
            or entry.get("file_sha256") != file_sha256(path)
            or entry.get("content_sha256") != digest(rows)
        ):
            raise P5ExternalCustodyContractError("dataset manifest blind binding is stale")
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != "trip-check-p5-dataset-manifest-v2"
        or manifest.get("frozen") is not True
        or manifest.get("generation", {}).get("ocr_mode") != "actual"
        or manifest.get("generation", {}).get("formal_validation_eligible") is not True
        or manifest.get("evidence_boundary", {}).get("actual_ocr") != "PASS"
        or manifest.get("manifest_hash")
        != digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
    ):
        raise P5ExternalCustodyContractError("dataset manifest is not a frozen self-bound contract")
    lane = manifest.get("lanes", {}).get("frozen_blind", {})
    expected_case_set_hash = digest(
        sorted(
            ({"case_id": row["case_id"], "case_hash": row["case_hash"]} for row in inputs),
            key=lambda item: item["case_id"],
        )
    )
    expected_materialization_set_hash = digest(
        sorted(
            (
                {
                    "case_id": row["case_id"],
                    "materialization_id": row["materialization_id"],
                    "materialization_hash": row["materialization_hash"],
                }
                for row in materializations
            ),
            key=lambda item: item["case_id"],
        )
    )
    if (
        lane.get("case_count") != 90
        or lane.get("materialization_count") != 90
        or lane.get("case_set_hash") != expected_case_set_hash
        or lane.get("materialization_set_hash") != expected_materialization_set_hash
        or lane.get("label_payload_present") is not False
        or lane.get("label_storage") != "external_bundle_only"
    ):
        raise P5ExternalCustodyContractError("dataset manifest blind lane commitment mismatch")
    if manifest.get("contract_hashes") != {
        "judge_rubric_sha256": file_sha256(rubric_path),
        "run_spec_template_sha256": file_sha256(run_spec_path),
    }:
        raise P5ExternalCustodyContractError("dataset manifest contract hashes are stale")
    binding = {
        "case_count": 90,
        "case_ids_sha256": digest(sorted(case_ids)),
        "inputs_file_sha256": file_sha256(inputs_path),
        "inputs_content_sha256": digest(inputs),
        "materializations_file_sha256": file_sha256(materializations_path),
        "materializations_content_sha256": digest(materializations),
        "schema_contract_sha256": schema_contract_sha256_v2(repo_root),
        "run_spec_template_sha256": file_sha256(run_spec_path),
        "rubric_sha256": file_sha256(rubric_path),
        "variant_ids_sha256": digest(list(VARIANT_IDS_V2)),
    }
    return binding, tuple(sorted(case_ids))


def validate_external_blind_bundle_v2(
    *,
    repo_root: Path,
    bundle_path: Path,
    expected_bundle_sha256: str,
    expected_labels_canonical_sha256: str,
    expected_dataset_binding: Mapping[str, Any],
    expected_case_ids: Sequence[str],
) -> dict[str, Any]:
    """Validate external bundle structure and commitments without judging oracle meaning."""

    bundle, payload = _read_external_json(repo_root, bundle_path, expected_bundle_sha256)
    errors = sorted(
        Draft202012Validator(_read_schema(repo_root, BUNDLE_SCHEMA_RELATIVE)).iter_errors(bundle),
        key=lambda item: list(item.path),
    )
    if errors:
        raise P5ExternalCustodyContractError("external blind bundle schema mismatch")
    if bundle.get("dataset_binding") != dict(expected_dataset_binding):
        raise P5ExternalCustodyContractError("external blind bundle dataset commitment mismatch")
    labels = bundle.get("labels")
    if not isinstance(labels, list):
        raise P5ExternalCustodyContractError("external blind bundle labels are missing")
    case_ids = [item.get("case_id") for item in labels if isinstance(item, Mapping)]
    if len(case_ids) != 90 or tuple(sorted(case_ids)) != tuple(sorted(expected_case_ids)):
        raise P5ExternalCustodyContractError("external blind bundle case-set commitment mismatch")
    labels_hash = canonical_labels_hash_v2(labels)
    if labels_hash != _require_sha256(expected_labels_canonical_sha256, "labels"):
        raise P5ExternalCustodyContractError("external blind label commitment mismatch")
    return {
        "status": "PASS",
        "case_count": 90,
        "bundle_byte_sha256": hashlib.sha256(payload).hexdigest(),
        "bundle_canonical_sha256": digest(bundle),
        "labels_canonical_sha256": labels_hash,
        "dataset_binding": dict(expected_dataset_binding),
    }


def validate_external_blind_review_receipt_v2(
    *,
    repo_root: Path,
    receipt_path: Path,
    expected_receipt_sha256: str,
    expected_candidate_subject_commit: str,
    expected_bundle_sha256: str,
    expected_bundle_canonical_sha256: str,
    expected_labels_canonical_sha256: str,
    expected_dataset_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate independent review receipt structure and frozen commitments only."""

    if not _COMMIT_RE.fullmatch(expected_candidate_subject_commit):
        raise P5ExternalCustodyContractError("candidate subject commit must be a full lowercase SHA")
    receipt, payload = _read_external_json(repo_root, receipt_path, expected_receipt_sha256)
    errors = sorted(
        Draft202012Validator(_read_schema(repo_root, REVIEW_SCHEMA_RELATIVE)).iter_errors(receipt),
        key=lambda item: list(item.path),
    )
    if errors:
        raise P5ExternalCustodyContractError("external blind review receipt schema mismatch")
    self_hash = receipt.get("receipt_sha256")
    if self_hash != digest({key: value for key, value in receipt.items() if key != "receipt_sha256"}):
        raise P5ExternalCustodyContractError("external blind review self-hash mismatch")
    if receipt.get("candidate_subject_commit") != expected_candidate_subject_commit:
        raise P5ExternalCustodyContractError("external blind review subject commitment mismatch")
    if receipt.get("bundle_byte_sha256") != _require_sha256(expected_bundle_sha256, "bundle"):
        raise P5ExternalCustodyContractError("external blind review bundle commitment mismatch")
    if receipt.get("bundle_canonical_sha256") != _require_sha256(
        expected_bundle_canonical_sha256, "canonical bundle"
    ):
        raise P5ExternalCustodyContractError("external blind review canonical bundle commitment mismatch")
    if receipt.get("labels_canonical_sha256") != _require_sha256(
        expected_labels_canonical_sha256, "labels"
    ):
        raise P5ExternalCustodyContractError("external blind review label commitment mismatch")
    if receipt.get("dataset_binding") != dict(expected_dataset_binding):
        raise P5ExternalCustodyContractError("external blind review dataset commitment mismatch")
    if payload != canonical_bytes(receipt) + b"\n":
        raise P5ExternalCustodyContractError("external blind review receipt is not canonical JSON")
    return {
        "status": "PASS",
        "candidate_subject_commit": expected_candidate_subject_commit,
        "review_receipt_sha256": hashlib.sha256(payload).hexdigest(),
        "labels_canonical_sha256": receipt["labels_canonical_sha256"],
        "bundle_byte_sha256": receipt["bundle_byte_sha256"],
        "dataset_binding": dict(expected_dataset_binding),
    }


__all__ = [
    "P5ExternalCustodyContractError",
    "expected_blind_dataset_binding_v2",
    "validate_external_blind_bundle_v2",
    "validate_external_blind_review_receipt_v2",
]
