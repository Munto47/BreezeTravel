"""Executable receipt chain for the formal P5 v5 Evaluation Gate.

PASS is derived only from a command that was actually executed and whose
repository/config/artifact state can be read back.  The module never upgrades
NOT_RUN, FAIL, or BLOCKED evidence to PASS.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from evals.trip_check_v1.p5.data_contract import digest


DATASET_ID_V5 = "trip-check-p5-360-v5"
ACTIVE_CONTRACT_V5 = "trip-check-p5-v5"
VERIFICATION_KINDS_V5 = (
    "p1",
    "p2",
    "p3",
    "p4",
    "backend_pytest",
    "ruff",
    "frontend_build",
    "dual_entry",
)
COMMAND_KINDS_V5 = ("dataset_formal", *VERIFICATION_KINDS_V5)
COMMAND_STATUSES_V5 = {"PASS", "FAIL", "BLOCKED"}
RECEIPT_STATUSES_V5 = {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "REJECT"}
PRIMARY_ARTIFACT_NAMES_V5 = (
    "dataset_manifest",
    "active_contract",
    "blind_seal",
    "run_spec",
    "judge_rubric",
    "nonblind_run_manifest",
    "nonblind_score",
    "blind_run_manifest",
    "blind_score",
    "judge_panel",
    "nonblind_terminal_outputs",
    "nonblind_replay_outputs",
    "nonblind_artifact_index",
    "blind_terminal_outputs",
    "blind_replay_outputs",
    "blind_artifact_index",
    "blind_nonce",
    "blind_nonce_mint_receipt",
    "blind_nonce_consumption_receipt",
)


class P5FormalReceiptErrorV5(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class RepoBindingV5:
    subject_commit: str
    upstream_ref: str
    upstream_commit: str
    dirty_tree: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_commit": self.subject_commit,
            "upstream_ref": self.upstream_ref,
            "upstream_commit": self.upstream_commit,
            "dirty_tree": self.dirty_tree,
        }


def _git(repo_root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise P5FormalReceiptErrorV5("RECEIPT_GIT_STATE_UNAVAILABLE") from exc


def read_repo_binding_v5(repo_root: Path) -> RepoBindingV5:
    return RepoBindingV5(
        subject_commit=_git(repo_root, "rev-parse", "HEAD"),
        upstream_ref=_git(
            repo_root,
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ),
        upstream_commit=_git(repo_root, "rev-parse", "@{upstream}"),
        dirty_tree=bool(_git(repo_root, "status", "--short")),
    )


def _require_formal_repo_binding(binding: RepoBindingV5) -> None:
    if (
        re.fullmatch(r"[0-9a-f]{40}", binding.subject_commit) is None
        or binding.upstream_commit != binding.subject_commit
        or not binding.upstream_ref
        or binding.dirty_tree
    ):
        raise P5FormalReceiptErrorV5("RECEIPT_SUBJECT_NOT_CLEAN_PUSHED_UPSTREAM")


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise P5FormalReceiptErrorV5("RECEIPT_ARTIFACT_UNREADABLE") from exc


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5FormalReceiptErrorV5(reason) from exc
    if not isinstance(value, dict):
        raise P5FormalReceiptErrorV5(reason)
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    staged = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _contains_link(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.exists() and (
                current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction())
            ):
                return True
        except OSError:
            return True
    return False


def require_external_output_v5(repo_root: Path, path: Path) -> Path:
    if not path.is_absolute() or _inside(path.absolute(), repo_root) or _contains_link(path.absolute()):
        raise P5FormalReceiptErrorV5("RECEIPT_OUTPUT_MUST_BE_EXTERNAL")
    return path.absolute()


def _artifact(logical_name: str, path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or _contains_link(resolved):
        raise P5FormalReceiptErrorV5("RECEIPT_ARTIFACT_PATH_INVALID")
    return {
        "logical_name": logical_name,
        "path": str(resolved),
        "sha256": _sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _parse_named_paths(values: dict[str, Path]) -> list[dict[str, Any]]:
    if not values or any(not name or not path.is_absolute() for name, path in values.items()):
        raise P5FormalReceiptErrorV5("RECEIPT_NAMED_ARTIFACTS_INVALID")
    return [_artifact(name, path) for name, path in sorted(values.items())]


def _readback_artifacts(records: object) -> bool:
    if not isinstance(records, list) or not records:
        return False
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "logical_name",
            "path",
            "sha256",
            "size_bytes",
        }:
            return False
        try:
            path = Path(record["path"]).resolve(strict=True)
        except (OSError, TypeError):
            return False
        if (
            not path.is_file()
            or _contains_link(path)
            or _sha256(path) != record["sha256"]
            or path.stat().st_size != record["size_bytes"]
        ):
            return False
    return True


def _validate_self_hash(value: dict[str, Any], field: str, reason: str) -> None:
    if value.get(field) != digest({key: item for key, item in value.items() if key != field}):
        raise P5FormalReceiptErrorV5(reason)


def execute_command_receipt_v5(
    *,
    repo_root: Path,
    kind: str,
    command: list[str],
    command_cwd: Path,
    config_artifacts: dict[str, Path],
    expected_artifacts: dict[str, Path],
    output_dir: Path,
    repo_binding: RepoBindingV5 | None = None,
) -> dict[str, Any]:
    """Run one real command and persist its immutable result/log receipt."""

    if kind not in COMMAND_KINDS_V5 or not command:
        raise P5FormalReceiptErrorV5("COMMAND_RECEIPT_ARGUMENT_INVALID")
    root = repo_root.resolve()
    destination = require_external_output_v5(root, output_dir)
    if destination.exists() and any(destination.iterdir()):
        raise P5FormalReceiptErrorV5("COMMAND_RECEIPT_OUTPUT_NOT_EMPTY")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        cwd = command_cwd.resolve(strict=True)
        cwd.relative_to(root)
    except (OSError, ValueError) as exc:
        raise P5FormalReceiptErrorV5("COMMAND_RECEIPT_CWD_INVALID") from exc
    before = repo_binding or read_repo_binding_v5(root)
    _require_formal_repo_binding(before)
    config_records = _parse_named_paths(config_artifacts)
    started_at = datetime.now(timezone.utc).isoformat()
    blocked_reason: str | None = None
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
        )
        exit_code: int | None = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except OSError:
        exit_code = None
        stdout = b""
        stderr = b""
        blocked_reason = "COMMAND_PROCESS_START_FAILED"
    ended_at = datetime.now(timezone.utc).isoformat()
    stdout_path = destination / "stdout.log"
    stderr_path = destination / "stderr.log"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    artifact_errors: list[str] = []
    artifact_paths = {
        "command_stdout": stdout_path,
        "command_stderr": stderr_path,
        **expected_artifacts,
    }
    artifact_records: list[dict[str, Any]] = []
    for name, path in sorted(artifact_paths.items()):
        try:
            artifact_records.append(_artifact(name, path))
        except (OSError, P5FormalReceiptErrorV5):
            artifact_errors.append(f"ARTIFACT_READBACK_FAILED:{name}")
    after = repo_binding or read_repo_binding_v5(root)
    stable_repo = after == before
    stable_config = _readback_artifacts(config_records)
    if blocked_reason is not None:
        status = "BLOCKED"
    elif exit_code != 0 or artifact_errors or not stable_repo or not stable_config:
        status = "FAIL"
    else:
        status = "PASS"
    errors = [*artifact_errors]
    if blocked_reason:
        errors.append(blocked_reason)
    if not stable_repo:
        errors.append("COMMAND_CHANGED_REPOSITORY_STATE")
    if not stable_config:
        errors.append("COMMAND_CHANGED_CONFIG_ARTIFACT")
    receipt: dict[str, Any] = {
        "schema_version": "trip-check-p5-command-result-v5",
        "receipt_kind": kind,
        "status": status,
        **before.as_dict(),
        "command": command,
        "command_cwd": str(cwd),
        "started_at": started_at,
        "ended_at": ended_at,
        "exit_code": exit_code,
        "config_artifacts": config_records,
        "config_hash": digest(config_records),
        "artifact_bindings": artifact_records,
        "artifact_set_hash": digest(artifact_records),
        "readback_verified": not errors,
        "errors": errors,
    }
    receipt["receipt_hash"] = digest(receipt)
    receipt_path = destination / "command_result.v5.json"
    _atomic_json(receipt_path, receipt)
    validate_command_result_v5(receipt_path)
    return {**receipt, "receipt_path": str(receipt_path)}


def validate_command_result_v5(path: Path) -> dict[str, Any]:
    value = _load_json(path, "COMMAND_RESULT_INVALID")
    expected = {
        "schema_version",
        "receipt_kind",
        "status",
        "subject_commit",
        "upstream_ref",
        "upstream_commit",
        "dirty_tree",
        "command",
        "command_cwd",
        "started_at",
        "ended_at",
        "exit_code",
        "config_artifacts",
        "config_hash",
        "artifact_bindings",
        "artifact_set_hash",
        "readback_verified",
        "errors",
        "receipt_hash",
    }
    _validate_self_hash(value, "receipt_hash", "COMMAND_RESULT_HASH_MISMATCH")
    if (
        set(value) != expected
        or value.get("schema_version") != "trip-check-p5-command-result-v5"
        or value.get("receipt_kind") not in COMMAND_KINDS_V5
        or value.get("status") not in COMMAND_STATUSES_V5
        or value.get("upstream_commit") != value.get("subject_commit")
        or value.get("dirty_tree") is not False
        or not isinstance(value.get("command"), list)
        or not value["command"]
        or value.get("config_hash") != digest(value.get("config_artifacts"))
        or value.get("artifact_set_hash") != digest(value.get("artifact_bindings"))
        or not isinstance(value.get("errors"), list)
    ):
        raise P5FormalReceiptErrorV5("COMMAND_RESULT_CONTRACT_INVALID")
    artifacts_ok = _readback_artifacts(value["artifact_bindings"])
    configs_ok = _readback_artifacts(value["config_artifacts"])
    pass_shape = (
        value.get("exit_code") == 0
        and value.get("readback_verified") is True
        and value.get("errors") == []
        and artifacts_ok
        and configs_ok
    )
    if (value["status"] == "PASS") != pass_shape:
        raise P5FormalReceiptErrorV5("COMMAND_RESULT_STATUS_INVALID")
    return value


def build_verification_receipt_v5(*, repo_root: Path, command_result_path: Path, output_path: Path) -> dict[str, Any]:
    require_external_output_v5(repo_root, output_path)
    source = validate_command_result_v5(command_result_path)
    kind = source["receipt_kind"]
    if kind not in VERIFICATION_KINDS_V5:
        raise P5FormalReceiptErrorV5("VERIFICATION_KIND_INVALID")
    readback = _readback_artifacts(source["artifact_bindings"]) and _readback_artifacts(source["config_artifacts"])
    status = source["status"] if readback else "FAIL"
    errors = list(source["errors"])
    if not readback:
        errors.append("VERIFICATION_ARTIFACT_READBACK_FAILED")
    receipt: dict[str, Any] = {
        "schema_version": "trip-check-p5-verification-receipt-v5",
        "receipt_kind": kind,
        "status": status,
        "subject_commit": source["subject_commit"],
        "upstream_ref": source["upstream_ref"],
        "upstream_commit": source["upstream_commit"],
        "dirty_tree": False,
        "config_hash": source["config_hash"],
        "command_result": {
            "path": str(command_result_path.resolve()),
            "sha256": _sha256(command_result_path),
            "receipt_hash": source["receipt_hash"],
        },
        "artifact_bindings": source["artifact_bindings"],
        "artifact_set_hash": source["artifact_set_hash"],
        "readback_verified": readback and status == "PASS",
        "errors": errors,
    }
    if kind == "p4":
        entries = {item["logical_name"]: Path(item["path"]) for item in source["artifact_bindings"]}
        p4_path = entries.get("p4_gate_manifest")
        if p4_path is None:
            raise P5FormalReceiptErrorV5("P4_GATE_MANIFEST_BINDING_MISSING")
        p4 = _load_json(p4_path, "P4_GATE_MANIFEST_INVALID")
        solver = p4.get("solver_admission")
        if not isinstance(solver, dict):
            raise P5FormalReceiptErrorV5("P4_SOLVER_ADMISSION_MISSING")
        receipt["solver_admission"] = {
            "status": solver.get("status"),
            "default_strategy": solver.get("default_strategy"),
        }
        if receipt["solver_admission"] != {
            "status": "REJECT",
            "default_strategy": "bounded_repair_v1",
        }:
            receipt["status"] = "FAIL"
            receipt["readback_verified"] = False
            receipt["errors"].append("P4_SOLVER_ADMISSION_NOT_REJECT")
    receipt["receipt_hash"] = digest(receipt)
    _atomic_json(output_path, receipt)
    return validate_verification_receipt_v5(output_path)


def validate_verification_receipt_v5(path: Path) -> dict[str, Any]:
    value = _load_json(path, "VERIFICATION_RECEIPT_INVALID")
    expected = {
        "schema_version",
        "receipt_kind",
        "status",
        "subject_commit",
        "upstream_ref",
        "upstream_commit",
        "dirty_tree",
        "config_hash",
        "command_result",
        "artifact_bindings",
        "artifact_set_hash",
        "readback_verified",
        "errors",
        "receipt_hash",
    }
    if value.get("receipt_kind") == "p4":
        expected.add("solver_admission")
    _validate_self_hash(value, "receipt_hash", "VERIFICATION_RECEIPT_HASH_MISMATCH")
    command = value.get("command_result")
    if (
        set(value) != expected
        or value.get("schema_version") != "trip-check-p5-verification-receipt-v5"
        or value.get("receipt_kind") not in VERIFICATION_KINDS_V5
        or value.get("status") not in RECEIPT_STATUSES_V5
        or value.get("upstream_commit") != value.get("subject_commit")
        or value.get("dirty_tree") is not False
        or not isinstance(command, dict)
        or set(command) != {"path", "sha256", "receipt_hash"}
        or not isinstance(value.get("errors"), list)
        or value.get("artifact_set_hash") != digest(value.get("artifact_bindings"))
    ):
        raise P5FormalReceiptErrorV5("VERIFICATION_RECEIPT_CONTRACT_INVALID")
    command_path = Path(command["path"])
    source = validate_command_result_v5(command_path)
    readback = (
        _sha256(command_path) == command["sha256"]
        and source["receipt_hash"] == command["receipt_hash"]
        and source["receipt_kind"] == value["receipt_kind"]
        and source["config_hash"] == value["config_hash"]
        and source["artifact_set_hash"] == value["artifact_set_hash"]
        and _readback_artifacts(value["artifact_bindings"])
    )
    if value["status"] == "PASS" and (
        source["status"] != "PASS"
        or value.get("readback_verified") is not True
        or value.get("errors") != []
        or not readback
    ):
        raise P5FormalReceiptErrorV5("VERIFICATION_PASS_NOT_PROVEN")
    if value["receipt_kind"] == "p4" and value.get("solver_admission") != {
        "status": "REJECT",
        "default_strategy": "bounded_repair_v1",
    }:
        raise P5FormalReceiptErrorV5("P4_SOLVER_ADMISSION_INVALID")
    return value


def build_dataset_formal_validation_receipt_v5(
    *,
    repo_root: Path,
    command_result_path: Path,
    dataset_manifest_path: Path,
    validator_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    require_external_output_v5(repo_root, output_path)
    source = validate_command_result_v5(command_result_path)
    if source["receipt_kind"] != "dataset_formal":
        raise P5FormalReceiptErrorV5("DATASET_RECEIPT_COMMAND_KIND_INVALID")
    stdout_records = [item for item in source["artifact_bindings"] if item["logical_name"] == "command_stdout"]
    if len(stdout_records) != 1:
        raise P5FormalReceiptErrorV5("DATASET_VALIDATION_STDOUT_MISSING")
    result = _load_json(Path(stdout_records[0]["path"]), "DATASET_VALIDATION_OUTPUT_INVALID")
    manifest = _load_json(dataset_manifest_path, "DATASET_MANIFEST_INVALID")
    _validate_self_hash(manifest, "manifest_hash", "DATASET_MANIFEST_HASH_MISMATCH")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise P5FormalReceiptErrorV5("DATASET_MANIFEST_FILE_INDEX_INVALID")
    dataset_files: dict[str, dict[str, Any]] = {}
    for name, entry in sorted(files.items()):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise P5FormalReceiptErrorV5("DATASET_MANIFEST_FILE_INDEX_INVALID")
        path = repo_root / "backend" / entry["path"]
        dataset_files[name] = {
            "path": str(path.resolve()),
            "sha256": _sha256(path),
            "content_sha256": entry.get("content_sha256"),
            "row_count": entry.get("row_count"),
        }
        if dataset_files[name]["sha256"] != entry.get("file_sha256"):
            raise P5FormalReceiptErrorV5("DATASET_FILE_HASH_MISMATCH")
    errors: list[str] = []
    if source["status"] != "PASS":
        errors.append("DATASET_VALIDATOR_COMMAND_NOT_PASS")
    if (
        result.get("schema_version") != "trip-check-p5-dataset-validation-v5"
        or result.get("status") != "PASS"
        or result.get("formal") is not True
        or result.get("errors") != []
        or result.get("manifest_hash") != manifest.get("manifest_hash")
    ):
        errors.append("DATASET_FORMAL_VALIDATION_NOT_PASS")
    receipt: dict[str, Any] = {
        "schema_version": "trip-check-p5-dataset-formal-validation-receipt-v5",
        "status": "PASS" if not errors else "REJECT",
        "formal": True,
        "subject_commit": source["subject_commit"],
        "upstream_ref": source["upstream_ref"],
        "upstream_commit": source["upstream_commit"],
        "dirty_tree": False,
        "config_hash": source["config_hash"],
        "command_result": {
            "path": str(command_result_path.resolve()),
            "sha256": _sha256(command_result_path),
            "receipt_hash": source["receipt_hash"],
        },
        "dataset_manifest": {
            "path": str(dataset_manifest_path.resolve()),
            "sha256": _sha256(dataset_manifest_path),
            "manifest_hash": manifest.get("manifest_hash"),
        },
        "validator": {
            "path": str(validator_path.resolve()),
            "sha256": _sha256(validator_path),
        },
        "dataset_files": dataset_files,
        "validation_output_sha256": _sha256(Path(stdout_records[0]["path"])),
        "readback_verified": not errors,
        "errors": errors,
    }
    receipt["receipt_hash"] = digest(receipt)
    _atomic_json(output_path, receipt)
    return validate_dataset_formal_validation_receipt_v5(output_path)


def validate_dataset_formal_validation_receipt_v5(path: Path) -> dict[str, Any]:
    value = _load_json(path, "DATASET_FORMAL_RECEIPT_INVALID")
    _validate_self_hash(value, "receipt_hash", "DATASET_FORMAL_RECEIPT_HASH_MISMATCH")
    expected_fields = {
        "schema_version",
        "status",
        "formal",
        "subject_commit",
        "upstream_ref",
        "upstream_commit",
        "dirty_tree",
        "config_hash",
        "command_result",
        "dataset_manifest",
        "validator",
        "dataset_files",
        "validation_output_sha256",
        "readback_verified",
        "errors",
        "receipt_hash",
    }
    if (
        set(value) != expected_fields
        or value.get("schema_version") != "trip-check-p5-dataset-formal-validation-receipt-v5"
        or value.get("status") not in {"PASS", "REJECT"}
        or value.get("formal") is not True
        or value.get("upstream_commit") != value.get("subject_commit")
        or value.get("dirty_tree") is not False
        or not isinstance(value.get("command_result"), dict)
        or set(value["command_result"]) != {"path", "sha256", "receipt_hash"}
        or not isinstance(value.get("dataset_manifest"), dict)
        or set(value["dataset_manifest"]) != {"path", "sha256", "manifest_hash"}
        or not isinstance(value.get("validator"), dict)
        or set(value["validator"]) != {"path", "sha256"}
        or not isinstance(value.get("dataset_files"), dict)
        or not value["dataset_files"]
        or any(
            not isinstance(item, dict) or set(item) != {"path", "sha256", "content_sha256", "row_count"}
            for item in value["dataset_files"].values()
        )
        or not isinstance(value.get("errors"), list)
    ):
        raise P5FormalReceiptErrorV5("DATASET_FORMAL_RECEIPT_CONTRACT_INVALID")
    command_path = Path(value["command_result"]["path"])
    source = validate_command_result_v5(command_path)
    manifest_path = Path(value["dataset_manifest"]["path"])
    manifest = _load_json(manifest_path, "DATASET_MANIFEST_INVALID")
    _validate_self_hash(manifest, "manifest_hash", "DATASET_MANIFEST_HASH_MISMATCH")
    stdout_records = [item for item in source["artifact_bindings"] if item["logical_name"] == "command_stdout"]
    if len(stdout_records) != 1:
        raise P5FormalReceiptErrorV5("DATASET_VALIDATION_STDOUT_MISSING")
    validation_output_path = Path(stdout_records[0]["path"])
    result = _load_json(validation_output_path, "DATASET_VALIDATION_OUTPUT_INVALID")
    expected_binding = {key: value[key] for key in ("subject_commit", "upstream_ref", "upstream_commit", "dirty_tree")}
    readback = (
        _sha256(command_path) == value["command_result"]["sha256"]
        and source["receipt_hash"] == value["command_result"]["receipt_hash"]
        and source["receipt_kind"] == "dataset_formal"
        and source["config_hash"] == value["config_hash"]
        and all(source[key] == expected_binding[key] for key in expected_binding)
        and _sha256(manifest_path) == value["dataset_manifest"]["sha256"]
        and manifest["manifest_hash"] == value["dataset_manifest"]["manifest_hash"]
        and _sha256(Path(value["validator"]["path"])) == value["validator"]["sha256"]
        and all(_sha256(Path(item["path"])) == item["sha256"] for item in value["dataset_files"].values())
        and _sha256(validation_output_path) == value["validation_output_sha256"]
        and result.get("schema_version") == "trip-check-p5-dataset-validation-v5"
        and result.get("status") == "PASS"
        and result.get("formal") is True
        and result.get("errors") == []
        and result.get("manifest_hash") == manifest["manifest_hash"]
    )
    if value["status"] == "PASS" and (
        source["status"] != "PASS"
        or value.get("readback_verified") is not True
        or value.get("errors") != []
        or not readback
    ):
        raise P5FormalReceiptErrorV5("DATASET_FORMAL_PASS_NOT_PROVEN")
    return value


def build_formal_gate_receipt_v5(
    *,
    repo_root: Path,
    dataset_receipt_path: Path,
    verification_receipts: dict[str, Path],
    primary_artifacts: dict[str, Path],
    output_path: Path,
    repo_binding: RepoBindingV5 | None = None,
) -> dict[str, Any]:
    require_external_output_v5(repo_root, output_path)
    if set(verification_receipts) != set(VERIFICATION_KINDS_V5) or set(primary_artifacts) != set(
        PRIMARY_ARTIFACT_NAMES_V5
    ):
        raise P5FormalReceiptErrorV5("FORMAL_RECEIPT_INPUT_SET_INVALID")
    binding = repo_binding or read_repo_binding_v5(repo_root)
    _require_formal_repo_binding(binding)
    dataset_receipt = validate_dataset_formal_validation_receipt_v5(dataset_receipt_path)
    wrappers = {kind: validate_verification_receipt_v5(path) for kind, path in verification_receipts.items()}
    primary = {name: path.resolve(strict=True) for name, path in primary_artifacts.items()}
    dataset = _load_json(primary["dataset_manifest"], "FORMAL_DATASET_INVALID")
    nonblind_run = _load_json(primary["nonblind_run_manifest"], "FORMAL_NONBLIND_RUN_INVALID")
    blind_run = _load_json(primary["blind_run_manifest"], "FORMAL_BLIND_RUN_INVALID")
    panel = _load_json(primary["judge_panel"], "FORMAL_JUDGE_PANEL_INVALID")
    errors: list[str] = []
    if dataset_receipt["status"] != "PASS":
        errors.append("DATASET_FORMAL_RECEIPT_NOT_PASS")
    for kind, wrapper in wrappers.items():
        if wrapper["status"] != "PASS":
            errors.append(f"VERIFICATION_RECEIPT_NOT_PASS:{kind}")
    expected_binding = binding.as_dict()
    evidence_bindings = [dataset_receipt, *wrappers.values()]
    if any(any(item.get(key) != value for key, value in expected_binding.items()) for item in evidence_bindings):
        errors.append("RECEIPT_SUBJECT_UPSTREAM_MISMATCH")
    if (
        nonblind_run.get("subject_commit") != binding.subject_commit
        or blind_run.get("subject_commit") != binding.subject_commit
        or nonblind_run.get("upstream_ref") != binding.upstream_ref
        or blind_run.get("upstream_ref") != binding.upstream_ref
        or nonblind_run.get("upstream_commit") != binding.upstream_commit
        or blind_run.get("upstream_commit") != binding.upstream_commit
    ):
        errors.append("RUN_SUBJECT_UPSTREAM_MISMATCH")
    counts = {
        "nonblind_cases": nonblind_run.get("case_count"),
        "blind_cases": blind_run.get("case_count"),
        "nonblind_terminals": nonblind_run.get("terminal_count"),
        "blind_terminals": blind_run.get("terminal_count"),
        "replay_readback": nonblind_run.get("replay_readback_count", 0) + blind_run.get("replay_readback_count", 0),
        "judge_rounds": panel.get("round_count"),
        "judge_provenance": len(panel.get("provenance", [])),
    }
    if counts != {
        "nonblind_cases": 270,
        "blind_cases": 90,
        "nonblind_terminals": 810,
        "blind_terminals": 270,
        "replay_readback": 1080,
        "judge_rounds": 3,
        "judge_provenance": 3,
    }:
        errors.append("FORMAL_COUNTS_INVALID")
    receipt_entries = {
        kind: {
            "path": str(verification_receipts[kind].resolve()),
            "sha256": _sha256(verification_receipts[kind]),
            "status": wrapper["status"],
            "subject_commit": wrapper["subject_commit"],
            "upstream_ref": wrapper["upstream_ref"],
            "upstream_commit": wrapper["upstream_commit"],
            "dirty_tree": wrapper["dirty_tree"],
            "config_hash": wrapper["config_hash"],
            "artifact_set_hash": wrapper["artifact_set_hash"],
            "readback_verified": wrapper["readback_verified"],
        }
        for kind, wrapper in wrappers.items()
    }
    receipt: dict[str, Any] = {
        "schema_version": "trip-check-p5-formal-validation-receipt-v5",
        "status": "PASS" if not errors else "REJECT",
        "formal": True,
        **binding.as_dict(),
        "dataset_id": DATASET_ID_V5,
        "dataset_manifest_hash": dataset.get("manifest_hash"),
        "config_hash": digest({kind: wrapper["config_hash"] for kind, wrapper in sorted(wrappers.items())}),
        "dataset_validation_receipt": {
            "path": str(dataset_receipt_path.resolve()),
            "sha256": _sha256(dataset_receipt_path),
            "receipt_hash": dataset_receipt["receipt_hash"],
            "status": dataset_receipt["status"],
        },
        "bindings": {f"{name}_sha256": _sha256(path) for name, path in primary.items()},
        "counts": counts,
        "verification_receipts": receipt_entries,
        "errors": errors,
    }
    receipt["receipt_hash"] = digest(receipt)
    _atomic_json(output_path, receipt)
    validate_formal_gate_receipt_v5(output_path)
    return receipt


def validate_formal_gate_receipt_v5(path: Path) -> dict[str, Any]:
    value = _load_json(path, "FORMAL_GATE_RECEIPT_INVALID")
    _validate_self_hash(value, "receipt_hash", "FORMAL_GATE_RECEIPT_HASH_MISMATCH")
    expected_fields = {
        "schema_version",
        "status",
        "formal",
        "subject_commit",
        "upstream_ref",
        "upstream_commit",
        "dirty_tree",
        "dataset_id",
        "dataset_manifest_hash",
        "config_hash",
        "dataset_validation_receipt",
        "bindings",
        "counts",
        "verification_receipts",
        "errors",
        "receipt_hash",
    }
    dataset_entry = value.get("dataset_validation_receipt")
    bindings = value.get("bindings")
    counts = value.get("counts")
    verification_entries = value.get("verification_receipts")
    if (
        set(value) != expected_fields
        or value.get("schema_version") != "trip-check-p5-formal-validation-receipt-v5"
        or value.get("status") not in {"PASS", "REJECT"}
        or value.get("formal") is not True
        or value.get("upstream_commit") != value.get("subject_commit")
        or value.get("dirty_tree") is not False
        or value.get("dataset_id") != DATASET_ID_V5
        or not isinstance(dataset_entry, dict)
        or set(dataset_entry) != {"path", "sha256", "receipt_hash", "status"}
        or not isinstance(bindings, dict)
        or set(bindings) != {f"{name}_sha256" for name in PRIMARY_ARTIFACT_NAMES_V5}
        or any(re.fullmatch(r"[0-9a-f]{64}", item) is None for item in bindings.values())
        or counts
        != {
            "nonblind_cases": 270,
            "blind_cases": 90,
            "nonblind_terminals": 810,
            "blind_terminals": 270,
            "replay_readback": 1080,
            "judge_rounds": 3,
            "judge_provenance": 3,
        }
        or not isinstance(verification_entries, dict)
        or set(verification_entries) != set(VERIFICATION_KINDS_V5)
        or any(
            not isinstance(entry, dict)
            or set(entry)
            != {
                "path",
                "sha256",
                "status",
                "subject_commit",
                "upstream_ref",
                "upstream_commit",
                "dirty_tree",
                "config_hash",
                "artifact_set_hash",
                "readback_verified",
            }
            for entry in verification_entries.values()
        )
        or not isinstance(value.get("errors"), list)
    ):
        raise P5FormalReceiptErrorV5("FORMAL_GATE_RECEIPT_CONTRACT_INVALID")
    dataset_path = Path(dataset_entry["path"])
    dataset_receipt = validate_dataset_formal_validation_receipt_v5(dataset_path)
    wrappers = {
        kind: validate_verification_receipt_v5(Path(entry["path"])) for kind, entry in verification_entries.items()
    }
    expected_binding = {key: value[key] for key in ("subject_commit", "upstream_ref", "upstream_commit", "dirty_tree")}
    readback = (
        _sha256(dataset_path) == dataset_entry["sha256"]
        and dataset_receipt["receipt_hash"] == dataset_entry["receipt_hash"]
        and dataset_receipt["status"] == dataset_entry["status"]
        and dataset_receipt["dataset_manifest"]["manifest_hash"] == value["dataset_manifest_hash"]
        and all(dataset_receipt[key] == expected_binding[key] for key in expected_binding)
        and value["config_hash"] == digest({kind: wrapper["config_hash"] for kind, wrapper in sorted(wrappers.items())})
        and all(
            _sha256(Path(verification_entries[kind]["path"])) == verification_entries[kind]["sha256"]
            and all(wrapper[key] == expected_binding[key] for key in expected_binding)
            and all(
                wrapper[key] == verification_entries[kind][key]
                for key in (
                    "status",
                    "subject_commit",
                    "upstream_ref",
                    "upstream_commit",
                    "dirty_tree",
                    "config_hash",
                    "artifact_set_hash",
                    "readback_verified",
                )
            )
            for kind, wrapper in wrappers.items()
        )
    )
    if value["status"] == "PASS" and (
        dataset_receipt["status"] != "PASS"
        or any(wrapper["status"] != "PASS" for wrapper in wrappers.values())
        or value["errors"] != []
        or not readback
    ):
        raise P5FormalReceiptErrorV5("FORMAL_GATE_PASS_NOT_PROVEN")
    return value


def mint_blind_nonce_v5(
    *,
    repo_root: Path,
    output_path: Path,
    active_contract_path: Path,
    dataset_manifest_path: Path,
    seal_path: Path,
    nonce_schema_path: Path,
    receipt_output_path: Path | None = None,
    repo_binding: RepoBindingV5 | None = None,
) -> dict[str, Any]:
    destination = require_external_output_v5(repo_root, output_path)
    if destination.exists():
        raise P5FormalReceiptErrorV5("BLIND_NONCE_OVERWRITE_FORBIDDEN")
    binding = repo_binding or read_repo_binding_v5(repo_root)
    _require_formal_repo_binding(binding)
    active = _load_json(active_contract_path, "BLIND_NONCE_ACTIVE_CONTRACT_INVALID")
    dataset = _load_json(dataset_manifest_path, "BLIND_NONCE_DATASET_INVALID")
    if (
        active.get("active_contract") != ACTIVE_CONTRACT_V5
        or active.get("formal_evidence_status") != "READY"
        or dataset.get("dataset_id") != DATASET_ID_V5
        or dataset.get("frozen") is not True
        or dataset.get("formal_validation_eligible") is not True
        or dataset.get("seal_status") != "SEALED"
        or active.get("dataset_manifest_hash") != dataset.get("manifest_hash")
        or active.get("blind_seal_v5_sha256") != _sha256(seal_path)
    ):
        raise P5FormalReceiptErrorV5("BLIND_NONCE_FORMAL_DATASET_NOT_READY")
    nonce = {
        "schema_version": "trip-check-p5-blind-run-nonce-v5",
        "purpose": "execute_frozen_blind_once",
        "dataset_id": DATASET_ID_V5,
        "active_contract": ACTIVE_CONTRACT_V5,
        "nonce": secrets.token_hex(32),
    }
    schema = _load_json(nonce_schema_path, "BLIND_NONCE_SCHEMA_INVALID")
    errors = list(Draft202012Validator(schema).iter_errors(nonce))
    if errors:
        raise P5FormalReceiptErrorV5("BLIND_NONCE_SCHEMA_REJECTED")
    _atomic_json(destination, nonce)
    readback = _load_json(destination, "BLIND_NONCE_READBACK_FAILED")
    if readback != nonce:
        raise P5FormalReceiptErrorV5("BLIND_NONCE_READBACK_FAILED")
    serialized = json.dumps(readback, sort_keys=True).lower()
    if any(token in serialized for token in ("label", "oracle", "answer", "expected")):
        raise P5FormalReceiptErrorV5("BLIND_NONCE_FORBIDDEN_PAYLOAD")
    receipt: dict[str, Any] = {
        "schema_version": "trip-check-p5-blind-run-nonce-mint-receipt-v5",
        "status": "MINTED_NOT_CONSUMED",
        **binding.as_dict(),
        "nonce_file_path": str(destination),
        "nonce_file_sha256": _sha256(destination),
        "nonce_sha256": digest(nonce["nonce"]),
        "label_payload_present": False,
    }
    receipt["receipt_hash"] = digest(receipt)
    if receipt_output_path is not None:
        receipt_destination = require_external_output_v5(repo_root, receipt_output_path)
        if receipt_destination.exists():
            raise P5FormalReceiptErrorV5("BLIND_NONCE_RECEIPT_OVERWRITE_FORBIDDEN")
        _atomic_json(receipt_destination, receipt)
        if _load_json(receipt_destination, "BLIND_NONCE_RECEIPT_READBACK_FAILED") != receipt:
            raise P5FormalReceiptErrorV5("BLIND_NONCE_RECEIPT_READBACK_FAILED")
    return receipt


__all__ = [
    "P5FormalReceiptErrorV5",
    "RepoBindingV5",
    "VERIFICATION_KINDS_V5",
    "build_dataset_formal_validation_receipt_v5",
    "build_formal_gate_receipt_v5",
    "build_verification_receipt_v5",
    "execute_command_receipt_v5",
    "mint_blind_nonce_v5",
    "validate_command_result_v5",
    "validate_dataset_formal_validation_receipt_v5",
    "validate_formal_gate_receipt_v5",
    "validate_verification_receipt_v5",
]
