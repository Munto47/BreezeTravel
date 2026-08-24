"""P5 v5 dataset envelope over byte-identical v4 product payloads.

Only the external blind oracle commitment may change in v5.  Repository-side
cases, materializations, product adapters, rubric, and scoring semantics remain
the v4 bytes/behavior and are verified fail closed before custody or execution.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3
from evals.trip_check_v1.p5.data_contract import (
    P5_ROOT,
    digest,
    file_sha256,
    load_jsonl,
)
from evals.trip_check_v1.p5.data_contract_v3 import (
    case_set_hash_v3,
    materialization_set_hash_v3,
)
from evals.trip_check_v1.p5.data_contract_v4 import validate_materialization_v4


DATASET_ID_V5 = "trip-check-p5-360-v5"
MANIFEST_SCHEMA_VERSION_V5 = "trip-check-p5-dataset-manifest-v5"
NONBLIND_PATH_V5 = P5_ROOT / "cases_nonblind_v5.jsonl"
BLIND_INPUT_PATH_V5 = P5_ROOT / "frozen_blind.v5.inputs.jsonl"
NONBLIND_MATERIALIZATIONS_PATH_V5 = P5_ROOT / "materializations_nonblind_v5.jsonl"
BLIND_MATERIALIZATIONS_PATH_V5 = P5_ROOT / "frozen_blind.v5.materializations.jsonl"
MANIFEST_PATH_V5 = P5_ROOT / "dataset_v5.manifest.json"
BLIND_SEAL_PATH_V5 = P5_ROOT / "sealed" / "frozen_blind.v5.seal.json"
DATASET_CONTRACTS_PATH_V5 = P5_ROOT / "dataset_contracts_v5.py"
RUN_SPEC_TEMPLATE_PATH_V5 = P5_ROOT / "run_spec_template_v5.json"
SOURCE_ACTIVE_CONTRACT_V4_PATH = P5_ROOT / "source_active_contract_v4.json"

_V4_PAYLOAD_PAIRS = (
    ("cases_nonblind_v4.jsonl", "cases_nonblind_v5.jsonl"),
    ("materializations_nonblind_v4.jsonl", "materializations_nonblind_v5.jsonl"),
    ("frozen_blind.v4.inputs.jsonl", "frozen_blind.v5.inputs.jsonl"),
    ("frozen_blind.v4.materializations.jsonl", "frozen_blind.v5.materializations.jsonl"),
)
_RUN_SPEC_ENVELOPE_KEYS = {
    "evidence_policy_version",
    "output_schema_version",
    "replay_hash_policy",
    "schema_version",
}


class P5DataContractErrorV5(ValueError):
    """Stable fail-closed v5 dataset contract error."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5DataContractErrorV5(f"unreadable JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise P5DataContractErrorV5(f"invalid JSON object: {path.name}")
    return value


def _p5_root_for(repo_root: Path) -> Path:
    root = repo_root.resolve()
    p5 = (root / "backend" / "evals" / "trip_check_v1" / "p5").resolve()
    try:
        p5.relative_to(root)
    except ValueError as exc:
        raise P5DataContractErrorV5("P5 root escapes repository") from exc
    if p5 != P5_ROOT.resolve():
        raise P5DataContractErrorV5("repository root does not own active P5 module")
    return p5


def validate_materialization_v5(
    case: P5CaseV3 | Mapping[str, Any], materialization: Mapping[str, Any]
) -> dict[str, Any]:
    """Reuse the frozen v4/v3 materialization semantics without modification."""

    validated = case if isinstance(case, P5CaseV3) else P5CaseV3.model_validate(case)
    return validate_materialization_v4(validated, materialization)


