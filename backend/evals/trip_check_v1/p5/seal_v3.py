"""Fail-closed, isolated custody activation for the P5 v3 blind dataset.

The v3 seal re-envelopes the exact external v2 truth commitments.  It never
returns label payloads and it never rewrites the frozen v2 dataset, rubric,
seal, or external custody artifacts.
"""

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

from evals.trip_check_v1.p5.blind_external_contract_v2 import (
    expected_blind_dataset_binding_v2,
    validate_external_blind_bundle_v2,
    validate_external_blind_review_receipt_v2,
)
from evals.trip_check_v1.p5.contracts_v3 import (
    P5BlindSealV3,
    P5SealingCommitmentV3,
    VARIANT_IDS_V3,
)
from evals.trip_check_v1.p5.data_contract import digest, file_sha256, load_jsonl
from evals.trip_check_v1.p5.data_contract_v2 import BLIND_SEAL_PATH_V2
from evals.trip_check_v1.p5.data_contract_v3 import (
    BLIND_INPUT_PATH_V3,
    BLIND_MATERIALIZATIONS_PATH_V3,
    BLIND_SEAL_PATH_V3,
    CONTRACTS_PATH_V3,
    JUDGE_RUBRIC_PATH_V2,
    MANIFEST_PATH_V3,
    NONBLIND_MATERIALIZATIONS_PATH_V3,
    NONBLIND_PATH_V3,
    RUN_SPEC_TEMPLATE_PATH_V3,
    build_manifest_v3,
    case_set_hash_v3,
    materialization_set_hash_v3,
    validate_v2_source_anchor,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class P5V3SealError(RuntimeError):
    """Stable fail-closed reason emitted without custody paths or label data."""


@dataclass(frozen=True)
class SealPathsV3:
    repo_root: Path
    nonblind_cases_path: Path
    blind_inputs_path: Path
    nonblind_materializations_path: Path
    blind_materializations_path: Path
    contracts_path: Path
    run_spec_template_path: Path
    rubric_path: Path
    source_v2_seal_path: Path
    manifest_path: Path
    seal_path: Path

    @classmethod
    def for_repo(cls, repo_root: Path) -> "SealPathsV3":
        root = repo_root.resolve()
        return cls(
            repo_root=root,
            nonblind_cases_path=NONBLIND_PATH_V3,
            blind_inputs_path=BLIND_INPUT_PATH_V3,
            nonblind_materializations_path=NONBLIND_MATERIALIZATIONS_PATH_V3,
            blind_materializations_path=BLIND_MATERIALIZATIONS_PATH_V3,
            contracts_path=CONTRACTS_PATH_V3,
            run_spec_template_path=RUN_SPEC_TEMPLATE_PATH_V3,
            rubric_path=JUDGE_RUBRIC_PATH_V2,
            source_v2_seal_path=BLIND_SEAL_PATH_V2,
            manifest_path=MANIFEST_PATH_V3,
            seal_path=BLIND_SEAL_PATH_V3,
        )


def _require_sha256(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise P5V3SealError(reason)
    return value


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5V3SealError(reason) from exc
    if not isinstance(value, dict):
        raise P5V3SealError(reason)
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _canonical_text_sha256(path: Path) -> str:
    try:
        payload = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    except OSError as exc:
        raise P5V3SealError("P5_V3_CONTRACT_UNREADABLE") from exc
    return _sha256_bytes(payload)


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
        raise P5V3SealError("P5_V3_CANDIDATE_BLOB_MISSING") from exc
    return completed.stdout


def _preflight_git(
    *,
    paths: SealPathsV3,
    candidate_freeze_commit: str,
    git_output: Callable[[Path, Sequence[str]], str],
) -> None:
    if not _COMMIT_RE.fullmatch(candidate_freeze_commit):
        raise P5V3SealError("P5_V3_CANDIDATE_COMMIT_INVALID")
    try:
        head = git_output(paths.repo_root, ("rev-parse", "HEAD"))
        dirty = git_output(paths.repo_root, ("status", "--porcelain"))
        upstream_ref = git_output(
            paths.repo_root,
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
        )
        upstream_head = git_output(paths.repo_root, ("rev-parse", upstream_ref))
    except (OSError, subprocess.SubprocessError) as exc:
        raise P5V3SealError("P5_V3_GIT_PREFLIGHT_FAILED") from exc
    if head != candidate_freeze_commit:
        raise P5V3SealError("P5_V3_CANDIDATE_HEAD_NOT_EXACT")
    if dirty:
        raise P5V3SealError("P5_V3_DIRTY_TREE_FORBIDDEN")
    if upstream_head != head:
        raise P5V3SealError("P5_V3_UPSTREAM_NOT_SYNCHRONIZED")


def _candidate_manifest(
    *,
    paths: SealPathsV3,
    candidate_freeze_commit: str,
    candidate_manifest_hash: str,
) -> dict[str, Any]:
    expected_manifest_hash = _require_sha256(
        candidate_manifest_hash, "P5_V3_CANDIDATE_MANIFEST_HASH_INVALID"
    )
    manifest = _load_json(paths.manifest_path, "P5_V3_CANDIDATE_MANIFEST_INVALID")
    if (
        manifest.get("schema_version") != "trip-check-p5-dataset-manifest-v3"
        or manifest.get("manifest_hash") != expected_manifest_hash
        or manifest.get("manifest_hash")
        != digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
        or manifest.get("seal_status") != "PENDING_V3_SEAL"
        or manifest.get("frozen") is not False
        or manifest.get("formal_validation_eligible") is not False
        or "sealing_commitment" in manifest
    ):
        raise P5V3SealError("P5_V3_CANDIDATE_MANIFEST_NOT_PENDING")
    relative_manifest = paths.manifest_path.relative_to(paths.repo_root).as_posix()
    try:
        candidate_manifest = json.loads(
            _git_blob(paths.repo_root, candidate_freeze_commit, relative_manifest).decode("utf-8")
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise P5V3SealError("P5_V3_CANDIDATE_MANIFEST_BLOB_MISMATCH") from exc
    if candidate_manifest != manifest:
        raise P5V3SealError("P5_V3_CANDIDATE_MANIFEST_BLOB_MISMATCH")

    expected_paths = {
        "nonblind_cases": paths.nonblind_cases_path,
        "blind_cases": paths.blind_inputs_path,
        "nonblind_materializations": paths.nonblind_materializations_path,
        "blind_materializations": paths.blind_materializations_path,
    }
    files = manifest.get("files")
    if not isinstance(files, Mapping) or set(files) != set(expected_paths):
        raise P5V3SealError("P5_V3_CANDIDATE_FILE_INDEX_INVALID")
    for key, path in expected_paths.items():
        entry = files.get(key)
        rows = load_jsonl(path)
        if (
            not isinstance(entry, Mapping)
            or entry.get("path") != path.relative_to(paths.repo_root / "backend").as_posix()
            or entry.get("row_count") != len(rows)
            or entry.get("file_sha256") != file_sha256(path)
            or entry.get("content_sha256") != digest(rows)
        ):
            raise P5V3SealError(f"P5_V3_CANDIDATE_FILE_BINDING_MISMATCH:{key}")
        relative = path.relative_to(paths.repo_root).as_posix()
        if _sha256_bytes(_git_blob(paths.repo_root, candidate_freeze_commit, relative)) != entry["file_sha256"]:
            raise P5V3SealError(f"P5_V3_CANDIDATE_FILE_BLOB_MISMATCH:{key}")

    contracts = manifest.get("contract_hashes")
    expected_contracts = (
        (paths.contracts_path, "contracts_v3_sha256", True),
        (paths.run_spec_template_path, "run_spec_template_sha256", False),
        (paths.rubric_path, "judge_rubric_sha256", False),
    )
    if not isinstance(contracts, Mapping):
        raise P5V3SealError("P5_V3_CANDIDATE_CONTRACT_INDEX_INVALID")
    for path, key, canonical_text in expected_contracts:
        current_hash = _canonical_text_sha256(path) if canonical_text else file_sha256(path)
        relative = path.relative_to(paths.repo_root).as_posix()
        blob_hash = _sha256_bytes(_git_blob(paths.repo_root, candidate_freeze_commit, relative))
        if contracts.get(key) != current_hash or blob_hash != current_hash:
            raise P5V3SealError(f"P5_V3_CANDIDATE_CONTRACT_BLOB_MISMATCH:{key}")
    return manifest


def _validate_external_truth_v2(
    *,
    paths: SealPathsV3,
    external_bundle_path: Path,
    external_review_receipt_path: Path,
) -> dict[str, str]:
    source_seal = _load_json(paths.source_v2_seal_path, "P5_V3_SOURCE_V2_SEAL_INVALID")
    source_anchor = validate_v2_source_anchor()
    expected_binding, expected_case_ids = expected_blind_dataset_binding_v2(paths.repo_root)
    bundle = validate_external_blind_bundle_v2(
        repo_root=paths.repo_root,
        bundle_path=external_bundle_path,
        expected_bundle_sha256=_require_sha256(
            source_seal.get("external_bundle_sha256"), "P5_V3_SOURCE_BUNDLE_HASH_INVALID"
        ),
        expected_labels_canonical_sha256=_require_sha256(
            source_seal.get("labels_canonical_sha256"), "P5_V3_SOURCE_LABEL_HASH_INVALID"
        ),
        expected_dataset_binding=expected_binding,
        expected_case_ids=expected_case_ids,
    )
    review = validate_external_blind_review_receipt_v2(
        repo_root=paths.repo_root,
        receipt_path=external_review_receipt_path,
        expected_receipt_sha256=_require_sha256(
            source_seal.get("review_receipt_sha256"), "P5_V3_SOURCE_REVIEW_HASH_INVALID"
        ),
        expected_candidate_subject_commit=str(source_anchor["candidate_freeze_commit"]),
        expected_bundle_sha256=bundle["bundle_byte_sha256"],
        expected_bundle_canonical_sha256=bundle["bundle_canonical_sha256"],
        expected_labels_canonical_sha256=bundle["labels_canonical_sha256"],
        expected_dataset_binding=expected_binding,
    )
    blind_v3 = load_jsonl(paths.blind_inputs_path)
    case_ids_v3 = tuple(sorted(str(row.get("case_id", "")) for row in blind_v3))
    if case_ids_v3 != expected_case_ids:
        raise P5V3SealError("P5_V3_TRUTH_REBIND_CASE_SET_MISMATCH")
    if (
        bundle["labels_canonical_sha256"] != source_seal.get("labels_canonical_sha256")
        or review["labels_canonical_sha256"] != source_seal.get("labels_canonical_sha256")
        or review["bundle_byte_sha256"] != source_seal.get("external_bundle_sha256")
        or review["review_receipt_sha256"] != source_seal.get("review_receipt_sha256")
    ):
        raise P5V3SealError("P5_V3_EXTERNAL_TRUTH_COMMITMENT_MISMATCH")
    return {
        "labels_canonical_sha256": str(source_seal["labels_canonical_sha256"]),
        "external_bundle_sha256": str(source_seal["external_bundle_sha256"]),
        "review_receipt_sha256": str(source_seal["review_receipt_sha256"]),
        "source_v2_blind_seal_file_sha256": file_sha256(paths.source_v2_seal_path),
    }


def build_blind_seal_v3(
    *,
    paths: SealPathsV3,
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
        raise P5V3SealError("P5_V3_BLIND_CASE_SET_INVALID")
    seal = P5BlindSealV3(
        candidate_freeze_commit=candidate_freeze_commit,
        candidate_dataset_manifest_hash=candidate_manifest_hash,
        case_ids_sha256=digest(sorted(case_ids)),
        nonblind_cases_file_sha256=file_sha256(paths.nonblind_cases_path),
        nonblind_materializations_file_sha256=file_sha256(
            paths.nonblind_materializations_path
        ),
        inputs_file_sha256=file_sha256(paths.blind_inputs_path),
        inputs_content_sha256=digest(blind_cases),
        materializations_file_sha256=file_sha256(paths.blind_materializations_path),
        materializations_content_sha256=digest(blind_materializations),
        case_set_hash=case_set_hash_v3(blind_cases),
        materialization_set_hash=materialization_set_hash_v3(blind_materializations),
        contracts_v3_sha256=_canonical_text_sha256(paths.contracts_path),
        run_spec_template_sha256=file_sha256(paths.run_spec_template_path),
        rubric_sha256=file_sha256(paths.rubric_path),
        variant_ids_sha256=digest(list(VARIANT_IDS_V3)),
        labels_canonical_sha256=_require_sha256(
            custody_commitments.get("labels_canonical_sha256"),
            "P5_V3_LABEL_COMMITMENT_INVALID",
        ),
        external_bundle_sha256=_require_sha256(
            custody_commitments.get("external_bundle_sha256"),
            "P5_V3_BUNDLE_COMMITMENT_INVALID",
        ),
        review_receipt_sha256=_require_sha256(
            custody_commitments.get("review_receipt_sha256"),
            "P5_V3_REVIEW_COMMITMENT_INVALID",
        ),
        source_v2_blind_seal_file_sha256=_require_sha256(
            custody_commitments.get("source_v2_blind_seal_file_sha256"),
            "P5_V3_SOURCE_SEAL_COMMITMENT_INVALID",
        ),
    )
    return seal.model_dump(mode="json")


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


def seal_and_freeze_v3(
    *,
    paths: SealPathsV3,
    external_bundle_path: Path,
    external_review_receipt_path: Path,
    candidate_freeze_commit: str,
    candidate_manifest_hash: str,
    nonformal_validator: Callable[[], Mapping[str, Any]],
    formal_validator: Callable[[], Mapping[str, Any]],
    git_output: Callable[[Path, Sequence[str]], str] = _git_output,
) -> dict[str, Any]:
    """Seal a clean, pushed candidate and freeze its v3 manifest atomically."""

    _preflight_git(
        paths=paths,
        candidate_freeze_commit=candidate_freeze_commit,
        git_output=git_output,
    )
    _candidate_manifest(
        paths=paths,
        candidate_freeze_commit=candidate_freeze_commit,
        candidate_manifest_hash=candidate_manifest_hash,
    )
    pending_manifest_bytes = paths.manifest_path.read_bytes()
    validation = nonformal_validator()
    if validation.get("status") != "PASS" or validation.get("formal") is not False:
        raise P5V3SealError("P5_V3_NONFORMAL_DATASET_VALIDATION_FAILED")
    commitments = _validate_external_truth_v2(
        paths=paths,
        external_bundle_path=external_bundle_path,
        external_review_receipt_path=external_review_receipt_path,
    )
    seal = build_blind_seal_v3(
        paths=paths,
        candidate_freeze_commit=candidate_freeze_commit,
        candidate_manifest_hash=candidate_manifest_hash,
        custody_commitments=commitments,
    )
    seal_bytes = _json_bytes(seal)
    if paths.seal_path.exists():
        raise P5V3SealError("P5_V3_BLIND_SEAL_OVERWRITE_FORBIDDEN")
    commitment = P5SealingCommitmentV3(
        candidate_freeze_commit=candidate_freeze_commit,
        candidate_dataset_manifest_hash=candidate_manifest_hash,
        blind_seal_path=paths.seal_path.relative_to(paths.repo_root).as_posix(),
        blind_seal_file_sha256=_sha256_bytes(seal_bytes),
        labels_canonical_sha256=commitments["labels_canonical_sha256"],
        external_bundle_sha256=commitments["external_bundle_sha256"],
        review_receipt_sha256=commitments["review_receipt_sha256"],
    ).model_dump(mode="json")

    _atomic_write(paths.seal_path, seal_bytes)
    try:
        sealed_manifest = build_manifest_v3(
            nonblind_cases=load_jsonl(paths.nonblind_cases_path),
            blind_cases=load_jsonl(paths.blind_inputs_path),
            nonblind_materializations=load_jsonl(paths.nonblind_materializations_path),
            blind_materializations=load_jsonl(paths.blind_materializations_path),
            sealing_commitment=commitment,
        )
        _atomic_write(paths.manifest_path, _json_bytes(sealed_manifest))
        readback = formal_validator()
        if readback.get("status") != "PASS" or readback.get("formal") is not True:
            raise P5V3SealError("P5_V3_FORMAL_DATASET_VALIDATION_FAILED")
    except Exception:
        _atomic_write(paths.manifest_path, pending_manifest_bytes)
        paths.seal_path.unlink(missing_ok=True)
        raise
    return {
        "status": "SEALED",
        "candidate_freeze_commit": candidate_freeze_commit,
        "candidate_dataset_manifest_hash": candidate_manifest_hash,
        "blind_seal_file_sha256": file_sha256(paths.seal_path),
        "sealed_dataset_manifest_hash": sealed_manifest["manifest_hash"],
        "sealed_dataset_manifest_file_sha256": file_sha256(paths.manifest_path),
        "case_count": 90,
        "blind_labels_read_by_custodian": True,
        "blind_label_details_emitted": False,
        "human_evidence": False,
    }


__all__ = [
    "P5V3SealError",
    "SealPathsV3",
    "build_blind_seal_v3",
    "seal_and_freeze_v3",
]
