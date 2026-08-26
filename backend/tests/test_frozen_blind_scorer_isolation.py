from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

from evals.continuous import http_builder, http_import
from evals.final_blind_scorer import BlindScoringError, main, score_external_blind_bundle


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical(value) + b"\n"
    path.write_bytes(payload)
    return payload


def _write_jsonl(path: Path, rows: list[dict]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(_canonical(row) + b"\n" for row in rows)
    path.write_bytes(payload)
    return payload


def _na(reason_code: str) -> dict:
    return {"applicability": "N_A", "reason_code": reason_code}


@pytest.fixture
def blind_run(tmp_path: Path) -> dict:
    repo = tmp_path / "repo"
    external = tmp_path / "isolated-secret"
    data = repo / "backend" / "eval_data" / "dual_entry_v1"
    run_id = "run-frozen-blind-001"
    case_id = "blind.test.import.01"
    case = {
        "case_id": case_id,
        "split": "frozen_blind",
        "entry": "IMPORT",
        "input": {"raw_itinerary": "D1 故宫"},
    }
    input_bytes = _write_jsonl(data / "frozen_blind.inputs.jsonl", [case])
    metric_oracles = {
        "parse_f1": {
            "applicability": "APPLICABLE",
            "metric_version": "set-f1-v1",
            "normalization": "unicode-nfc-trim-collapse-space",
            "ground_truth_items": [{"stop_name": "故宫"}],
        },
        "entity_precision_recall": _na("NO_STRUCTURED_ENTITY_TRUTH"),
        "finding_precision_recall": _na("NO_STRUCTURED_FINDING_TRUTH"),
        "repair_postcheck": _na("NO_STRUCTURED_REPAIR_TRUTH"),
        "builder_ndcg_at_5": _na("NO_GRADED_RANKING_TRUTH"),
        "builder_recall_at_5": _na("NO_RELEVANT_CANDIDATE_TRUTH"),
    }
    label = {
        "schema_version": "dual-entry-label-v1",
        "case_id": case_id,
        "deterministic_truth": {
            "must_pass": [],
            "must_fail": [],
            "must_be_unknown": [],
            "must_not_happen": ["UNKNOWN_PROMOTED"],
        },
        "metric_oracles": metric_oracles,
        "gate_assertions": ["UNKNOWN_PROMOTED=0"],
    }
    case_ids_sha256 = _sha(_canonical([case_id]))
    labels_canonical_sha256 = _sha(_canonical(label) + b"\n")
    seal = {
        "schema_version": "dual-entry-sealed-label-manifest-v1",
        "split": "frozen_blind",
        "scoring_payload_present": False,
        "external_bundle_required": True,
        "bundle_schema_version": "dual-entry-blind-label-bundle-v1",
        "case_count": 1,
        "case_ids_sha256": case_ids_sha256,
        "labels_canonical_sha256": labels_canonical_sha256,
        "truth_provenance": "controlled_blind_oracle",
        "human_evidence": False,
    }
    seal_name = "sealed/frozen_blind.labels.jsonl"
    seal_bytes = _write_json(data / seal_name, seal)
    manifest = {
        "schema_version": "dual-entry-manifest-v1",
        "dataset_id": "test-blind-v1",
        "files": [
            {
                "split": "frozen_blind",
                "inputs": "frozen_blind.inputs.jsonl",
                "labels_seal": seal_name,
                "labels_seal_sha256": _sha(seal_bytes),
                "label_storage": "external_bundle_only",
                "case_count": 1,
            }
        ],
    }
    manifest_bytes = _write_json(data / "manifest.json", manifest)
    manifest_sha256 = _sha(manifest_bytes)
    dataset_content_sha256 = _sha(
        _canonical(
            {
                "manifest_sha256": manifest_sha256,
                "input_files": {"frozen_blind.inputs.jsonl": _sha(input_bytes)},
                "sealed_label_manifests": {seal_name: _sha(seal_bytes)},
            }
        )
    )
    run_dir = repo / "backend" / "evidence" / "runs" / run_id
    outputs = [
        {
            "schema_version": "continuous-import-product-output-v1",
            "case_id": case_id,
            "metric_actuals": {"parse_items": [{"stop_name": "故宫"}]},
        }
    ]
    output_bytes = _write_jsonl(run_dir / "product_outputs.jsonl", outputs)
    run_spec = {
        "schema_version": "dual-entry-run-spec-v1",
        "run_id": run_id,
        "lane": "release_blind",
        "purpose": "promotion",
        "dataset": {
            "manifest": "backend/eval_data/dual_entry_v1/manifest.json",
            "manifest_sha256": manifest_sha256,
            "case_ids_sha256": case_ids_sha256,
            "splits": ["frozen_blind"],
            "label_access": "isolated_scorer_only",
        },
        "execution": {
            "bindings": {
                "manifest_sha256": manifest_sha256,
                "case_ids_sha256": case_ids_sha256,
                "dataset_content_sha256": dataset_content_sha256,
            }
        },
        "thresholds": {"parse_f1": 1.0},
    }
    run_spec_bytes = _write_json(run_dir / "run_spec.json", run_spec)
    bundle = {
        "schema_version": "dual-entry-blind-label-bundle-v1",
        "evidence_class": "controlled_blind_oracle",
        "human_evidence": False,
        "run_binding": {
            "run_id": run_id,
            "run_spec_sha256": _sha(run_spec_bytes),
            "dataset_content_sha256": dataset_content_sha256,
            "manifest_sha256": manifest_sha256,
            "case_ids_sha256": case_ids_sha256,
            "product_outputs_sha256": _sha(output_bytes),
        },
        "labels": [label],
    }
    bundle_path = external / "frozen-blind.bundle.json"
    bundle_bytes = _write_json(bundle_path, bundle)
    return {
        "repo": repo,
        "run_dir": run_dir,
        "bundle": bundle,
        "bundle_path": bundle_path,
        "bundle_bytes": bundle_bytes,
        "bundle_sha256": _sha(bundle_bytes),
    }


def _score(fixture: dict, **overrides):
    arguments = {
        "repo_root": fixture["repo"],
        "bundle_path": fixture["bundle_path"],
        "expected_bundle_sha256": fixture["bundle_sha256"],
    }
    arguments.update(overrides)
    return score_external_blind_bundle(fixture["run_dir"], **arguments)


def test_external_bundle_scores_only_after_all_run_bindings_match(blind_run):
    receipt = _score(blind_run)

    assert receipt["status"] == "PASS"
    assert receipt["decision"] == "ACCEPT_BLIND_SCORE"
    assert receipt["bundle_origin"] == "external_bundle_path"
    assert receipt["human_evidence"] is False
    assert receipt["aggregate"]["metrics"]["parse_f1"]["value"] == 1.0
    assert receipt["scored_case_count"] == 1
    assert receipt["invalid_case_count"] == 0
    assert "case_scores" not in receipt
    assert "applicable_case_ids" not in receipt["aggregate"]["metrics"]["parse_f1"]
    assert all(gate["status"] == "PASS" for gate in receipt["threshold_gates"])


def test_isolated_process_stdin_is_an_explicit_supported_bundle_source(blind_run, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(blind_run["bundle_bytes"]), encoding="utf-8"))

    exit_code = main(
        [
            "--run-dir",
            str(blind_run["run_dir"]),
            "--repo-root",
            str(blind_run["repo"]),
            "--bundle",
            "-",
            "--bundle-sha256",
            blind_run["bundle_sha256"],
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["bundle_origin"] == "isolated_process_input"


def test_missing_bundle_and_repository_bundle_path_fail_before_payload_read(blind_run):
    with pytest.raises(BlindScoringError, match="exactly one") as missing:
        _score(blind_run, bundle_path=None)
    assert missing.value.reason_code == "BLIND_BUNDLE_SOURCE_REQUIRED"

    with pytest.raises(BlindScoringError) as absent:
        _score(blind_run, bundle_path=blind_run["bundle_path"].with_name("missing.bundle.json"))
    assert absent.value.reason_code == "BLIND_BUNDLE_MISSING"

    repository_bundle = blind_run["repo"] / "secrets" / "bundle.json"
    repository_bytes = _write_json(repository_bundle, blind_run["bundle"])
    with pytest.raises(BlindScoringError) as exposed:
        _score(
            blind_run,
            bundle_path=repository_bundle,
            expected_bundle_sha256=_sha(repository_bytes),
        )
    assert exposed.value.reason_code == "BLIND_BUNDLE_PATH_INSIDE_REPOSITORY"


def test_bundle_byte_tampering_fails_against_independent_hash(blind_run):
    blind_run["bundle_path"].write_bytes(blind_run["bundle_bytes"] + b" ")

    with pytest.raises(BlindScoringError) as error:
        _score(blind_run)

    assert error.value.reason_code == "BLIND_BUNDLE_SHA256_MISMATCH"


def test_checked_in_seal_or_dataset_tampering_fails_against_run_binding(blind_run):
    manifest_path = blind_run["repo"] / "backend/eval_data/dual_entry_v1/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    seal_path = manifest_path.parent / manifest["files"][0]["labels_seal"]
    seal_path.write_bytes(seal_path.read_bytes() + b" ")
    with pytest.raises(BlindScoringError) as seal_error:
        _score(blind_run)
    assert seal_error.value.reason_code == "BLIND_LABEL_SEAL_SHA256_MISMATCH"

    seal_path.write_bytes(seal_path.read_bytes()[:-1])
    inputs_path = manifest_path.parent / "frozen_blind.inputs.jsonl"
    inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    inputs["input"]["raw_itinerary"] = "D1 被篡改"
    _write_jsonl(inputs_path, [inputs])
    with pytest.raises(BlindScoringError) as dataset_error:
        _score(blind_run)
    assert dataset_error.value.reason_code == "RUN_SPEC_DATASET_HASH_MISMATCH"


@pytest.mark.parametrize(
    ("binding", "expected_code"),
    [
        ("run_id", "BLIND_BUNDLE_RUN_ID_MISMATCH"),
        ("run_spec_sha256", "BLIND_BUNDLE_RUN_SPEC_SHA256_MISMATCH"),
        ("dataset_content_sha256", "BLIND_BUNDLE_DATASET_CONTENT_SHA256_MISMATCH"),
        ("manifest_sha256", "BLIND_BUNDLE_MANIFEST_SHA256_MISMATCH"),
        ("case_ids_sha256", "BLIND_BUNDLE_CASE_IDS_SHA256_MISMATCH"),
        ("product_outputs_sha256", "BLIND_BUNDLE_PRODUCT_OUTPUTS_SHA256_MISMATCH"),
    ],
)
def test_bundle_must_bind_exact_run_spec_dataset_cases_and_outputs(blind_run, binding, expected_code):
    bundle = json.loads(blind_run["bundle_bytes"])
    bundle["run_binding"][binding] = "0" * 64
    if binding == "run_id":
        bundle["run_binding"][binding] = "other-run"
    changed = _write_json(blind_run["bundle_path"], bundle)

    with pytest.raises(BlindScoringError) as error:
        _score(blind_run, expected_bundle_sha256=_sha(changed))

    assert error.value.reason_code == expected_code


def test_label_payload_must_match_checked_in_non_scoreable_commitment(blind_run):
    bundle = json.loads(blind_run["bundle_bytes"])
    bundle["labels"][0]["metric_oracles"]["parse_f1"]["ground_truth_items"] = [{"stop_name": "天坛"}]
    changed = _write_json(blind_run["bundle_path"], bundle)

    with pytest.raises(BlindScoringError) as error:
        _score(blind_run, expected_bundle_sha256=_sha(changed))

    assert error.value.reason_code == "BLIND_LABEL_COMMITMENT_MISMATCH"


def test_development_http_runners_reject_blind_before_any_dataset_file_read(blind_run, monkeypatch):
    spec = {
        "lane": "release_blind",
        "dataset": {
            "manifest": "backend/eval_data/dual_entry_v1/manifest.json",
            "splits": ["frozen_blind"],
            "label_access": "isolated_scorer_only",
        },
    }
    reads: list[Path] = []

    def forbidden_read(path: Path):
        reads.append(path)
        raise AssertionError("development runner attempted dataset read")

    monkeypatch.setattr(http_import, "_load_rows", forbidden_read)
    monkeypatch.setattr(http_builder, "_load_rows", forbidden_read)
    with pytest.raises(
        ValueError,
        match="IMPORT_HTTP_ADAPTER_ONLY_SUPPORTS_DEVELOPMENT_LABELS",
    ):
        http_import._load_selected_cases_and_labels(spec, blind_run["repo"])
    with pytest.raises(
        ValueError,
        match="BUILDER_HTTP_ADAPTER_ONLY_SUPPORTS_DEVELOPMENT_LABELS",
    ):
        http_builder._load_selected_builder_cases_and_labels(spec, blind_run["repo"])
    assert reads == []