def validate_v4_source_anchor(repo_root: Path) -> dict[str, Any]:
    p5 = _p5_root_for(repo_root)
    source = _load_json(p5 / "source_active_contract_v4.json")
    manifest = _load_json(p5 / "dataset_v4.manifest.json")
    seal_path = p5 / "sealed" / "frozen_blind.v4.seal.json"
    seal = _load_json(seal_path)
    commitment = manifest.get("sealing_commitment")
    if (
        source.get("schema_version") != "trip-check-p5-active-contract-v1"
        or source.get("active_contract") != "trip-check-p5-v4"
        or source.get("formal_evidence_status") != "READY"
        or source.get("dataset_manifest_hash") != manifest.get("manifest_hash")
        or source.get("blind_seal_v4_sha256") != file_sha256(seal_path)
        or manifest.get("schema_version") != "trip-check-p5-dataset-manifest-v4"
        or manifest.get("dataset_id") != "trip-check-p5-360-v4"
        or manifest.get("manifest_hash")
        != digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
        or not isinstance(commitment, Mapping)
        or commitment.get("blind_seal_file_sha256") != file_sha256(seal_path)
        or seal.get("candidate_freeze_commit") != source.get("candidate_freeze_commit")
    ):
        raise P5DataContractErrorV5("v4 source contract is not immutable and self-consistent")
    return {
        "active_contract": "trip-check-p5-v4",
        "active_contract_file_sha256": file_sha256(p5 / "source_active_contract_v4.json"),
        "active_contract_sha256": digest(source),
        "candidate_freeze_commit": source["candidate_freeze_commit"],
        "dataset_manifest_file_sha256": file_sha256(p5 / "dataset_v4.manifest.json"),
        "dataset_manifest_hash": manifest["manifest_hash"],
        "blind_seal_file_sha256": file_sha256(seal_path),
        "external_bundle_sha256": seal["external_bundle_sha256"],
        "labels_canonical_sha256": seal["labels_canonical_sha256"],
        "review_receipt_sha256": seal["review_receipt_sha256"],
    }


def validate_v4_v5_byte_identity(repo_root: Path) -> dict[str, str]:
    p5 = _p5_root_for(repo_root)
    result: dict[str, str] = {}
    for source_name, target_name in _V4_PAYLOAD_PAIRS:
        source = p5 / source_name
        target = p5 / target_name
        if source.read_bytes() != target.read_bytes():
            raise P5DataContractErrorV5(f"v5 payload differs from v4: {target_name}")
        result[target_name] = file_sha256(target)

    v4_spec = _load_json(p5 / "run_spec_template_v4.json")
    v5_spec = _load_json(p5 / "run_spec_template_v5.json")
    changed = {key for key in set(v4_spec) | set(v5_spec) if v4_spec.get(key) != v5_spec.get(key)}
    if changed != _RUN_SPEC_ENVELOPE_KEYS:
        raise P5DataContractErrorV5("v5 RunSpec changed outside envelope version fields")
    if (
        v5_spec.get("schema_version") != "trip-check-p5-run-spec-v5"
        or v5_spec.get("replay_hash_policy") != "p5-semantic-projection-v5"
        or v5_spec.get("output_schema_version") != "trip-check-p5-case-result-v5"
    ):
        raise P5DataContractErrorV5("v5 RunSpec envelope is invalid")
    return result


def _file_entry(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(P5_ROOT.parent.parent.parent)).replace("\\", "/"),
        "row_count": len(rows),
        "file_sha256": file_sha256(path),
        "content_sha256": digest(rows),
    }


