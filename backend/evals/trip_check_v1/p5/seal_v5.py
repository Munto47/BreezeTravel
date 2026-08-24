"""Atomic P5 v5 seal activation over independently reviewed external custody."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p5.blind_external_contract_v5 import (
    validate_external_custody_v5,
)
from evals.trip_check_v1.p5.contracts_v3 import VARIANT_IDS_V3
from evals.trip_check_v1.p5.data_contract import digest, file_sha256, load_jsonl
from evals.trip_check_v1.p5.data_contract_v2 import JUDGE_RUBRIC_PATH_V2
from evals.trip_check_v1.p5.data_contract_v3 import (
    CONTRACTS_PATH_V3,
    case_set_hash_v3,
    materialization_set_hash_v3,
)
from evals.trip_check_v1.p5.data_contract_v5 import (
    BLIND_INPUT_PATH_V5,
    BLIND_MATERIALIZATIONS_PATH_V5,
    BLIND_SEAL_PATH_V5,
    DATASET_CONTRACTS_PATH_V5,
    MANIFEST_PATH_V5,
    NONBLIND_MATERIALIZATIONS_PATH_V5,
    NONBLIND_PATH_V5,
    RUN_SPEC_TEMPLATE_PATH_V5,
    SOURCE_ACTIVE_CONTRACT_V4_PATH,
    build_pending_manifest_v5,
    validate_manifest_v5,
    validate_v4_source_anchor,
)
from evals.trip_check_v1.p5.dataset_contracts_v5 import (
    P5BlindSealV5,
    P5SealingCommitmentV5,
)


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class P5V5SealError(RuntimeError):
    """Stable fail-closed error that never includes custody paths or labels."""


@dataclass(frozen=True)
class SealPathsV5:
    repo_root: Path
    nonblind_cases_path: Path
    blind_inputs_path: Path
    nonblind_materializations_path: Path
    blind_materializations_path: Path
    contracts_v3_path: Path
    dataset_contracts_v5_path: Path
    run_spec_template_path: Path
    rubric_path: Path
    source_v4_active_path: Path
    source_v4_seal_path: Path
    manifest_path: Path
    seal_path: Path
    active_contract_path: Path

    @classmethod
    def for_repo(cls, repo_root: Path) -> "SealPathsV5":
        root = repo_root.resolve()
        return cls(
            repo_root=root,
            nonblind_cases_path=NONBLIND_PATH_V5,
            blind_inputs_path=BLIND_INPUT_PATH_V5,
            nonblind_materializations_path=NONBLIND_MATERIALIZATIONS_PATH_V5,
            blind_materializations_path=BLIND_MATERIALIZATIONS_PATH_V5,
            contracts_v3_path=CONTRACTS_PATH_V3,
            dataset_contracts_v5_path=DATASET_CONTRACTS_PATH_V5,
            run_spec_template_path=RUN_SPEC_TEMPLATE_PATH_V5,
            rubric_path=JUDGE_RUBRIC_PATH_V2,
            source_v4_active_path=SOURCE_ACTIVE_CONTRACT_V4_PATH,
            source_v4_seal_path=BLIND_SEAL_PATH_V5.parent / "frozen_blind.v4.seal.json",
            manifest_path=MANIFEST_PATH_V5,
            seal_path=BLIND_SEAL_PATH_V5,
            active_contract_path=MANIFEST_PATH_V5.parent / "active_contract.json",
        )


def _require_sha256(value: object, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise P5V5SealError(reason)
    return value


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5V5SealError(reason) from exc
    if not isinstance(value, dict):
        raise P5V5SealError(reason)
    return value


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_output(repo_root: Path, arguments: Sequence[str]) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


def _git_blob(repo_root: Path, commit: str, repository_path: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "show", f"{commit}:{repository_path}"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise P5V5SealError("P5_V5_CANDIDATE_BLOB_MISSING") from exc
    return completed.stdout


def _preflight_git(
    *,
    paths: SealPathsV5,
    candidate_freeze_commit: str,
    git_output: Callable[[Path, Sequence[str]], str],
) -> None:
    if _COMMIT_RE.fullmatch(candidate_freeze_commit) is None:
        raise P5V5SealError("P5_V5_CANDIDATE_COMMIT_INVALID")
    try:
        head = git_output(paths.repo_root, ("rev-parse", "HEAD"))
        dirty = git_output(paths.repo_root, ("status", "--porcelain"))
        upstream = git_output(
            paths.repo_root,
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
        )
        upstream_head = git_output(paths.repo_root, ("rev-parse", upstream))
    except (OSError, subprocess.SubprocessError) as exc:
        raise P5V5SealError("P5_V5_GIT_PREFLIGHT_FAILED") from exc
    if head != candidate_freeze_commit:
        raise P5V5SealError("P5_V5_CANDIDATE_HEAD_NOT_EXACT")
    if dirty:
        raise P5V5SealError("P5_V5_DIRTY_TREE_FORBIDDEN")
    if upstream_head != head:
        raise P5V5SealError("P5_V5_UPSTREAM_NOT_SYNCHRONIZED")


def _validate_candidate_manifest(
    *,
    paths: SealPathsV5,
    candidate_freeze_commit: str,
    candidate_manifest_hash: str,
) -> dict[str, Any]:
    expected_hash = _require_sha256(candidate_manifest_hash, "P5_V5_CANDIDATE_MANIFEST_HASH_INVALID")
    try:
        manifest = validate_manifest_v5(paths.repo_root, manifest_path=paths.manifest_path, require_sealed=False)
    except Exception as exc:
        raise P5V5SealError("P5_V5_CANDIDATE_MANIFEST_NOT_PENDING") from exc
    if manifest.get("manifest_hash") != expected_hash:
        raise P5V5SealError("P5_V5_CANDIDATE_MANIFEST_HASH_MISMATCH")
    relative = paths.manifest_path.relative_to(paths.repo_root).as_posix()
    try:
        committed = json.loads(_git_blob(paths.repo_root, candidate_freeze_commit, relative).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise P5V5SealError("P5_V5_CANDIDATE_MANIFEST_BLOB_MISMATCH") from exc
    if committed != manifest:
        raise P5V5SealError("P5_V5_CANDIDATE_MANIFEST_BLOB_MISMATCH")
    for name, path in {
        "nonblind_cases": paths.nonblind_cases_path,
        "blind_cases": paths.blind_inputs_path,
        "nonblind_materializations": paths.nonblind_materializations_path,
        "blind_materializations": paths.blind_materializations_path,
    }.items():
        entry = manifest["files"][name]
        repository_path = path.relative_to(paths.repo_root).as_posix()
        if (
            entry.get("file_sha256") != file_sha256(path)
            or entry.get("content_sha256") != digest(load_jsonl(path))
            or _sha256_bytes(_git_blob(paths.repo_root, candidate_freeze_commit, repository_path))
            != entry.get("file_sha256")
        ):
            raise P5V5SealError(f"P5_V5_CANDIDATE_FILE_BINDING_MISMATCH:{name}")
    return manifest


def build_blind_seal_v5(
    *,
    paths: SealPathsV5,
    candidate_freeze_commit: str,
    candidate_manifest_hash: str,
    custody_commitments: Mapping[str, str],
) -> dict[str, Any]:
    blind_cases = load_jsonl(paths.blind_inputs_path)
    blind_materializations = load_jsonl(paths.blind_materializations_path)
    case_ids = [row.get("case_id") for row in blind_cases]
    if (
        len(blind_cases) != 90
        or len(blind_materializations) != 90
        or any(not isinstance(item, str) for item in case_ids)
        or len(set(case_ids)) != 90
        or {row.get("case_id") for row in blind_materializations} != set(case_ids)
    ):
        raise P5V5SealError("P5_V5_BLIND_CASE_SET_INVALID")
    source = validate_v4_source_anchor(paths.repo_root)
    seal = P5BlindSealV5(
        candidate_freeze_commit=candidate_freeze_commit,
        candidate_dataset_manifest_hash=candidate_manifest_hash,
        case_ids_sha256=digest(sorted(case_ids)),
        nonblind_cases_file_sha256=file_sha256(paths.nonblind_cases_path),
        nonblind_materializations_file_sha256=file_sha256(paths.nonblind_materializations_path),
        inputs_file_sha256=file_sha256(paths.blind_inputs_path),
        inputs_content_sha256=digest(blind_cases),
        materializations_file_sha256=file_sha256(paths.blind_materializations_path),
        materializations_content_sha256=digest(blind_materializations),
        case_set_hash=case_set_hash_v3(blind_cases),
        materialization_set_hash=materialization_set_hash_v3(blind_materializations),
        contracts_v3_sha256=file_sha256(paths.contracts_v3_path),
        dataset_contracts_v5_sha256=file_sha256(paths.dataset_contracts_v5_path),
        run_spec_template_sha256=file_sha256(paths.run_spec_template_path),
        rubric_sha256=file_sha256(paths.rubric_path),
        variant_ids_sha256=digest(list(VARIANT_IDS_V3)),
        labels_canonical_sha256=_require_sha256(
            custody_commitments.get("labels_canonical_sha256"),
            "P5_V5_LABEL_COMMITMENT_INVALID",
        ),
        external_bundle_sha256=_require_sha256(
            custody_commitments.get("external_bundle_sha256"),
            "P5_V5_BUNDLE_COMMITMENT_INVALID",
        ),
        correction_receipt_sha256=_require_sha256(
            custody_commitments.get("correction_receipt_sha256"),
            "P5_V5_CORRECTION_RECEIPT_INVALID",
        ),
        review_receipt_sha256=_require_sha256(
            custody_commitments.get("review_receipt_sha256"),
            "P5_V5_REVIEW_RECEIPT_INVALID",
        ),
        policy_mapping_sha256=_require_sha256(
            custody_commitments.get("policy_mapping_sha256"),
            "P5_V5_POLICY_MAPPING_INVALID",
        ),
        source_v4_blind_seal_file_sha256=file_sha256(paths.source_v4_seal_path),
        source_v4_inputs_file_sha256=file_sha256(paths.blind_inputs_path.parent / "frozen_blind.v4.inputs.jsonl"),
        source_v4_materializations_file_sha256=file_sha256(
            paths.blind_inputs_path.parent / "frozen_blind.v4.materializations.jsonl"
        ),
        source_v4_dataset_manifest_hash=source["dataset_manifest_hash"],
        source_v4_labels_canonical_sha256=source["labels_canonical_sha256"],
        source_v4_external_bundle_sha256=source["external_bundle_sha256"],
        source_v4_review_receipt_sha256=source["review_receipt_sha256"],
    )
    return seal.model_dump(mode="json")


def build_active_contract_v5(
    *,
    paths: SealPathsV5,
    candidate_freeze_commit: str,
    sealed_dataset_manifest_hash: str,
    blind_seal_file_sha256: str,
) -> dict[str, Any]:
    source = _load_json(paths.source_v4_active_path, "P5_V5_SOURCE_ACTIVE_CONTRACT_INVALID")
    deprecated = deepcopy(source.get("deprecated_contracts", []))
    deprecated.append(
        {
            "contract_id": "trip-check-p5-v4",
            "formal_evidence_eligible": False,
            "reason": "INVALID_EVIDENCE_SUPERSEDED_BY_SEALED_P5_V5",
        }
    )
    return {
        "schema_version": "trip-check-p5-active-contract-v1",
        "active_contract": "trip-check-p5-v5",
        "formal_evidence_status": "READY",
        "candidate_freeze_commit": candidate_freeze_commit,
        "dataset_manifest_hash": sealed_dataset_manifest_hash,
        "blind_seal_v5_sha256": blind_seal_file_sha256,
        "deprecated_contracts": deprecated,
        "source_v4_contract": {
            **source,
            "path": "evals/trip_check_v1/p5/source_active_contract_v4.json",
            "active_contract_sha256": digest(source),
            "active_contract_file_sha256": file_sha256(paths.source_v4_active_path),
        },
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def seal_and_freeze_v5(
    *,
    paths: SealPathsV5,
    external_bundle_path: Path,
    external_correction_receipt_path: Path,
    external_review_receipt_path: Path,
    expected_bundle_sha256: str,
    expected_labels_canonical_sha256: str,
    expected_correction_receipt_sha256: str,
    expected_review_receipt_sha256: str,
    candidate_freeze_commit: str,
    candidate_manifest_hash: str,
    nonformal_validator: Callable[[], Mapping[str, Any]],
    formal_validator: Callable[[], Mapping[str, Any]],
    git_output: Callable[[Path, Sequence[str]], str] = _git_output,
    custody_validator: Callable[..., Mapping[str, str]] = validate_external_custody_v5,
) -> dict[str, Any]:
    """Seal and activate only an exact clean, pushed, independently reviewed v5."""

    _preflight_git(
        paths=paths,
        candidate_freeze_commit=candidate_freeze_commit,
        git_output=git_output,
    )
    _validate_candidate_manifest(
        paths=paths,
        candidate_freeze_commit=candidate_freeze_commit,
        candidate_manifest_hash=candidate_manifest_hash,
    )
    if paths.seal_path.exists():
        raise P5V5SealError("P5_V5_BLIND_SEAL_OVERWRITE_FORBIDDEN")
    pending_manifest_bytes = paths.manifest_path.read_bytes()
    active_contract_bytes = paths.active_contract_path.read_bytes()
    validation = nonformal_validator()
    if validation.get("status") != "PASS" or validation.get("formal") is not False:
        raise P5V5SealError("P5_V5_NONFORMAL_DATASET_VALIDATION_FAILED")
    source = validate_v4_source_anchor(paths.repo_root)
    try:
        commitments = custody_validator(
            repo_root=paths.repo_root,
            external_bundle_path=external_bundle_path,
            external_correction_receipt_path=external_correction_receipt_path,
            external_review_receipt_path=external_review_receipt_path,
            expected_bundle_sha256=expected_bundle_sha256,
            expected_labels_canonical_sha256=expected_labels_canonical_sha256,
            expected_correction_receipt_sha256=expected_correction_receipt_sha256,
            expected_review_receipt_sha256=expected_review_receipt_sha256,
            candidate_subject_commit=candidate_freeze_commit,
            source_bundle_sha256=source["external_bundle_sha256"],
            source_labels_canonical_sha256=source["labels_canonical_sha256"],
        )
    except Exception as exc:
        raise P5V5SealError("P5_V5_EXTERNAL_CUSTODY_INVALID") from exc
    seal = build_blind_seal_v5(
        paths=paths,
        candidate_freeze_commit=candidate_freeze_commit,
        candidate_manifest_hash=candidate_manifest_hash,
        custody_commitments=commitments,
    )
    seal_bytes = _json_bytes(seal)
    commitment = P5SealingCommitmentV5(
        candidate_freeze_commit=candidate_freeze_commit,
        candidate_dataset_manifest_hash=candidate_manifest_hash,
        blind_seal_path=("backend/evals/trip_check_v1/p5/sealed/frozen_blind.v5.seal.json"),
        blind_seal_file_sha256=_sha256_bytes(seal_bytes),
        labels_canonical_sha256=commitments["labels_canonical_sha256"],
        external_bundle_sha256=commitments["external_bundle_sha256"],
        correction_receipt_sha256=commitments["correction_receipt_sha256"],
        review_receipt_sha256=commitments["review_receipt_sha256"],
        policy_mapping_sha256=commitments["policy_mapping_sha256"],
    ).model_dump(mode="json")
    _atomic_write(paths.seal_path, seal_bytes)
    try:
        sealed_manifest = build_pending_manifest_v5(paths.repo_root)
        sealed_manifest.update(
            {
                "formal_validation_eligible": True,
                "seal_status": "SEALED",
                "sealing_commitment": commitment,
            }
        )
        sealed_manifest["manifest_hash"] = digest(
            {key: value for key, value in sealed_manifest.items() if key != "manifest_hash"}
        )
        _atomic_write(paths.manifest_path, _json_bytes(sealed_manifest))
        active = build_active_contract_v5(
            paths=paths,
            candidate_freeze_commit=candidate_freeze_commit,
            sealed_dataset_manifest_hash=sealed_manifest["manifest_hash"],
            blind_seal_file_sha256=file_sha256(paths.seal_path),
        )
        _atomic_write(paths.active_contract_path, _json_bytes(active))
        readback = formal_validator()
        if readback.get("status") != "PASS" or readback.get("formal") is not True:
            raise P5V5SealError("P5_V5_FORMAL_DATASET_VALIDATION_FAILED")
    except Exception:
        _atomic_write(paths.manifest_path, pending_manifest_bytes)
        _atomic_write(paths.active_contract_path, active_contract_bytes)
        paths.seal_path.unlink(missing_ok=True)
        raise
    return {
        "status": "SEALED",
        "active_contract": "trip-check-p5-v5",
        "candidate_freeze_commit": candidate_freeze_commit,
        "candidate_dataset_manifest_hash": candidate_manifest_hash,
        "blind_seal_file_sha256": file_sha256(paths.seal_path),
        "sealed_dataset_manifest_hash": sealed_manifest["manifest_hash"],
        "case_count": 90,
        "changed_label_count": 60,
        "non_target_oracle_diff_count": 0,
        "blind_payload_changed": False,
        "blind_labels_read_by_custodian": True,
        "blind_label_details_emitted": False,
        "human_evidence": False,
    }


__all__ = [
    "P5V5SealError",
    "SealPathsV5",
    "build_active_contract_v5",
    "build_blind_seal_v5",
    "seal_and_freeze_v5",
]
