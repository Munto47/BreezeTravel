"""Fail-closed construction and activation of the P5 v2 blind seal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from evals.trip_check_v1.p5.contracts_v2 import VARIANT_IDS_V2
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.final_blind_scorer_v2 import SCHEMA_CONTRACT_PATHS_V2


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SEAL_SCHEMA_VERSION = "trip-check-p5-blind-seal-v2"
COMMITMENT_SCHEMA_VERSION = "trip-check-p5-sealing-commitment-v2"
ACTIVE_SCHEMA_VERSION = "trip-check-p5-active-contract-v1"
V2_CONTRACT_ID = "trip-check-p5-v2"
V1_CONTRACT_ID = "trip-check-p5-v1"

SEAL_FIELDS = frozenset(
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

COMMITMENT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "candidate_freeze_commit",
        "candidate_dataset_manifest_hash",
        "blind_seal_path",
        "blind_seal_v2_sha256",
        "labels_canonical_sha256",
        "external_bundle_sha256",
        "review_receipt_sha256",
    }
)


class P5V2SealError(RuntimeError):
    """A stable fail-closed reason emitted by the v2-only sealing path."""


@dataclass(frozen=True)
class SealPathsV2:
    repo_root: Path
    inputs_path: Path
    materializations_path: Path
    run_spec_template_path: Path
    rubric_path: Path
    seal_schema_path: Path
    seal_path: Path
    dataset_manifest_path: Path
    active_contract_path: Path

    @classmethod
    def for_repo(cls, repo_root: Path) -> "SealPathsV2":
        root = repo_root.resolve()
        p5 = root / "backend" / "evals" / "trip_check_v1" / "p5"
        return cls(
            repo_root=root,
            inputs_path=p5 / "frozen_blind.v2.inputs.jsonl",
            materializations_path=p5 / "frozen_blind.v2.materializations.jsonl",
            run_spec_template_path=p5 / "run_spec_template_v2.json",
            rubric_path=p5 / "judge_rubric_v2.json",
            seal_schema_path=p5 / "blind_seal_v2.schema.json",
            seal_path=p5 / "sealed" / "frozen_blind.v2.seal.json",
            dataset_manifest_path=p5 / "dataset_v2.manifest.json",
            active_contract_path=p5 / "active_contract.json",
        )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise P5V2SealError(f"P5_V2_SEAL_INPUT_UNREADABLE:{path.name}") from exc


def _require_sha256(value: str, field: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise P5V2SealError(f"P5_V2_SEAL_HASH_INVALID:{field}")
    return value


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5V2SealError(reason) from exc
    if not isinstance(payload, dict):
        raise P5V2SealError(reason)
    return payload


def _load_jsonl(path: Path, reason: str) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5V2SealError(reason) from exc
    if any(not isinstance(row, dict) for row in rows):
        raise P5V2SealError(reason)
    return rows


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _git_output(repo_root: Path, arguments: Sequence[str]) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _read_git_state(
    *,
    repo_root: Path,
    candidate_freeze_commit: str,
    git_output: Callable[[Path, Sequence[str]], str],
) -> tuple[str, str | None, str]:
    if not COMMIT_PATTERN.fullmatch(candidate_freeze_commit):
        raise P5V2SealError("P5_V2_CANDIDATE_COMMIT_INVALID")
    try:
        head = git_output(repo_root, ("rev-parse", "HEAD"))
        dirty = git_output(repo_root, ("status", "--porcelain"))
    except (OSError, subprocess.SubprocessError) as exc:
        raise P5V2SealError("P5_V2_GIT_PREFLIGHT_FAILED") from exc
    parent: str | None = None
    if head != candidate_freeze_commit:
        try:
            parent = git_output(repo_root, ("rev-parse", "HEAD^"))
        except (OSError, subprocess.SubprocessError) as exc:
            raise P5V2SealError("P5_V2_MIXED_OR_WRONG_CANDIDATE_COMMIT") from exc
        if parent != candidate_freeze_commit:
            raise P5V2SealError("P5_V2_MIXED_OR_WRONG_CANDIDATE_COMMIT")
    return head, parent, dirty


def _dirty_paths(status: str) -> set[str]:
    paths: set[str] = set()
    for line in status.splitlines():
        if len(line) < 4:
            raise P5V2SealError("P5_V2_DIRTY_TREE_FORBIDDEN")
        value = line[3:]
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[1]
        paths.add(value.strip('"').replace("\\", "/"))
    return paths


def _allowed_activation_paths(paths: SealPathsV2) -> set[str]:
    values: set[str] = set()
    for path in (paths.seal_path, paths.dataset_manifest_path, paths.active_contract_path):
        try:
            values.add(path.resolve().relative_to(paths.repo_root.resolve()).as_posix())
        except ValueError as exc:
            raise P5V2SealError("P5_V2_SEAL_PATH_OUTSIDE_REPOSITORY") from exc
    return values


def _validate_v1_superseded(active: Mapping[str, Any]) -> None:
    deprecated = active.get("deprecated_contracts")
    if not isinstance(deprecated, list) or not any(
        isinstance(item, dict)
        and item.get("contract_id") == V1_CONTRACT_ID
        and item.get("formal_evidence_eligible") is False
        for item in deprecated
    ):
        raise P5V2SealError("P5_V1_SUPERSESSION_RECEIPT_MISSING")


def _validate_pending_active(active: Mapping[str, Any]) -> None:
    if active.get("schema_version") != ACTIVE_SCHEMA_VERSION or active.get("active_contract") != V2_CONTRACT_ID:
        raise P5V2SealError("P5_V2_ACTIVE_CONTRACT_INVALID")
    _validate_v1_superseded(active)
    if active.get("formal_evidence_status") not in {"PENDING_V2_SEAL", "READY"}:
        raise P5V2SealError("P5_V2_ACTIVE_CONTRACT_INVALID")


def _manifest_file_binding(
    manifest: Mapping[str, Any],
    *,
    key: str,
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    entry = manifest.get("files", {}).get(key, {})
    if entry.get("file_sha256") != _sha256_file(path) or entry.get("content_sha256") != digest(rows):
        raise P5V2SealError(f"P5_V2_DATASET_MANIFEST_BINDING_MISMATCH:{key}")


def _validate_candidate_manifest(
    manifest: Mapping[str, Any],
    *,
    paths: SealPathsV2,
    inputs: list[dict[str, Any]],
    materializations: list[dict[str, Any]],
) -> tuple[str, Mapping[str, Any] | None]:
    commitment = manifest.get("sealing_commitment")
    base = dict(manifest)
    base.pop("manifest_hash", None)
    base.pop("sealing_commitment", None)
    candidate_hash = digest(base)
    if commitment is None:
        if manifest.get("manifest_hash") != candidate_hash:
            raise P5V2SealError("P5_V2_DATASET_MANIFEST_HASH_MISMATCH")
    elif not isinstance(commitment, dict):
        raise P5V2SealError("P5_V2_SEALING_COMMITMENT_INVALID")
    _manifest_file_binding(manifest, key="blind_cases", path=paths.inputs_path, rows=inputs)
    _manifest_file_binding(
        manifest,
        key="blind_materializations",
        path=paths.materializations_path,
        rows=materializations,
    )
    if manifest.get("generation", {}).get("ocr_mode") != "actual":
        raise P5V2SealError("P5_V2_FORMAL_OCR_NOT_ACTUAL")
    return candidate_hash, commitment


def _schema_contract_hash(paths: SealPathsV2) -> str:
    # Production uses the exact scorer allowlist. Tests may replace that tuple
    # with a smaller temporary contract set without weakening production.
    bindings = [
        {"path": relative, "file_sha256": _sha256_file(paths.repo_root / relative)}
        for relative in sorted(SCHEMA_CONTRACT_PATHS_V2)
    ]
    return digest(bindings)


def build_blind_seal_v2(
    *,
    paths: SealPathsV2,
    labels_canonical_sha256: str,
    external_bundle_sha256: str,
    review_receipt_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    labels_hash = _require_sha256(labels_canonical_sha256, "labels_canonical_sha256")
    bundle_hash = _require_sha256(external_bundle_sha256, "external_bundle_sha256")
    review_hash = _require_sha256(review_receipt_sha256, "review_receipt_sha256")
    inputs = _load_jsonl(paths.inputs_path, "P5_V2_BLIND_INPUTS_INVALID")
    materializations = _load_jsonl(paths.materializations_path, "P5_V2_BLIND_MATERIALIZATIONS_INVALID")
    case_ids = [row.get("case_id") for row in inputs]
    if len(inputs) != 90 or len(materializations) != 90 or any(not isinstance(item, str) for item in case_ids):
        raise P5V2SealError("P5_V2_BLIND_CASE_SET_INVALID")
    if len(set(case_ids)) != 90 or {row.get("case_id") for row in materializations} != set(case_ids):
        raise P5V2SealError("P5_V2_BLIND_CASE_SET_INVALID")
    seal = {
        "schema_version": SEAL_SCHEMA_VERSION,
        "split": "frozen_blind",
        "case_count": 90,
        "case_ids_sha256": digest(sorted(case_ids)),
        "inputs_file_sha256": _sha256_file(paths.inputs_path),
        "inputs_content_sha256": digest(inputs),
        "materializations_file_sha256": _sha256_file(paths.materializations_path),
        "materializations_content_sha256": digest(materializations),
        "schema_contract_sha256": _schema_contract_hash(paths),
        "labels_canonical_sha256": labels_hash,
        "external_bundle_sha256": bundle_hash,
        "rubric_sha256": _sha256_file(paths.rubric_path),
        "run_spec_template_sha256": _sha256_file(paths.run_spec_template_path),
        "variant_ids_sha256": digest(list(VARIANT_IDS_V2)),
        "review_receipt_sha256": review_hash,
        "label_storage": "external_bundle_only",
        "label_access": "isolated_scorer_only",
        "scoring_payload_present": False,
        "human_evidence": False,
    }
    schema = _load_json(paths.seal_schema_path, "P5_V2_BLIND_SEAL_SCHEMA_INVALID")
    schema_errors = sorted(Draft202012Validator(schema).iter_errors(seal), key=lambda item: list(item.path))
    if schema_errors or set(seal) != SEAL_FIELDS:
        raise P5V2SealError("P5_V2_BLIND_SEAL_SCHEMA_REJECTED")
    return seal, inputs, materializations


def _build_commitment(
    *,
    paths: SealPathsV2,
    seal: Mapping[str, Any],
    seal_sha256: str,
    candidate_freeze_commit: str,
    candidate_manifest_hash: str,
) -> dict[str, Any]:
    try:
        relative_seal = paths.seal_path.resolve().relative_to(paths.repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise P5V2SealError("P5_V2_SEAL_PATH_OUTSIDE_REPOSITORY") from exc
    return {
        "schema_version": COMMITMENT_SCHEMA_VERSION,
        "status": "SEALED",
        "candidate_freeze_commit": candidate_freeze_commit,
        "candidate_dataset_manifest_hash": candidate_manifest_hash,
        "blind_seal_path": relative_seal,
        "blind_seal_v2_sha256": seal_sha256,
        "labels_canonical_sha256": seal["labels_canonical_sha256"],
        "external_bundle_sha256": seal["external_bundle_sha256"],
        "review_receipt_sha256": seal["review_receipt_sha256"],
    }


def _build_active(
    *,
    current: Mapping[str, Any],
    candidate_freeze_commit: str,
    seal_sha256: str,
    manifest_hash: str,
) -> dict[str, Any]:
    return {
        "schema_version": ACTIVE_SCHEMA_VERSION,
        "active_contract": V2_CONTRACT_ID,
        "formal_evidence_status": "READY",
        "candidate_freeze_commit": candidate_freeze_commit,
        "blind_seal_v2_sha256": seal_sha256,
        "dataset_manifest_hash": manifest_hash,
        "deprecated_contracts": current["deprecated_contracts"],
    }


def _stage_atomic(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    staged = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    return staged


def _atomic_write(path: Path, payload: bytes) -> None:
    staged = _stage_atomic(path, payload)
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _existing_seal_must_match(path: Path, expected_bytes: bytes) -> None:
    if path.exists() and path.read_bytes() != expected_bytes:
        raise P5V2SealError("P5_V2_BLIND_SEAL_DRIFT_OVERWRITE_FORBIDDEN")


def _ready_outputs_match(
    *,
    current_active: Mapping[str, Any],
    expected_active: Mapping[str, Any],
    current_manifest: Mapping[str, Any],
    expected_manifest: Mapping[str, Any],
    seal_path: Path,
    seal_bytes: bytes,
) -> bool:
    return (
        current_active == expected_active
        and current_manifest == expected_manifest
        and seal_path.is_file()
        and seal_path.read_bytes() == seal_bytes
    )


def seal_and_activate_v2(
    *,
    paths: SealPathsV2,
    labels_canonical_sha256: str,
    external_bundle_sha256: str,
    review_receipt_sha256: str,
    candidate_freeze_commit: str,
    dataset_validator: Callable[[], Mapping[str, Any]],
    enforce_git: bool = True,
    git_output: Callable[[Path, Sequence[str]], str] = _git_output,
    atomic_write: Callable[[Path, bytes], None] = _atomic_write,
) -> dict[str, Any]:
    """Create the v2 seal, commit it into the dataset manifest, and activate v2.

    The candidate commit is the clean parent that contains the frozen dataset.
    A later commit contains these three metadata writes; no self-referential
    formal subject commit is embedded here.
    """

    if not COMMIT_PATTERN.fullmatch(candidate_freeze_commit):
        raise P5V2SealError("P5_V2_CANDIDATE_COMMIT_INVALID")
    git_state: tuple[str, str | None, str] | None = None
    if enforce_git:
        git_state = _read_git_state(
            repo_root=paths.repo_root,
            candidate_freeze_commit=candidate_freeze_commit,
            git_output=git_output,
        )

    validation = dataset_validator()
    if validation.get("status") != "PASS" or validation.get("formal") is not True:
        raise P5V2SealError("P5_V2_FORMAL_DATASET_VALIDATION_FAILED")

    seal, inputs, materializations = build_blind_seal_v2(
        paths=paths,
        labels_canonical_sha256=labels_canonical_sha256,
        external_bundle_sha256=external_bundle_sha256,
        review_receipt_sha256=review_receipt_sha256,
    )
    manifest = _load_json(paths.dataset_manifest_path, "P5_V2_DATASET_MANIFEST_INVALID")
    active = _load_json(paths.active_contract_path, "P5_V2_ACTIVE_CONTRACT_INVALID")
    _validate_pending_active(active)
    candidate_manifest_hash, existing_commitment = _validate_candidate_manifest(
        manifest,
        paths=paths,
        inputs=inputs,
        materializations=materializations,
    )

    seal_bytes = _json_bytes(seal)
    seal_sha256 = _sha256_bytes(seal_bytes)
    commitment = _build_commitment(
        paths=paths,
        seal=seal,
        seal_sha256=seal_sha256,
        candidate_freeze_commit=candidate_freeze_commit,
        candidate_manifest_hash=candidate_manifest_hash,
    )
    if set(commitment) != COMMITMENT_FIELDS:
        raise AssertionError("internal P5 v2 commitment field drift")
    if existing_commitment is not None and existing_commitment != commitment:
        raise P5V2SealError("P5_V2_SEALING_COMMITMENT_DRIFT")

    sealed_manifest = dict(manifest)
    sealed_manifest.pop("manifest_hash", None)
    sealed_manifest["sealing_commitment"] = commitment
    sealed_manifest["manifest_hash"] = digest(sealed_manifest)
    ready_active = _build_active(
        current=active,
        candidate_freeze_commit=candidate_freeze_commit,
        seal_sha256=seal_sha256,
        manifest_hash=sealed_manifest["manifest_hash"],
    )

    _existing_seal_must_match(paths.seal_path, seal_bytes)
    if active.get("formal_evidence_status") == "READY":
        if not _ready_outputs_match(
            current_active=active,
            expected_active=ready_active,
            current_manifest=manifest,
            expected_manifest=sealed_manifest,
            seal_path=paths.seal_path,
            seal_bytes=seal_bytes,
        ):
            raise P5V2SealError("P5_V2_ACTIVATION_READBACK_DRIFT")
        if git_state is not None:
            head, parent, dirty = git_state
            allowed_dirty = _allowed_activation_paths(paths)
            if dirty and not _dirty_paths(dirty).issubset(allowed_dirty):
                raise P5V2SealError("P5_V2_DIRTY_TREE_FORBIDDEN")
            if parent == candidate_freeze_commit and dirty:
                raise P5V2SealError("P5_V2_DIRTY_TREE_FORBIDDEN")
            if head != candidate_freeze_commit and parent != candidate_freeze_commit:
                raise P5V2SealError("P5_V2_MIXED_OR_WRONG_CANDIDATE_COMMIT")
        return {
            "status": "READY",
            "idempotent": True,
            "blind_seal_v2_sha256": seal_sha256,
            "dataset_manifest_hash": sealed_manifest["manifest_hash"],
            "candidate_freeze_commit": candidate_freeze_commit,
        }

    if git_state is not None:
        head, _parent, dirty = git_state
        if head != candidate_freeze_commit:
            raise P5V2SealError("P5_V2_MIXED_OR_WRONG_CANDIDATE_COMMIT")
        if dirty:
            raise P5V2SealError("P5_V2_DIRTY_TREE_FORBIDDEN")

    atomic_write(paths.seal_path, seal_bytes)
    atomic_write(paths.dataset_manifest_path, _json_bytes(sealed_manifest))
    atomic_write(paths.active_contract_path, _json_bytes(ready_active))
    validate_activation_readback_v2(paths=paths)
    return {
        "status": "READY",
        "idempotent": False,
        "blind_seal_v2_sha256": seal_sha256,
        "dataset_manifest_hash": sealed_manifest["manifest_hash"],
        "candidate_freeze_commit": candidate_freeze_commit,
    }


def validate_activation_readback_v2(*, paths: SealPathsV2) -> dict[str, Any]:
    seal = _load_json(paths.seal_path, "P5_V2_BLIND_SEAL_READBACK_FAILED")
    manifest = _load_json(paths.dataset_manifest_path, "P5_V2_DATASET_MANIFEST_READBACK_FAILED")
    active = _load_json(paths.active_contract_path, "P5_V2_ACTIVE_CONTRACT_READBACK_FAILED")
    _validate_pending_active(active)
    if active.get("formal_evidence_status") != "READY":
        raise P5V2SealError("P5_V2_ACTIVE_CONTRACT_NOT_READY")
    if set(seal) != SEAL_FIELDS:
        raise P5V2SealError("P5_V2_BLIND_SEAL_READBACK_FAILED")
    expected_seal, _inputs, _materializations = build_blind_seal_v2(
        paths=paths,
        labels_canonical_sha256=str(seal.get("labels_canonical_sha256", "")),
        external_bundle_sha256=str(seal.get("external_bundle_sha256", "")),
        review_receipt_sha256=str(seal.get("review_receipt_sha256", "")),
    )
    if seal != expected_seal:
        raise P5V2SealError("P5_V2_BLIND_SEAL_READBACK_DRIFT")
    commitment = manifest.get("sealing_commitment")
    if not isinstance(commitment, dict) or set(commitment) != COMMITMENT_FIELDS:
        raise P5V2SealError("P5_V2_SEALING_COMMITMENT_INVALID")
    if manifest.get("manifest_hash") != digest({key: value for key, value in manifest.items() if key != "manifest_hash"}):
        raise P5V2SealError("P5_V2_DATASET_MANIFEST_HASH_MISMATCH")
    seal_sha256 = _sha256_file(paths.seal_path)
    expected = {
        "blind_seal_v2_sha256": seal_sha256,
        "labels_canonical_sha256": seal["labels_canonical_sha256"],
        "external_bundle_sha256": seal["external_bundle_sha256"],
        "review_receipt_sha256": seal["review_receipt_sha256"],
    }
    if any(commitment.get(key) != value for key, value in expected.items()):
        raise P5V2SealError("P5_V2_SEALING_COMMITMENT_READBACK_MISMATCH")
    if active.get("blind_seal_v2_sha256") != seal_sha256:
        raise P5V2SealError("P5_V2_ACTIVE_SEAL_READBACK_MISMATCH")
    if active.get("dataset_manifest_hash") != manifest["manifest_hash"]:
        raise P5V2SealError("P5_V2_ACTIVE_MANIFEST_READBACK_MISMATCH")
    if active.get("candidate_freeze_commit") != commitment["candidate_freeze_commit"]:
        raise P5V2SealError("P5_V2_ACTIVE_CANDIDATE_READBACK_MISMATCH")
    _validate_v1_superseded(active)
    return {
        "status": "PASS",
        "blind_seal_v2_sha256": seal_sha256,
        "dataset_manifest_hash": manifest["manifest_hash"],
        "candidate_freeze_commit": active["candidate_freeze_commit"],
    }


__all__ = [
    "P5V2SealError",
    "SealPathsV2",
    "build_blind_seal_v2",
    "seal_and_activate_v2",
    "validate_activation_readback_v2",
]
