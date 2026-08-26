from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.data_contract import digest, file_sha256
from evals.trip_check_v1.p5.runner_v5 import (
    BLIND_OCR_REPLAY_COUNT_V5,
    BlindDatasetPathsV5,
    BlindExecutionResultV5,
    P5BlindRunnerErrorV5,
    VARIANT_IDS_V5,
    run_blind_once_v5,
    semantic_output_hash_v5,
    _validate_dataset_envelope,
)


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _dataset(tmp_path: Path, *, subject_commit: str) -> BlindDatasetPathsV5:
    data = tmp_path / "data"
    data.mkdir()
    inputs = [
        {
            "case_id": f"p5.blind.{index:03d}",
            "split": "frozen_blind",
            "input_kind": "SYNTHETIC_SCREENSHOT" if index < 45 else "TEXT",
        }
        for index in range(90)
    ]
    materializations = [
        {"case_id": row["case_id"], "materialization_hash": digest(row["case_id"])}
        for row in inputs
    ]
    inputs_path = data / "inputs.jsonl"
    materializations_path = data / "materializations.jsonl"
    template_path = data / "template.json"
    rubric_path = data / "rubric.json"
    seal_path = data / "seal.json"
    manifest_path = data / "manifest.json"
    active_path = data / "active.json"
    _write_jsonl(inputs_path, inputs)
    _write_jsonl(materializations_path, materializations)
    _write_json(template_path, {"schema_version": "test-template"})
    _write_json(rubric_path, {"schema_version": "test-rubric"})
    seal = {
        "schema_version": "trip-check-p5-blind-seal-v5",
        "split": "frozen_blind",
        "case_count": 90,
        "case_set_hash": digest(inputs),
        "materialization_set_hash": digest(materializations),
        "inputs_file_sha256": file_sha256(inputs_path),
        "inputs_content_sha256": digest(inputs),
        "materializations_file_sha256": file_sha256(materializations_path),
        "materializations_content_sha256": digest(materializations),
        "run_spec_template_sha256": file_sha256(template_path),
        "rubric_sha256": file_sha256(rubric_path),
        "variant_ids_sha256": digest(list(VARIANT_IDS_V5)),
        "external_bundle_sha256": "a" * 64,
        "labels_canonical_sha256": "b" * 64,
        "review_receipt_sha256": "c" * 64,
        "label_storage": "external_bundle_only",
        "label_access": "isolated_scorer_only",
        "scoring_payload_present": False,
        "human_evidence": False,
    }
    _write_json(seal_path, seal)
    manifest = {
        "schema_version": "trip-check-p5-dataset-manifest-v5",
        "dataset_id": "trip-check-p5-360-v5",
        "frozen": True,
        "formal_validation_eligible": True,
        "seal_status": "SEALED",
        "files": {
            "blind_cases": {
                "row_count": 90,
                "file_sha256": file_sha256(inputs_path),
                "content_sha256": digest(inputs),
            },
            "blind_materializations": {
                "row_count": 90,
                "file_sha256": file_sha256(materializations_path),
                "content_sha256": digest(materializations),
            },
        },
        "lanes": {
            "frozen_blind": {
                "case_count": 90,
                "materialization_count": 90,
                "case_set_hash": digest(inputs),
                "materialization_set_hash": digest(materializations),
                "label_payload_present": False,
            }
        },
        "sealing_commitment": {
            "blind_seal_file_sha256": file_sha256(seal_path),
            "external_bundle_sha256": seal["external_bundle_sha256"],
            "labels_canonical_sha256": seal["labels_canonical_sha256"],
            "review_receipt_sha256": seal["review_receipt_sha256"],
            "candidate_freeze_commit": subject_commit,
        },
    }
    manifest["manifest_hash"] = digest(manifest)
    _write_json(manifest_path, manifest)
    active = {
        "schema_version": "trip-check-p5-active-contract-v1",
        "active_contract": "trip-check-p5-v5",
        "formal_evidence_status": "READY",
        "dataset_manifest_hash": manifest["manifest_hash"],
        "blind_seal_v5_sha256": file_sha256(seal_path),
        "candidate_freeze_commit": subject_commit,
    }
    _write_json(active_path, active)
    return BlindDatasetPathsV5(
        inputs=inputs_path,
        materializations=materializations_path,
        manifest=manifest_path,
        seal=seal_path,
        run_spec_template=template_path,
        rubric=rubric_path,
        active_contract=active_path,
    )


