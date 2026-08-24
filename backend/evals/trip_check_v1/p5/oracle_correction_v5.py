"""External-only P5 v5 oracle correction performed by the custodian."""

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


_POLICY = {
    "VALID": True,
    "EMPTY": False,
    "MISSING_RECEIPT": False,
    "NOT_APPLICABLE": True,
}
_CHANGED_FIELD = "oracle.specific_place_allowed"
_CORRECTION_SCOPE = "specific_place_allowed_payload_policy_only"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class P5OracleCorrectionErrorV5(RuntimeError):
    """Aggregate-only fail-closed error; messages never contain case details."""


def _reject_unsafe_path(path: Path, reason: str) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise P5OracleCorrectionErrorV5(reason)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if not current.exists():
            break
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            raise P5OracleCorrectionErrorV5(reason)


def _external_input(repo_root: Path, path: Path) -> Path:
    _reject_unsafe_path(path, "EXTERNAL_INPUT_LINK_FORBIDDEN")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        if resolved.is_file():
            return resolved
    except OSError as exc:
        raise P5OracleCorrectionErrorV5("EXTERNAL_INPUT_UNREADABLE") from exc
    raise P5OracleCorrectionErrorV5("EXTERNAL_INPUT_MUST_BE_OUTSIDE_REPOSITORY")


