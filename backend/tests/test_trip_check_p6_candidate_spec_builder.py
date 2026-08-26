from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p6.candidate_spec_builder import (
    _validate_p5_gate_manifest,
    build_candidate_run_spec,
)
from evals.trip_check_v1.p6.contracts_v1 import P6ContractError, digest, file_sha256
from tests.test_trip_check_p6_real_ocr_runner import SUBJECT, _fixture


MODEL_ARTIFACTS = [{
    "model_name": "PP-OCRv6_medium_det",
    "filename": "inference.pdiparams",
    "sha256": "1" * 64,
    "bytes": 1,
}]


def _p5_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "p5-gate-manifest.json"
    path.write_bytes(b"p5-gate-fixture")
    return path


def test_p5_binding_uses_the_manifest_self_hash_not_file_bytes(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    value = {
        "schema_version": "trip-check-p5-evaluation-gate-v5",
        "status": "PASS",
        "dirty_tree": False,
        "promotion_decision": "KEEP_CORE_B",
    }
    value["manifest_hash"] = digest(value)
    path = tmp_path / "p5.json"
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    monkeypatch.setattr(
        "evals.trip_check_v1.p6.candidate_spec_builder.P5_GATE_MANIFEST_HASH",
        value["manifest_hash"],
    )
    _validate_p5_gate_manifest(path)


def test_candidate_spec_builder_binds_all_inputs(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    p5 = _p5_manifest(tmp_path)
    spec, spec_path = build_candidate_run_spec(
        ocr_dataset_manifest_path=paths["dataset_manifest_path"],
        p5_gate_manifest_path=p5,
        output_root=tmp_path / "inputs",
        repo_root=Path(__file__).parents[2],
        formal=False,
        subject_commit=SUBJECT,
        p5_validator=lambda path: None,
        model_artifact_builder=lambda: MODEL_ARTIFACTS,
    )
    assert spec["subject_commit"] == SUBJECT
    assert len(spec["bindings"]["ocr_dataset_manifest_sha256"]) == 64
    assert spec_path.is_file()
    assert {path.name for path in spec_path.parent.iterdir()} == {
        "candidate_run_spec.json",
        "config_manifest.json",
        "migration_manifest.json",
        "model_manifest.json",
        "rule_manifest.json",
        "snapshot_manifest.json",
    }
    config_manifest = json.loads(
        (spec_path.parent / "config_manifest.json").read_text(encoding="utf-8")
    )
    controlled_snapshot = Path(__file__).parents[1] / "app" / "data" / "amap_mock_places.json"
    assert {
        "path": "backend/app/data/amap_mock_places.json",
        "sha256": file_sha256(controlled_snapshot),
        "bytes": controlled_snapshot.stat().st_size,
    } in config_manifest["files"]
    assert any(item["path"] == ".gitattributes" for item in config_manifest["files"])
    assert any(item["path"] == "backend/.dockerignore" for item in config_manifest["files"])


def test_candidate_spec_builder_rejects_dataset_subject_mismatch(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    p5 = _p5_manifest(tmp_path)
    with pytest.raises(P6ContractError, match="P6_REAL_OCR_RUN_SPEC_BINDING_INVALID"):
        build_candidate_run_spec(
            ocr_dataset_manifest_path=paths["dataset_manifest_path"],
            p5_gate_manifest_path=p5,
            output_root=tmp_path / "inputs",
            repo_root=Path(__file__).parents[2],
            formal=False,
            subject_commit="b" * 40,
            p5_validator=lambda path: None,
            model_artifact_builder=lambda: MODEL_ARTIFACTS,
        )
