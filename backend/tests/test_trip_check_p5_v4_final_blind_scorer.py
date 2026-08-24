from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.final_blind_scorer_v4 import (
    P5BlindScoringErrorV4,
    canonical_labels_hash_v4,
    score_isolated_blind_v4,
)
from evals.trip_check_v1.p5.runner_v4 import BlindDatasetPathsV4, VARIANT_IDS_V4


class _Reader:
    role = "blind_custodian"

    def __init__(self, payload: bytes, manifest_hash: str) -> None:
        self.payload = payload
        self.run_group_manifest_hash = manifest_hash
        self.bundle_sha256 = hashlib.sha256(payload).hexdigest()
        self.read_count = 0

    def read_committed_bundle(self, *, expected_sha256: str, repo_root: Path) -> bytes:
        del repo_root
        assert expected_sha256 == self.bundle_sha256
        self.read_count += 1
        return self.payload


def _score(case, output, oracle, materialization):
    del case, oracle, materialization
    return {
        "variant_id": output["variant_id"],
        "task_success": True,
        "score": 100.0,
        "deterministic_pass": True,
        "wrong_city_or_poi_count": 0,
        "missing_reason_codes": [],
        "unknown_preservation": "PASS",
        "candidate_receipt_coverage": "PASS",
        "concurrency_result": "PASS",
        "repair_postcheck": "PASS",
        "replay_hash_match": True,
        "nonpass_finding_count": 1,
        "covered_nonpass_finding_count": 1,
        "unsupported_claim_count": 0,
        "usage_measurement": "PASS",
        "token_count": 0,
        "cost_usd": 0.0,
    }


def _fixture(tmp_path: Path) -> tuple[dict, _Reader, BlindDatasetPathsV4, callable]:
    cases = [{"case_id": f"blind-{index:03d}"} for index in range(90)]
    outputs = [
        {
            "case_id": case["case_id"],
            "variant_id": variant_id,
            "latency_ms": 1.0,
            "terminal_status": "SUCCEEDED",
        }
        for variant_id in VARIANT_IDS_V4
        for case in cases
    ]
    labels = [
        {
            "schema_version": "trip-check-p5-blind-label-v2",
            "case_id": case["case_id"],
            "oracle": {"truth": True},
        }
        for case in reversed(cases)
    ]
    bundle = {
        "schema_version": "trip-check-p5-blind-label-bundle-v2",
        "evidence_class": "controlled_blind_oracle",
        "human_evidence": False,
        "dataset_binding": {"source": "sealed-v2"},
        "labels": labels,
    }
    payload = json.dumps(bundle, sort_keys=True).encode()
    manifest = {
        "manifest_hash": "a" * 64,
        "subject_commit": "b" * 40,
        "dataset_manifest_hash": "c" * 64,
        "terminal_outputs_file_sha256": "d" * 64,
        "terminal_outputs_content_sha256": "e" * 64,
        "artifact_index_hash": "f" * 64,
        "blind_seal_sha256": "1" * 64,
        "run_spec_template_sha256": "2" * 64,
    }
    reader = _Reader(payload, manifest["manifest_hash"])
    seal = tmp_path / "seal.json"
    seal.write_text(
        json.dumps(
            {
                "external_bundle_sha256": reader.bundle_sha256,
                "labels_canonical_sha256": canonical_labels_hash_v4(labels),
            }
        ),
        encoding="utf-8",
    )
    paths = BlindDatasetPathsV4(
        inputs=tmp_path / "unused-inputs",
        materializations=tmp_path / "unused-materializations",
        manifest=tmp_path / "unused-manifest",
        seal=seal,
        run_spec_template=tmp_path / "unused-template",
        rubric=tmp_path / "unused-rubric",
        active_contract=tmp_path / "unused-active",
    )

    def validator(**kwargs):
        del kwargs
        return manifest, cases, outputs, {case["case_id"]: {} for case in cases}

    return manifest, reader, paths, validator


def test_v4_scorer_returns_only_variant_aggregates_without_label_side_channels(
    tmp_path: Path,
) -> None:
    manifest, reader, paths, validator = _fixture(tmp_path)
    report = score_isolated_blind_v4(
        repo_root=tmp_path / "repo",
        run_dir=tmp_path / "run",
        expected_bundle_sha256=reader.bundle_sha256,
        custodian_reader=reader,
        dataset_paths=paths,
        run_validator=validator,
        oracle_validator=lambda value: value,
        case_scorer=_score,
    )

    assert report["status"] == "PASS"
    assert report["bindings"]["run_group_manifest_hash"] == manifest["manifest_hash"]
    assert report["case_count"] == 90
    assert report["terminal_count"] == 270
    assert report["replay_readback_count"] == 270
    assert set(report["variant_metrics"]) == set(VARIANT_IDS_V4)
    assert all(metrics["case_count"] == 90 for metrics in report["variant_metrics"].values())
    serialized = json.dumps(report, sort_keys=True)
    assert reader.read_count == 1
    assert all(case_id not in serialized for case_id in ("blind-000", "blind-089"))
    assert all(token not in serialized for token in ('"labels"', '"oracle"', '"case_id"', '"buckets"'))


def test_v4_scorer_rejects_non_custodian_before_bundle_read(tmp_path: Path) -> None:
    _manifest, reader, paths, validator = _fixture(tmp_path)
    reader.role = "developer"  # type: ignore[misc]

    with pytest.raises(P5BlindScoringErrorV4, match="CUSTODIAN_AUTHORIZATION_BINDING_MISMATCH"):
        score_isolated_blind_v4(
            repo_root=tmp_path / "repo",
            run_dir=tmp_path / "run",
            expected_bundle_sha256=reader.bundle_sha256,
            custodian_reader=reader,
            dataset_paths=paths,
            run_validator=validator,
            oracle_validator=lambda value: value,
            case_scorer=_score,
        )

    assert reader.read_count == 0
