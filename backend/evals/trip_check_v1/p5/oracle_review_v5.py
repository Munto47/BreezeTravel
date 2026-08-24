"""Independent aggregate-only review of the P5 v5 external oracle correction."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from evals.trip_check_v1.p5.blind_external_contract_v5 import (
    expected_blind_dataset_binding_v5,
)
from evals.trip_check_v1.p5.data_contract import (
    canonical_bytes,
    digest,
    file_sha256,
    load_jsonl,
)
from evals.trip_check_v1.p5.data_contract_v5 import (
    BLIND_INPUT_PATH_V5,
    validate_v4_source_anchor,
    validate_v4_v5_byte_identity,
)
from evals.trip_check_v1.p5.final_blind_scorer_v2 import canonical_labels_hash_v2


_REVIEW_POLICY = {
    "VALID": True,
    "EMPTY": False,
    "MISSING_RECEIPT": False,
    "NOT_APPLICABLE": True,
}
_REVIEWED_FIELD = "oracle.specific_place_allowed"
_CORRECTION_SCOPE = "specific_place_allowed_payload_policy_only"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class P5OracleReviewErrorV5(RuntimeError):
    """Aggregate-only fail-closed review error without case details."""


def _reject_unsafe_path(path: Path, reason: str) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise P5OracleReviewErrorV5(reason)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if not current.exists():
            break
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            raise P5OracleReviewErrorV5(reason)


def _external_input(repo_root: Path, path: Path) -> Path:
    _reject_unsafe_path(path, "EXTERNAL_INPUT_LINK_FORBIDDEN")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        if resolved.is_file():
            return resolved
    except OSError as exc:
        raise P5OracleReviewErrorV5("EXTERNAL_INPUT_UNREADABLE") from exc
    raise P5OracleReviewErrorV5("EXTERNAL_INPUT_MUST_BE_OUTSIDE_REPOSITORY")


def _external_output(repo_root: Path, path: Path) -> Path:
    _reject_unsafe_path(path, "EXTERNAL_OUTPUT_LINK_FORBIDDEN")
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        if resolved.exists():
            raise P5OracleReviewErrorV5("EXTERNAL_OUTPUT_OVERWRITE_FORBIDDEN")
        return resolved
    raise P5OracleReviewErrorV5("EXTERNAL_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    staged = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _tool_sha256(module_path: Path, entrypoint_path: Path) -> str:
    return digest(
        {
            "module_sha256": file_sha256(module_path),
            "entrypoint_sha256": file_sha256(entrypoint_path),
        }
    )


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5OracleReviewErrorV5(reason) from exc
    if not isinstance(value, dict):
        raise P5OracleReviewErrorV5(reason)
    return value


def _load_source_bundle(
    *, repo_root: Path, source_bundle_path: Path, source_anchor: Mapping[str, Any]
) -> dict[str, Any]:
    source = _external_input(repo_root, source_bundle_path)
    bundle = _load_json(source, "SOURCE_BUNDLE_INVALID")
    schema_path = (
        repo_root
        / "backend"
        / "evals"
        / "trip_check_v1"
        / "p5"
        / "blind_bundle_v2.schema.json"
    )
    schema = _load_json(schema_path, "SOURCE_BUNDLE_SCHEMA_INVALID")
    if list(Draft202012Validator(schema).iter_errors(bundle)):
        raise P5OracleReviewErrorV5("SOURCE_BUNDLE_SCHEMA_REJECTED")
    labels = bundle.get("labels")
    if (
        file_sha256(source) != source_anchor.get("external_bundle_sha256")
        or not isinstance(labels, list)
        or canonical_labels_hash_v2(labels)
        != source_anchor.get("labels_canonical_sha256")
        or source.read_bytes() != canonical_bytes(bundle) + b"\n"
    ):
        raise P5OracleReviewErrorV5("SOURCE_BUNDLE_COMMITMENT_MISMATCH")
    _binding, case_ids = expected_blind_dataset_binding_v5(repo_root)
    if len(labels) != 90 or {
        item.get("case_id") for item in labels if isinstance(item, Mapping)
    } != set(case_ids):
        raise P5OracleReviewErrorV5("SOURCE_BUNDLE_CASE_SET_MISMATCH")
    return bundle


def _review_modes() -> dict[str, str]:
    result: dict[str, str] = {}
    for row in load_jsonl(BLIND_INPUT_PATH_V5):
        control = row.get("runner_control")
        if (
            not isinstance(control, Mapping)
            or control.get("candidate_set_mode") not in _REVIEW_POLICY
        ):
            raise P5OracleReviewErrorV5("PUBLIC_CANDIDATE_POLICY_INVALID")
        result[str(row.get("case_id"))] = str(control["candidate_set_mode"])
    if len(result) != 90:
        raise P5OracleReviewErrorV5("PUBLIC_CASE_SET_INVALID")
    return result


def review_external_oracle_v5(
    *,
    repo_root: Path,
    source_bundle_path: Path,
    corrected_bundle_path: Path,
    correction_receipt_path: Path,
    review_receipt_path: Path,
    candidate_subject_commit: str,
    correction_entrypoint_path: Path,
    reviewer_entrypoint_path: Path,
    source_anchor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconstruct the target without importing the correction implementation."""

    if _COMMIT_RE.fullmatch(candidate_subject_commit) is None:
        raise P5OracleReviewErrorV5("CANDIDATE_SUBJECT_INVALID")
    validate_v4_v5_byte_identity(repo_root)
    anchor = dict(source_anchor or validate_v4_source_anchor(repo_root))
    source = _load_source_bundle(
        repo_root=repo_root,
        source_bundle_path=source_bundle_path,
        source_anchor=anchor,
    )
    corrected_path = _external_input(repo_root, corrected_bundle_path)
    corrected = _load_json(corrected_path, "CORRECTED_BUNDLE_INVALID")
    binding, _case_ids = expected_blind_dataset_binding_v5(repo_root)
    independently_expected = copy.deepcopy(source)
    independently_expected["dataset_binding"] = dict(binding)
    modes = _review_modes()
    changed = 0
    for label in independently_expected["labels"]:
        oracle = label.get("oracle")
        case_id = str(label.get("case_id"))
        if not isinstance(oracle, dict) or case_id not in modes:
            raise P5OracleReviewErrorV5("REVIEW_SOURCE_LABEL_CONTRACT_INVALID")
        expected = _REVIEW_POLICY[modes[case_id]]
        current = oracle.get("specific_place_allowed")
        if not isinstance(current, bool):
            raise P5OracleReviewErrorV5("REVIEW_SOURCE_ORACLE_FIELD_INVALID")
        if current is not expected:
            oracle["specific_place_allowed"] = expected
            changed += 1
    if changed != 60:
        raise P5OracleReviewErrorV5("REVIEW_TARGET_DIFF_COUNT_NOT_60")
    if corrected != independently_expected or corrected_path.read_bytes() != (
        canonical_bytes(corrected) + b"\n"
    ):
        raise P5OracleReviewErrorV5("NON_TARGET_ORACLE_DIFF_DETECTED")

    correction_path = _external_input(repo_root, correction_receipt_path)
    correction = _load_json(correction_path, "CORRECTION_RECEIPT_INVALID")
    corrected_bytes = corrected_path.read_bytes()
    labels_hash = canonical_labels_hash_v2(corrected["labels"])
    correction_module_path = Path(__file__).with_name("oracle_correction_v5.py")
    correction_tool_hash = _tool_sha256(
        correction_module_path, correction_entrypoint_path
    )
    expected_correction = {
        "candidate_subject_commit": candidate_subject_commit,
        "source_bundle_sha256": anchor["external_bundle_sha256"],
        "source_labels_canonical_sha256": anchor["labels_canonical_sha256"],
        "corrected_bundle_byte_sha256": hashlib.sha256(corrected_bytes).hexdigest(),
        "corrected_bundle_canonical_sha256": digest(corrected),
        "corrected_labels_canonical_sha256": labels_hash,
        "dataset_binding": binding,
        "correction_scope": _CORRECTION_SCOPE,
        "changed_label_count": 60,
        "changed_field": _REVIEWED_FIELD,
        "non_target_oracle_diff_count": 0,
        "blind_payload_changed": False,
        "policy_mapping_sha256": digest(_REVIEW_POLICY),
        "correction_tool_sha256": correction_tool_hash,
        "network_api_calls": 0,
        "blind_detail_emitted": False,
        "human_evidence": False,
    }
    if (
        correction.get("schema_version")
        != "trip-check-p5-blind-oracle-correction-receipt-v5"
        or correction.get("status") != "PASS"
        or correction.get("receipt_sha256")
        != digest(
            {
                key: value
                for key, value in correction.items()
                if key != "receipt_sha256"
            }
        )
        or correction_path.read_bytes() != canonical_bytes(correction) + b"\n"
        or any(
            correction.get(key) != value
            for key, value in expected_correction.items()
        )
    ):
        raise P5OracleReviewErrorV5("CORRECTION_RECEIPT_BINDING_MISMATCH")

    reviewer_tool_hash = _tool_sha256(Path(__file__), reviewer_entrypoint_path)
    if reviewer_tool_hash == correction_tool_hash:
        raise P5OracleReviewErrorV5("REVIEWER_NOT_INDEPENDENT")
    review: dict[str, Any] = {
        "schema_version": "trip-check-p5-blind-review-receipt-v5",
        "review_status": "PASS",
        "candidate_subject_commit": candidate_subject_commit,
        "source_bundle_sha256": anchor["external_bundle_sha256"],
        "source_labels_canonical_sha256": anchor["labels_canonical_sha256"],
        "corrected_bundle_byte_sha256": hashlib.sha256(corrected_bytes).hexdigest(),
        "corrected_bundle_canonical_sha256": digest(corrected),
        "corrected_labels_canonical_sha256": labels_hash,
        "correction_receipt_sha256": file_sha256(correction_path),
        "dataset_binding": binding,
        "correction_scope": _CORRECTION_SCOPE,
        "reviewed_changed_label_count": changed,
        "reviewed_changed_field": _REVIEWED_FIELD,
        "non_target_oracle_diff_count": 0,
        "blind_payload_changed": False,
        "policy_mapping_sha256": digest(_REVIEW_POLICY),
        "correction_tool_sha256": correction_tool_hash,
        "reviewer_tool_sha256": reviewer_tool_hash,
        "independent_reviewer": True,
        "privacy_findings_count": 0,
        "disclosure_findings_count": 0,
        "network_api_calls": 0,
        "blind_detail_emitted": False,
        "human_evidence": False,
    }
    review["receipt_sha256"] = digest(review)
    output = _external_output(repo_root, review_receipt_path)
    _atomic_write(output, canonical_bytes(review) + b"\n")
    return {
        "review_status": "PASS",
        "reviewed_changed_label_count": 60,
        "non_target_oracle_diff_count": 0,
        "blind_payload_changed": False,
        "privacy_findings_count": 0,
        "disclosure_findings_count": 0,
        "review_receipt_sha256": file_sha256(output),
    }


__all__ = ["P5OracleReviewErrorV5", "review_external_oracle_v5"]
