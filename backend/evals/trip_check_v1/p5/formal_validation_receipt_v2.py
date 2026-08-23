"""Create a commit-bound, fail-closed receipt for formal P5 v2 dataset validation."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from evals.trip_check_v1.p5.data_contract import digest, file_sha256


BACKEND_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_ROOT.parent
P5_ROOT = BACKEND_ROOT / "evals" / "trip_check_v1" / "p5"
MANIFEST_PATH = P5_ROOT / "dataset_v2.manifest.json"
SCHEMA_PATH = P5_ROOT / "dataset_formal_validation_receipt_v2.schema.json"
VALIDATOR_PATH = BACKEND_ROOT / "scripts" / "validate_trip_check_p5_dataset_v2.py"
DEFAULT_RECEIPT_PATH = (
    BACKEND_ROOT / "evidence" / "trip_check_v1" / "p5" / "dataset_v2_formal_validation_receipt.json"
)
_DATASET_KEYS = (
    "nonblind_cases",
    "nonblind_materializations",
    "blind_cases",
    "blind_materializations",
)


class P5FormalValidationReceiptError(RuntimeError):
    """Formal receipt cannot be produced without weakening its provenance."""


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise P5FormalValidationReceiptError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _assert_clean_subject(repo_root: Path, subject_commit: str | None) -> str:
    actual_root = Path(_git(repo_root, "rev-parse", "--show-toplevel")).resolve()
    if actual_root != repo_root.resolve():
        raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_WRONG_REPOSITORY")
    head = _git(repo_root, "rev-parse", "HEAD")
    if subject_commit is not None and subject_commit != head:
        raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_MIXED_SUBJECT_COMMIT")
    if _git(repo_root, "status", "--porcelain", "--untracked-files=all"):
        raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_DIRTY_TREE")
    return head


def _repo_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_PATH_ESCAPE") from exc


def _validate_receipt(payload: Mapping[str, Any]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        raise P5FormalValidationReceiptError(f"P5_FORMAL_VALIDATION_RECEIPT_SCHEMA: {errors[0].message}")
    expected_hash = digest({key: value for key, value in payload.items() if key != "receipt_hash"})
    if payload.get("receipt_hash") != expected_hash:
        raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_RECEIPT_HASH_MISMATCH")


def _file_bindings(manifest: Mapping[str, Any], repo_root: Path) -> dict[str, dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {}
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, dict) or set(manifest_files) != set(_DATASET_KEYS):
        raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_MANIFEST_FILES_INVALID")
    for key in _DATASET_KEYS:
        entry = manifest_files[key]
        if not isinstance(entry, dict):
            raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_MANIFEST_FILES_INVALID")
        source_path = BACKEND_ROOT / str(entry.get("path", ""))
        if not source_path.is_file() or _repo_path(source_path, repo_root) != f"backend/{entry.get('path')}":
            raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_DATASET_PATH_INVALID")
        if file_sha256(source_path) != entry.get("file_sha256"):
            raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_DATASET_FILE_DRIFT")
        bindings[key] = {
            "path": _repo_path(source_path, repo_root),
            "file_sha256": entry["file_sha256"],
            "content_sha256": entry["content_sha256"],
            "row_count": entry["row_count"],
        }
    return bindings


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _immutable_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in {"created_at", "receipt_hash"}}


def generate_formal_validation_receipt(
    *,
    subject_commit: str | None = None,
    output_path: Path = DEFAULT_RECEIPT_PATH,
    repo_root: Path = REPO_ROOT,
    validator: Callable[..., dict[str, Any]],
    created_at: str | None = None,
) -> dict[str, Any]:
    """Run the formal validator and atomically persist its commit-bound PASS receipt."""

    head = _assert_clean_subject(repo_root, subject_commit)
    result = validator(formal=True)
    counts = result.get("counts", {})
    screenshots_by_split = counts.get("screenshots_by_split", {})
    screenshot_count = sum(screenshots_by_split.values()) if isinstance(screenshots_by_split, dict) else -1
    if (
        result.get("schema_version") != "trip-check-p5-dataset-validation-v2"
        or result.get("status") != "PASS"
        or result.get("formal") is not True
        or result.get("errors") != []
        or counts.get("total") != 360
        or screenshot_count != 171
    ):
        raise P5FormalValidationReceiptError("P5_FORMAL_DATASET_VALIDATOR_FAILED")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if result.get("manifest_hash") != manifest.get("manifest_hash"):
        raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_MANIFEST_DRIFT")
    dataset_files = _file_bindings(manifest, repo_root)
    timestamp = created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload: dict[str, Any] = {
        "schema_version": "trip-check-p5-dataset-validation-v2",
        "status": "PASS",
        "formal": True,
        "subject_commit": head,
        "manifest_hash": manifest["manifest_hash"],
        "dataset_manifest": {
            "path": _repo_path(MANIFEST_PATH, repo_root),
            "file_sha256": file_sha256(MANIFEST_PATH),
            "manifest_hash": manifest["manifest_hash"],
        },
        "dataset_files": dataset_files,
        "validator": {
            "path": _repo_path(VALIDATOR_PATH, repo_root),
            "code_sha256": file_sha256(VALIDATOR_PATH),
        },
        "counts": {
            "total": counts["total"],
            "screenshots": screenshot_count,
            "by_split": counts["by_split"],
            "by_city": counts["by_city"],
            "screenshots_by_split": screenshots_by_split,
        },
        "errors": [],
        "created_at": timestamp,
    }
    payload["receipt_hash"] = digest(payload)
    _validate_receipt(payload)

    output_path = output_path.resolve()
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
            _validate_receipt(existing)
        except (OSError, UnicodeError, json.JSONDecodeError, P5FormalValidationReceiptError) as exc:
            raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_OVERWRITE_DRIFT") from exc
        if _immutable_projection(existing) != _immutable_projection(payload):
            raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_OVERWRITE_DRIFT")
        return existing

    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(output_path, encoded)
    readback = json.loads(output_path.read_text(encoding="utf-8"))
    _validate_receipt(readback)
    if readback != payload:
        raise P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_WRITE_READBACK_DRIFT")
    return payload


__all__ = [
    "DEFAULT_RECEIPT_PATH",
    "P5FormalValidationReceiptError",
    "generate_formal_validation_receipt",
]