def _external_output(repo_root: Path, path: Path) -> Path:
    _reject_unsafe_path(path, "EXTERNAL_OUTPUT_LINK_FORBIDDEN")
    resolved = path.resolve(strict=False)
    try:
        resolved.resolve().relative_to(repo_root.resolve())
    except ValueError:
        if resolved.exists():
            raise P5OracleCorrectionErrorV5("EXTERNAL_OUTPUT_OVERWRITE_FORBIDDEN")
        return resolved
    raise P5OracleCorrectionErrorV5("EXTERNAL_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
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
        raise P5OracleCorrectionErrorV5(reason) from exc
    if not isinstance(value, dict):
        raise P5OracleCorrectionErrorV5(reason)
    return value


def _load_source_bundle(
    *, repo_root: Path, source_bundle_path: Path, source_anchor: Mapping[str, Any]
) -> dict[str, Any]:
    source = _external_input(repo_root, source_bundle_path)
    bundle = _load_json(source, "SOURCE_BUNDLE_INVALID")
    schema_path = repo_root / "backend" / "evals" / "trip_check_v1" / "p5" / "blind_bundle_v2.schema.json"
    schema = _load_json(schema_path, "SOURCE_BUNDLE_SCHEMA_INVALID")
    if list(Draft202012Validator(schema).iter_errors(bundle)):
        raise P5OracleCorrectionErrorV5("SOURCE_BUNDLE_SCHEMA_REJECTED")
    labels = bundle.get("labels")
    if (
        file_sha256(source) != source_anchor.get("external_bundle_sha256")
        or not isinstance(labels, list)
        or canonical_labels_hash_v2(labels) != source_anchor.get("labels_canonical_sha256")
        or source.read_bytes() != canonical_bytes(bundle) + b"\n"
    ):
        raise P5OracleCorrectionErrorV5("SOURCE_BUNDLE_COMMITMENT_MISMATCH")
    _binding, case_ids = expected_blind_dataset_binding_v5(repo_root)
    if len(labels) != 90 or {item.get("case_id") for item in labels if isinstance(item, Mapping)} != set(case_ids):
        raise P5OracleCorrectionErrorV5("SOURCE_BUNDLE_CASE_SET_MISMATCH")
    return bundle


def _case_modes() -> dict[str, str]:
    result: dict[str, str] = {}
    for row in load_jsonl(BLIND_INPUT_PATH_V5):
        control = row.get("runner_control")
        if not isinstance(control, Mapping) or control.get("candidate_set_mode") not in _POLICY:
            raise P5OracleCorrectionErrorV5("PUBLIC_CANDIDATE_POLICY_INVALID")
        result[str(row.get("case_id"))] = str(control["candidate_set_mode"])
    if len(result) != 90:
        raise P5OracleCorrectionErrorV5("PUBLIC_CASE_SET_INVALID")
    return result


def _corrected_bundle(
    *, source_bundle: Mapping[str, Any], dataset_binding: Mapping[str, Any]
) -> tuple[dict[str, Any], int]:
    corrected = copy.deepcopy(dict(source_bundle))
    corrected["dataset_binding"] = dict(dataset_binding)
    modes = _case_modes()
    changed = 0
    for label in corrected["labels"]:
        if not isinstance(label, dict) or not isinstance(label.get("oracle"), dict):
            raise P5OracleCorrectionErrorV5("SOURCE_LABEL_CONTRACT_INVALID")
        oracle = label["oracle"]
        current = oracle.get("specific_place_allowed")
        expected = _POLICY[modes[str(label.get("case_id"))]]
        if not isinstance(current, bool):
            raise P5OracleCorrectionErrorV5("SOURCE_ORACLE_FIELD_INVALID")
        if current is not expected:
            oracle["specific_place_allowed"] = expected
            changed += 1
    if changed != 60:
        raise P5OracleCorrectionErrorV5("TARGET_DIFF_COUNT_NOT_60")
    return corrected, changed


def correct_external_oracle_v5(
    *,
    repo_root: Path,
    source_bundle_path: Path,
    corrected_bundle_path: Path,
    correction_receipt_path: Path,
    candidate_subject_commit: str,
    entrypoint_path: Path,
    source_anchor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Correct the one authorized field and emit only an aggregate receipt."""

    if _COMMIT_RE.fullmatch(candidate_subject_commit) is None:
        raise P5OracleCorrectionErrorV5("CANDIDATE_SUBJECT_INVALID")
    validate_v4_v5_byte_identity(repo_root)
    anchor = dict(source_anchor or validate_v4_source_anchor(repo_root))
    source = _load_source_bundle(
        repo_root=repo_root,
        source_bundle_path=source_bundle_path,
        source_anchor=anchor,
    )
    binding, _case_ids = expected_blind_dataset_binding_v5(repo_root)
    corrected, changed = _corrected_bundle(source_bundle=source, dataset_binding=binding)
    bundle_output = _external_output(repo_root, corrected_bundle_path)
    receipt_output = _external_output(repo_root, correction_receipt_path)
    if bundle_output == receipt_output:
        raise P5OracleCorrectionErrorV5("EXTERNAL_OUTPUT_PATHS_MUST_BE_DISTINCT")
    bundle_bytes = canonical_bytes(corrected) + b"\n"
    labels_hash = canonical_labels_hash_v2(corrected["labels"])
    receipt: dict[str, Any] = {
        "schema_version": "trip-check-p5-blind-oracle-correction-receipt-v5",
        "status": "PASS",
        "candidate_subject_commit": candidate_subject_commit,
        "source_bundle_sha256": anchor["external_bundle_sha256"],
        "source_labels_canonical_sha256": anchor["labels_canonical_sha256"],
        "corrected_bundle_byte_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
        "corrected_bundle_canonical_sha256": digest(corrected),
        "corrected_labels_canonical_sha256": labels_hash,
        "dataset_binding": binding,
        "correction_scope": _CORRECTION_SCOPE,
        "changed_label_count": changed,
        "changed_field": _CHANGED_FIELD,
        "non_target_oracle_diff_count": 0,
        "blind_payload_changed": False,
        "policy_mapping_sha256": digest(_POLICY),
        "correction_tool_sha256": _tool_sha256(Path(__file__), entrypoint_path),
        "network_api_calls": 0,
        "blind_detail_emitted": False,
        "human_evidence": False,
    }
    receipt["receipt_sha256"] = digest(receipt)
    try:
        _atomic_write(bundle_output, bundle_bytes)
        _atomic_write(receipt_output, canonical_bytes(receipt) + b"\n")
    except Exception:
        bundle_output.unlink(missing_ok=True)
        raise
    return {
        "status": "PASS",
        "changed_label_count": 60,
        "non_target_oracle_diff_count": 0,
        "blind_payload_changed": False,
        "corrected_bundle_sha256": file_sha256(bundle_output),
        "corrected_labels_canonical_sha256": labels_hash,
        "correction_receipt_sha256": file_sha256(receipt_output),
    }
__all__ = [
    "P5OracleCorrectionErrorV5",
    "correct_external_oracle_v5",
]