class _Engine:
    def __init__(self, *, ocr_lookup_count: int = BLIND_OCR_REPLAY_COUNT_V5) -> None:
        self.ocr_lookup_count = ocr_lookup_count

    async def execute(self, **kwargs) -> BlindExecutionResultV5:
        case_rows = kwargs["case_rows"]
        terminals = []
        for variant_id in VARIANT_IDS_V5:
            for case in case_rows:
                receipts = []
                if (
                    case["input_kind"] == "SYNTHETIC_SCREENSHOT"
                    and variant_id in {"core_b", "solver_c"}
                ):
                    receipts.append(
                        {
                            "type": "ocr_replay_provenance",
                            "mode": "FROZEN_ACTUAL_OCR_RECEIPT_REPLAY",
                            "fresh_model_inference": False,
                            "receipt_match": True,
                            "cleanup_status": "DELETED",
                            "cleanup_error_category": None,
                            "temporary_original_absent": True,
                        }
                    )
                terminal = {
                    "case_id": case["case_id"],
                    "input_kind": case["input_kind"],
                    "input_hash": "1" * 64,
                    "materialization_hash": "2" * 64,
                    "run_spec_hash": "3" * 64,
                    "variant_id": variant_id,
                    "adapter_version": f"{variant_id}-v4",
                    "repair_strategy": "read_only" if variant_id == "legacy_a" else "bounded_repair_v1",
                    "terminal_status": "SUCCEEDED",
                    "capability_outcomes": {},
                    "native_output": {},
                    "evaluation_projection": {},
                    "findings": [],
                    "advice": [],
                    "postcheck": None,
                    "receipts": receipts,
                    "token_count": 0,
                    "cost_usd": 0.0,
                    "error_category": None,
                }
                terminal["semantic_output_hash"] = semantic_output_hash_v5(terminal)
                terminal["replay_hash"] = terminal["semantic_output_hash"]
                terminals.append(terminal)
        return BlindExecutionResultV5(
            terminals=tuple(terminals),
            replay_terminals=tuple(dict(row) for row in terminals),
            run_specs={variant_id: {"variant_id": variant_id} for variant_id in VARIANT_IDS_V5},
            screenshot_hashes=frozenset(f"{index:064x}" for index in range(45)),
            ocr_provenance={
                "lookup_count": self.ocr_lookup_count,
                "hit_count": self.ocr_lookup_count,
                "receipt_match_count": self.ocr_lookup_count,
                "cleanup_deleted_count": self.ocr_lookup_count,
                "miss_count": 0,
                "fallback_count": 0,
                "fresh_prediction_count": 0,
                "unique_hash_count": 45,
            },
        )


def _nonce(path: Path) -> None:
    _write_json(
        path,
        {
            "schema_version": "trip-check-p5-blind-run-nonce-v5",
            "purpose": "execute_frozen_blind_once",
            "dataset_id": "trip-check-p5-360-v5",
            "active_contract": "trip-check-p5-v5",
            "nonce": "d" * 64,
        },
    )


def _run(tmp_path: Path, *, engine: _Engine, run_id: str = "blind-v5-test") -> dict:
    subject = "1" * 40
    paths = _dataset(tmp_path, subject_commit=subject)
    nonce = tmp_path / "nonce.json"
    _nonce(nonce)
    return asyncio.run(
        run_blind_once_v5(
            repo_root=Path(__file__).resolve().parents[2],
            dataset_paths=paths,
            output_root=tmp_path / "output",
            consumption_dir=tmp_path / "consumption",
            nonce_file=nonce,
            run_id=run_id,
            subject_commit=subject,
            upstream_ref="origin/codex/p5-v5-blind-score",
            upstream_commit=subject,
            dirty_tree=False,
            engine=engine,
        )
    )


