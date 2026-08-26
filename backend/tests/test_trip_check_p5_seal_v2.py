from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import evals.trip_check_v1.p5.seal_v2 as seal_module
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.seal_v2 import (
    P5V2SealError,
    SealPathsV2,
    seal_and_activate_v2,
    validate_activation_readback_v2,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
CANDIDATE = "1" * 40


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _file_sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def seal_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    paths = SealPathsV2.for_repo(tmp_path)
    inputs = [{"case_id": f"p5.blind.case.{index:03d}", "case_hash": f"{index:064x}"} for index in range(90)]
    materializations = [
        {"case_id": row["case_id"], "materialization_hash": f"{index + 1000:064x}"}
        for index, row in enumerate(inputs)
    ]
    _write_jsonl(paths.inputs_path, inputs)
    _write_jsonl(paths.materializations_path, materializations)
    _write_json(paths.run_spec_template_path, {"schema_version": "trip-check-p5-run-spec-v2"})
    _write_json(paths.rubric_path, {"schema_version": "trip-check-p5-judge-rubric-v2"})
    schema_source = Path(__file__).resolve().parents[1] / "evals/trip_check_v1/p5/blind_seal_v2.schema.json"
    paths.seal_schema_path.write_bytes(schema_source.read_bytes())
    contract_path = tmp_path / "schemas" / "contract.json"
    _write_json(contract_path, {"type": "object", "additionalProperties": False})
    monkeypatch.setattr(seal_module, "SCHEMA_CONTRACT_PATHS_V2", ("schemas/contract.json",))

    base_manifest = {
        "schema_version": "trip-check-p5-dataset-manifest-v2",
        "generation": {"ocr_mode": "actual"},
        "files": {
            "blind_cases": {
                "file_sha256": _file_sha(paths.inputs_path),
                "content_sha256": digest(inputs),
            },
            "blind_materializations": {
                "file_sha256": _file_sha(paths.materializations_path),
                "content_sha256": digest(materializations),
            },
        },
    }
    manifest = {**base_manifest, "manifest_hash": digest(base_manifest)}
    _write_json(paths.dataset_manifest_path, manifest)
    _write_json(
        paths.active_contract_path,
        {
            "schema_version": "trip-check-p5-active-contract-v1",
            "active_contract": "trip-check-p5-v2",
            "formal_evidence_status": "PENDING_V2_SEAL",
            "deprecated_contracts": [
                {
                    "contract_id": "trip-check-p5-v1",
                    "formal_evidence_eligible": False,
                    "reason": "SUPERSEDED_BY_USER_APPROVED_P5_V2",
                }
            ],
        },
    )
    return {"paths": paths, "inputs": inputs, "materializations": materializations}


def _seal(fixture: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    arguments = {
        "paths": fixture["paths"],
        "labels_canonical_sha256": HASH_A,
        "external_bundle_sha256": HASH_B,
        "review_receipt_sha256": HASH_C,
        "candidate_freeze_commit": CANDIDATE,
        "dataset_validator": lambda: {"status": "PASS", "formal": True},
        "enforce_git": False,
    }
    arguments.update(overrides)
    return seal_and_activate_v2(**arguments)


def test_v2_seal_activates_strict_bindings_without_self_referential_subject_sha(
    seal_fixture: dict[str, Any],
) -> None:
    result = _seal(seal_fixture)
    paths = seal_fixture["paths"]
    seal = json.loads(paths.seal_path.read_text(encoding="utf-8"))
    manifest = json.loads(paths.dataset_manifest_path.read_text(encoding="utf-8"))
    active = json.loads(paths.active_contract_path.read_text(encoding="utf-8"))

    assert result["status"] == "READY"
    assert result["idempotent"] is False
    assert seal["case_count"] == 90
    assert seal["labels_canonical_sha256"] == HASH_A
    assert seal["external_bundle_sha256"] == HASH_B
    assert seal["review_receipt_sha256"] == HASH_C
    assert manifest["sealing_commitment"]["candidate_freeze_commit"] == CANDIDATE
    assert active["formal_evidence_status"] == "READY"
    assert active["candidate_freeze_commit"] == CANDIDATE
    assert "formal_subject_commit" not in active
    assert active["blind_seal_v2_sha256"] == _file_sha(paths.seal_path)
    assert validate_activation_readback_v2(paths=paths)["status"] == "PASS"


def test_same_value_rerun_is_idempotent(seal_fixture: dict[str, Any]) -> None:
    first = _seal(seal_fixture)
    before = {path: path.read_bytes() for path in seal_fixture["paths"].__dict__.values() if isinstance(path, Path) and path.is_file()}
    second = _seal(seal_fixture)
    after = {path: path.read_bytes() for path in before}
    assert first["blind_seal_v2_sha256"] == second["blind_seal_v2_sha256"]
    assert second["idempotent"] is True
    assert after == before


def test_same_value_rerun_accepts_only_activation_dirty_paths_or_clean_child_commit(
    seal_fixture: dict[str, Any],
) -> None:
    _seal(seal_fixture)
    allowed_status = "\n".join(
        [
            " M backend/evals/trip_check_v1/p5/active_contract.json",
            " M backend/evals/trip_check_v1/p5/dataset_v2.manifest.json",
            "?? backend/evals/trip_check_v1/p5/sealed/frozen_blind.v2.seal.json",
        ]
    )

    def dirty_activation_git(_root: Path, arguments: Any) -> str:
        if arguments == ("rev-parse", "HEAD"):
            return CANDIDATE
        return allowed_status

    dirty_result = _seal(seal_fixture, enforce_git=True, git_output=dirty_activation_git)
    assert dirty_result["idempotent"] is True

    def clean_child_git(_root: Path, arguments: Any) -> str:
        if arguments == ("rev-parse", "HEAD"):
            return "2" * 40
        if arguments == ("rev-parse", "HEAD^"):
            return CANDIDATE
        return ""

    committed_result = _seal(seal_fixture, enforce_git=True, git_output=clean_child_git)
    assert committed_result["idempotent"] is True


@pytest.mark.parametrize("target", ["inputs_path", "materializations_path", "run_spec_template_path", "rubric_path"])
def test_bound_artifact_tampering_is_rejected(seal_fixture: dict[str, Any], target: str) -> None:
    _seal(seal_fixture)
    path = getattr(seal_fixture["paths"], target)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(P5V2SealError):
        _seal(seal_fixture)


def test_schema_contract_tampering_is_rejected(seal_fixture: dict[str, Any]) -> None:
    _seal(seal_fixture)
    contract = seal_fixture["paths"].repo_root / "schemas/contract.json"
    contract.write_bytes(contract.read_bytes() + b" ")
    with pytest.raises(P5V2SealError, match="P5_V2_SEALING_COMMITMENT_DRIFT"):
        _seal(seal_fixture)


def test_extra_seal_field_and_v1_active_contract_are_rejected(seal_fixture: dict[str, Any]) -> None:
    _seal(seal_fixture)
    seal = json.loads(seal_fixture["paths"].seal_path.read_text(encoding="utf-8"))
    seal["case_scores"] = []
    _write_json(seal_fixture["paths"].seal_path, seal)
    with pytest.raises(P5V2SealError, match="P5_V2_BLIND_SEAL_DRIFT_OVERWRITE_FORBIDDEN"):
        _seal(seal_fixture)

    seal_fixture["paths"].seal_path.unlink()
    active = json.loads(seal_fixture["paths"].active_contract_path.read_text(encoding="utf-8"))
    active["active_contract"] = "trip-check-p5-v1"
    active["formal_evidence_status"] = "PENDING_V2_SEAL"
    _write_json(seal_fixture["paths"].active_contract_path, active)
    with pytest.raises(P5V2SealError, match="P5_V2_ACTIVE_CONTRACT_INVALID"):
        _seal(seal_fixture)


def test_formal_validator_and_lowercase_hashes_fail_before_writes(seal_fixture: dict[str, Any]) -> None:
    with pytest.raises(P5V2SealError, match="P5_V2_FORMAL_DATASET_VALIDATION_FAILED"):
        _seal(seal_fixture, dataset_validator=lambda: {"status": "FAIL", "formal": True})
    assert not seal_fixture["paths"].seal_path.exists()

    with pytest.raises(P5V2SealError, match="P5_V2_SEAL_HASH_INVALID"):
        _seal(seal_fixture, labels_canonical_sha256="A" * 64)
    assert not seal_fixture["paths"].seal_path.exists()


def test_git_preflight_rejects_dirty_and_mixed_commits(seal_fixture: dict[str, Any]) -> None:
    def dirty_git(_root: Path, arguments: Any) -> str:
        return CANDIDATE if arguments[0] == "rev-parse" else " M user-file"

    with pytest.raises(P5V2SealError, match="P5_V2_DIRTY_TREE_FORBIDDEN"):
        _seal(seal_fixture, enforce_git=True, git_output=dirty_git)

    def mixed_git(_root: Path, arguments: Any) -> str:
        return "2" * 40 if arguments[0] == "rev-parse" else ""

    with pytest.raises(P5V2SealError, match="P5_V2_MIXED_OR_WRONG_CANDIDATE_COMMIT"):
        _seal(seal_fixture, enforce_git=True, git_output=mixed_git)


def test_atomic_seal_failure_does_not_activate_contract(seal_fixture: dict[str, Any]) -> None:
    def fail_write(_path: Path, _payload: bytes) -> None:
        raise OSError("simulated replace failure")

    with pytest.raises(OSError, match="simulated replace failure"):
        _seal(seal_fixture, atomic_write=fail_write)
    assert not seal_fixture["paths"].seal_path.exists()
    active = json.loads(seal_fixture["paths"].active_contract_path.read_text(encoding="utf-8"))
    assert active["formal_evidence_status"] == "PENDING_V2_SEAL"


def test_active_and_manifest_readback_drift_is_rejected(seal_fixture: dict[str, Any]) -> None:
    _seal(seal_fixture)
    active = json.loads(seal_fixture["paths"].active_contract_path.read_text(encoding="utf-8"))
    active["dataset_manifest_hash"] = HASH_A
    _write_json(seal_fixture["paths"].active_contract_path, active)
    with pytest.raises(P5V2SealError, match="P5_V2_ACTIVE_MANIFEST_READBACK_MISMATCH"):
        validate_activation_readback_v2(paths=seal_fixture["paths"])
