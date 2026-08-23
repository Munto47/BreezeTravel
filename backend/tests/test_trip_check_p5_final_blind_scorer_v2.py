from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import evals.trip_check_v1.p5.final_blind_scorer_v2 as blind_v2
from evals.trip_check_v1.p5.contracts_v2 import VARIANT_IDS_V2
from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest
from evals.trip_check_v1.p5.final_blind_scorer_v2 import (
    P5BlindScoringErrorV2,
    canonical_labels_hash_v2,
    score_external_blind_run_group_v2,
)
from tests.test_trip_check_p5_scorer_v2 import (
    _case,
    _materialization_row,
    _oracle,
    _output,
    _spec,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    cases = [_case(f"p5.blind.bj.{index:03d}", split="frozen_blind") for index in range(90)]
    raw_inputs = [case.model_dump(mode="json") for case in cases]
    inputs_path = repo / "frozen_blind.v2.inputs.jsonl"
    _write_jsonl(inputs_path, raw_inputs)
    materialization_rows = [_materialization_row(case) for case in cases]
    materializations_path = repo / "frozen_blind.v2.materializations.jsonl"
    _write_jsonl(materializations_path, materialization_rows)
    template = repo / "run_spec_template_v2.json"
    rubric = repo / "rubric_v2.json"
    template.write_text('{"schema_version":"template-v2"}\n', encoding="utf-8")
    rubric.write_text('{"schema_version":"rubric-v2"}\n', encoding="utf-8")
    labels = [
        {
            "schema_version": "trip-check-p5-blind-label-v2",
            "case_id": case.case_id,
            "oracle": _oracle().model_dump(mode="json"),
        }
        for case in reversed(cases)
    ]
    binding = {
        "case_count": 90,
        "case_ids_sha256": digest(sorted(case.case_id for case in cases)),
        "inputs_file_sha256": _sha(inputs_path),
        "inputs_content_sha256": digest(raw_inputs),
        "materializations_file_sha256": _sha(materializations_path),
        "materializations_content_sha256": digest(materialization_rows),
        "run_spec_template_sha256": _sha(template),
        "rubric_sha256": _sha(rubric),
        "variant_ids_sha256": digest(list(VARIANT_IDS_V2)),
    }
    bundle = {
        "schema_version": "trip-check-p5-blind-label-bundle-v2",
        "evidence_class": "controlled_blind_oracle",
        "human_evidence": False,
        "dataset_binding": binding,
        "labels": labels,
    }
    bundle_path = external / "bundle.json"
    bundle_path.write_bytes(canonical_bytes(bundle) + b"\n")
    seal = {
        "schema_version": "trip-check-p5-blind-seal-v2",
        "split": "frozen_blind",
        **binding,
        "labels_canonical_sha256": canonical_labels_hash_v2(labels),
        "external_bundle_sha256": _sha(bundle_path),
        "review_receipt_sha256": "f" * 64,
        "label_storage": "external_bundle_only",
        "label_access": "isolated_scorer_only",
        "scoring_payload_present": False,
        "human_evidence": False,
    }
    seal_path = repo / "seal.json"
    seal_path.write_text(json.dumps(seal, ensure_ascii=False) + "\n", encoding="utf-8")
    dataset_path = repo / "dataset.json"
    dataset_path.write_text("{}\n", encoding="utf-8")
    specs = {
        variant_id: _spec(cases[0], variant_id, "a" * 64).model_copy(
            update={
                "lane": "frozen_blind",
                "case_set_hash": "b" * 64,
                "materialization_set_hash": "c" * 64,
            }
        )
        for variant_id in VARIANT_IDS_V2
    }
    outputs = [
        _output(case, specs[variant_id])
        for case in cases
        for variant_id in VARIANT_IDS_V2
    ]
    manifest = {
        "subject_commit": "a" * 40,
        "dataset_manifest_hash": "a" * 64,
        "manifest_hash": "b" * 64,
        "terminal_outputs_file_sha256": "c" * 64,
    }

    def validated(**kwargs):
        del kwargs
        return manifest, cases, outputs

    monkeypatch.setattr(blind_v2, "validate_run_group_v2", validated)
    return {
        "repo": repo,
        "run_dir": repo / "run",
        "dataset": dataset_path,
        "inputs": inputs_path,
        "materializations": materializations_path,
        "seal": seal_path,
        "template": template,
        "rubric": rubric,
        "bundle": bundle_path,
        "bundle_hash": _sha(bundle_path),
        "labels": labels,
    }


def _score(fixture: dict, **updates):
    arguments = {
        "repo_root": fixture["repo"],
        "run_dir": fixture["run_dir"],
        "dataset_manifest_path": fixture["dataset"],
        "inputs_path": fixture["inputs"],
        "materializations_path": fixture["materializations"],
        "seal_path": fixture["seal"],
        "run_spec_template_path": fixture["template"],
        "rubric_path": fixture["rubric"],
        "expected_bundle_sha256": fixture["bundle_hash"],
        "bundle_path": fixture["bundle"],
        "require_current_subject": False,
    }
    arguments.update(updates)
    return score_external_blind_run_group_v2(**arguments)


def test_v2_label_commitment_sorts_case_id_and_hashes_each_lf_record() -> None:
    labels = [
        {"case_id": "b", "oracle": {"value": 2}},
        {"case_id": "a", "oracle": {"value": 1}},
    ]
    expected = hashlib.sha256(
        canonical_bytes(labels[1]) + b"\n" + canonical_bytes(labels[0]) + b"\n"
    ).hexdigest()

    assert canonical_labels_hash_v2(labels) == expected


def test_v2_formal_contract_blocks_before_external_bundle_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def not_ready() -> None:
        raise RuntimeError("not ready")

    monkeypatch.setattr(blind_v2, "require_v2_formal_ready", not_ready)
    with pytest.raises(P5BlindScoringErrorV2, match="P5_V2_FORMAL_CONTRACT_NOT_READY"):
        score_external_blind_run_group_v2(
            repo_root=tmp_path,
            run_dir=tmp_path / "missing-run",
            dataset_manifest_path=tmp_path / "missing-dataset",
            inputs_path=tmp_path / "missing-inputs",
            materializations_path=tmp_path / "missing-materializations",
            seal_path=tmp_path / "missing-seal",
            run_spec_template_path=tmp_path / "missing-template",
            rubric_path=tmp_path / "missing-rubric",
            expected_bundle_sha256="0" * 64,
            bundle_bytes=b'{"must_not_be_read":true}',
            require_current_subject=True,
        )


def test_v2_blind_output_is_strict_aggregate_with_minimum_bucket_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    receipt = _score(fixture)

    assert receipt["status"] == "PASS"
    assert receipt["minimum_bucket_size"] == 5
    assert "case_scores" not in receipt
    serialized = json.dumps(receipt, ensure_ascii=False)
    assert "p5.blind.bj." not in serialized
    assert all(
        bucket["case_count"] >= 5
        for variant in receipt["variant_metrics"].values()
        for dimension, buckets in variant.items()
        if dimension != "overall"
        for bucket in buckets.values()
    )


def test_v2_blind_bundle_rejects_repository_path_and_extra_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    inside = fixture["repo"] / "bundle.json"
    inside.write_bytes(fixture["bundle"].read_bytes())
    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_BUNDLE_INSIDE_REPOSITORY"):
        _score(fixture, bundle_path=inside)

    payload = json.loads(fixture["bundle"].read_text(encoding="utf-8"))
    payload["labels"][0]["oracle"]["unexpected"] = True
    fixture["bundle"].write_bytes(canonical_bytes(payload) + b"\n")
    bundle_hash = _sha(fixture["bundle"])
    seal = json.loads(fixture["seal"].read_text(encoding="utf-8"))
    seal["external_bundle_sha256"] = bundle_hash
    fixture["seal"].write_text(json.dumps(seal) + "\n", encoding="utf-8")
    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_BUNDLE_ORACLE_SCHEMA_INVALID"):
        _score(
            fixture,
            expected_bundle_sha256=bundle_hash,
        )


def test_v2_blind_bundle_rejects_stale_materialization_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    payload = json.loads(fixture["bundle"].read_text(encoding="utf-8"))
    payload["dataset_binding"]["materializations_content_sha256"] = "0" * 64
    fixture["bundle"].write_bytes(canonical_bytes(payload) + b"\n")
    changed_hash = _sha(fixture["bundle"])
    seal = json.loads(fixture["seal"].read_text(encoding="utf-8"))
    seal["external_bundle_sha256"] = changed_hash
    fixture["seal"].write_text(json.dumps(seal) + "\n", encoding="utf-8")

    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_BUNDLE_STALE_DATASET_BINDING"):
        _score(fixture, expected_bundle_sha256=changed_hash)