def test_v5_blind_runner_writes_exact_external_shape_and_consumes_nonce(tmp_path: Path) -> None:
    result = _run(tmp_path, engine=_Engine())

    assert result["case_count"] == 90
    assert result["terminal_count"] == 270
    assert result["replay_readback_count"] == 270
    assert result["blind_labels_read"] is False
    assert result["upstream_commit"] == result["subject_commit"]
    assert result["ocr_replay_provenance"] | {} == {
        **result["ocr_replay_provenance"],
        "lookup_count": 180,
        "hit_count": 180,
        "receipt_match_count": 180,
        "cleanup_deleted_count": 180,
        "miss_count": 0,
        "fallback_count": 0,
        "fresh_prediction_count": 0,
        "unique_hash_count": 45,
    }
    receipt_path = next((tmp_path / "consumption").glob("*.consumed.json"))
    consumption = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert consumption["status"] == "CONSUMED"
    assert consumption["run_binding_hash"] == result["run_binding_hash"]
    assert not str(Path(result["run_dir"])).startswith(
        str(Path(__file__).resolve().parents[2])
    )


def test_v5_blind_runner_rejects_review_commitment_mismatch(tmp_path: Path) -> None:
    paths = _dataset(tmp_path, subject_commit="1" * 40)
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    manifest["sealing_commitment"]["review_receipt_sha256"] = "f" * 64
    manifest["manifest_hash"] = digest(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    )
    _write_json(paths.manifest, manifest)
    active = json.loads(paths.active_contract.read_text(encoding="utf-8"))
    active["dataset_manifest_hash"] = manifest["manifest_hash"]
    _write_json(paths.active_contract, active)

    with pytest.raises(P5BlindRunnerErrorV5, match="BLIND_SEAL_COMMITMENT_MISMATCH"):
        _validate_dataset_envelope(paths=paths)


def test_v5_blind_runner_rejects_reusing_nonce_even_with_new_run_id(tmp_path: Path) -> None:
    result = _run(tmp_path, engine=_Engine(), run_id="first")
    paths = BlindDatasetPathsV5(
        inputs=tmp_path / "data" / "inputs.jsonl",
        materializations=tmp_path / "data" / "materializations.jsonl",
        manifest=tmp_path / "data" / "manifest.json",
        seal=tmp_path / "data" / "seal.json",
        run_spec_template=tmp_path / "data" / "template.json",
        rubric=tmp_path / "data" / "rubric.json",
        active_contract=tmp_path / "data" / "active.json",
    )
    del result
    with pytest.raises(P5BlindRunnerErrorV5, match="BLIND_NONCE_ALREADY_CONSUMED"):
        asyncio.run(
            run_blind_once_v5(
                repo_root=Path(__file__).resolve().parents[2],
                dataset_paths=paths,
                output_root=tmp_path / "output",
                consumption_dir=tmp_path / "consumption",
                nonce_file=tmp_path / "nonce.json",
                run_id="second",
                subject_commit="1" * 40,
                upstream_ref="origin/codex/p5-v5-blind-score",
                upstream_commit="1" * 40,
                dirty_tree=False,
                engine=_Engine(),
            )
        )


def test_v5_ocr_count_drift_fails_and_nonce_remains_consumed(tmp_path: Path) -> None:
    with pytest.raises(P5BlindRunnerErrorV5, match="BLIND_OCR_REPLAY_PROVENANCE_INVALID"):
        _run(tmp_path, engine=_Engine(ocr_lookup_count=179))

    receipt_path = next((tmp_path / "consumption").glob("*.consumed.json"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "FAILED"
    assert receipt["failure_reason_code"] == "BLIND_OCR_REPLAY_PROVENANCE_INVALID"