def build_pending_manifest_v5(repo_root: Path) -> dict[str, Any]:
    """Build the tracked, label-free manifest used for candidate custody."""

    p5 = _p5_root_for(repo_root)
    validate_v4_v5_byte_identity(repo_root)
    source_anchor = validate_v4_source_anchor(repo_root)
    v4 = _load_json(p5 / "dataset_v4.manifest.json")
    nonblind = load_jsonl(NONBLIND_PATH_V5)
    blind = load_jsonl(BLIND_INPUT_PATH_V5)
    nonblind_mats = load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V5)
    blind_mats = load_jsonl(BLIND_MATERIALIZATIONS_PATH_V5)
    manifest = copy.deepcopy(v4)
    manifest.update(
        {
            "schema_version": MANIFEST_SCHEMA_VERSION_V5,
            "dataset_id": DATASET_ID_V5,
            "formal_validation_eligible": False,
            "seal_status": "PENDING_V5_SEAL",
            "source_v4_anchor": source_anchor,
        }
    )
    manifest.pop("source_v3_anchor", None)
    manifest.pop("sealing_commitment", None)
    manifest["files"] = {
        "nonblind_cases": _file_entry(NONBLIND_PATH_V5, nonblind),
        "blind_cases": _file_entry(BLIND_INPUT_PATH_V5, blind),
        "nonblind_materializations": _file_entry(
            NONBLIND_MATERIALIZATIONS_PATH_V5, nonblind_mats
        ),
        "blind_materializations": _file_entry(BLIND_MATERIALIZATIONS_PATH_V5, blind_mats),
    }
    manifest["lanes"]["nonblind"].update(
        {
            "case_set_hash": case_set_hash_v3(nonblind),
            "materialization_set_hash": materialization_set_hash_v3(nonblind_mats),
            "bytes_identical_to_v4": True,
        }
    )
    manifest["lanes"]["frozen_blind"].update(
        {
            "case_set_hash": case_set_hash_v3(blind),
            "materialization_set_hash": materialization_set_hash_v3(blind_mats),
            "bytes_identical_to_v4": True,
        }
    )
    manifest["generation"] = {
        "mode": "V4_ORACLE_POLICY_SUPERSESSION",
        "blind_bytes_copied_from_v4": True,
        "nonblind_bytes_copied_from_v4": True,
        "blind_labels_read": False,
        "ocr_executed": False,
    }
    manifest["contract_hashes"].update(
        {
            "data_contract_v5_path": "evals/trip_check_v1/p5/data_contract_v5.py",
            "data_contract_v5_sha256": file_sha256(p5 / "data_contract_v5.py"),
            "dataset_contracts_v5_path": "evals/trip_check_v1/p5/dataset_contracts_v5.py",
            "dataset_contracts_v5_sha256": file_sha256(DATASET_CONTRACTS_PATH_V5),
            "run_spec_template_path": "evals/trip_check_v1/p5/run_spec_template_v5.json",
            "run_spec_template_sha256": file_sha256(RUN_SPEC_TEMPLATE_PATH_V5),
            "judge_rubric_semantics_changed": False,
        }
    )
    manifest["contract_hashes"].pop("dataset_contracts_v4_path", None)
    manifest["contract_hashes"].pop("dataset_contracts_v4_sha256", None)
    manifest["manifest_hash"] = digest(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    )
    return manifest


def validate_manifest_v5(
    repo_root: Path, *, manifest_path: Path = MANIFEST_PATH_V5, require_sealed: bool
) -> dict[str, Any]:
    validate_v4_v5_byte_identity(repo_root)
    source_anchor = validate_v4_source_anchor(repo_root)
    manifest = _load_json(manifest_path)
    if (
        manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION_V5
        or manifest.get("dataset_id") != DATASET_ID_V5
        or manifest.get("frozen") is not True
        or manifest.get("manifest_hash")
        != digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
        or manifest.get("source_v4_anchor") != source_anchor
    ):
        raise P5DataContractErrorV5("v5 manifest envelope is invalid")
    expected = build_pending_manifest_v5(repo_root)
    for key in (
        "counts",
        "files",
        "lanes",
        "contract_hashes",
        "generation",
        "route_evidence_repairs",
        "source_v4_anchor",
    ):
        if manifest.get(key) != expected.get(key):
            raise P5DataContractErrorV5(f"v5 manifest binding mismatch: {key}")
    if require_sealed:
        if (
            manifest.get("formal_validation_eligible") is not True
            or manifest.get("seal_status") != "SEALED"
            or not isinstance(manifest.get("sealing_commitment"), Mapping)
        ):
            raise P5DataContractErrorV5("v5 manifest is not formally sealed")
    elif (
        manifest.get("formal_validation_eligible") is not False
        or manifest.get("seal_status") != "PENDING_V5_SEAL"
        or "sealing_commitment" in manifest
    ):
        raise P5DataContractErrorV5("v5 pending manifest state is invalid")
    return manifest


__all__ = [
    "BLIND_INPUT_PATH_V5",
    "BLIND_MATERIALIZATIONS_PATH_V5",
    "BLIND_SEAL_PATH_V5",
    "DATASET_ID_V5",
    "MANIFEST_PATH_V5",
    "NONBLIND_MATERIALIZATIONS_PATH_V5",
    "NONBLIND_PATH_V5",
    "RUN_SPEC_TEMPLATE_PATH_V5",
    "P5DataContractErrorV5",
    "build_pending_manifest_v5",
    "validate_manifest_v5",
    "validate_materialization_v5",
    "validate_v4_source_anchor",
    "validate_v4_v5_byte_identity",
]
