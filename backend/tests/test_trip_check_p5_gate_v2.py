from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.gate_v2 import P5GateErrorV2, build_p5_gate_manifest_v2
from evals.trip_check_v1.p5.final_blind_scorer_v2 import (
    SCHEMA_CONTRACT_PATHS_V2,
    schema_contract_sha256_v2,
)


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hashed(value: dict, field: str) -> dict:
    value[field] = digest(value)
    return value


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    repo = tmp_path / "repo"
    p5 = repo / "backend" / "evals" / "trip_check_v1" / "p5"
    for relative in SCHEMA_CONTRACT_PATHS_V2:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    run_spec = p5 / "run_spec_template_v2.json"
    rubric = p5 / "judge_rubric_v2.json"
    run_spec.write_text('{"schema_version":"trip-check-p5-run-spec-v2"}\n', encoding="utf-8")
    rubric.write_text('{"schema_version":"trip-check-p5-judge-rubric-v2"}\n', encoding="utf-8")
    formal_schema_source = (
        Path(__file__).resolve().parents[1]
        / "evals"
        / "trip_check_v1"
        / "p5"
        / "dataset_formal_validation_receipt_v2.schema.json"
    )
    formal_schema = p5 / "dataset_formal_validation_receipt_v2.schema.json"
    formal_schema.write_bytes(formal_schema_source.read_bytes())
    validator_path = repo / "backend" / "scripts" / "validate_trip_check_p5_dataset_v2.py"
    validator_path.parent.mkdir(parents=True, exist_ok=True)
    validator_path.write_text("# frozen formal validator\n", encoding="utf-8")
    dataset_files = {
        "nonblind_cases": ("cases_nonblind_v2.jsonl", 270, "7" * 64),
        "nonblind_materializations": (
            "materializations_nonblind_v2.jsonl",
            270,
            "8" * 64,
        ),
        "blind_cases": ("frozen_blind.v2.inputs.jsonl", 90, "9" * 64),
        "blind_materializations": (
            "frozen_blind.v2.materializations.jsonl",
            90,
            "a" * 64,
        ),
    }
    file_bindings = {}
    for key, (name, row_count, content_sha256) in dataset_files.items():
        path = p5 / name
        path.write_text("{}\n", encoding="utf-8")
        file_bindings[key] = {
            "path": f"evals/trip_check_v1/p5/{name}",
            "file_sha256": _sha(path),
            "content_sha256": content_sha256,
            "row_count": row_count,
        }
    seal = {
        "schema_version": "trip-check-p5-blind-seal-v2",
        "schema_contract_sha256": schema_contract_sha256_v2(repo),
        "run_spec_template_sha256": _sha(run_spec),
        "rubric_sha256": _sha(rubric),
        "external_bundle_sha256": "4" * 64,
        "labels_canonical_sha256": "5" * 64,
        "review_receipt_sha256": "6" * 64,
    }
    seal_path = p5 / "sealed" / "frozen_blind.v2.seal.json"
    _write(seal_path, seal)
    seal_sha = _sha(seal_path)
    dataset = _hashed(
        {
            "schema_version": "trip-check-p5-dataset-manifest-v2",
            "frozen": True,
            "generation": {"formal_validation_eligible": True, "ocr_mode": "actual"},
            "counts": {
                "total": 360,
                "by_split": {
                    "pilot": 18,
                    "dev": 180,
                    "regression": 72,
                    "frozen_blind": 90,
                },
                "by_city": {"北京": 120, "上海": 120, "杭州": 120},
            },
            "files": file_bindings,
            "contract_hashes": {
                "run_spec_template_sha256": _sha(run_spec),
                "judge_rubric_sha256": _sha(rubric),
            },
            "sealing_commitment": {
                "status": "SEALED",
                "blind_seal_v2_sha256": seal_sha,
                "external_bundle_sha256": seal["external_bundle_sha256"],
                "labels_canonical_sha256": seal["labels_canonical_sha256"],
                "review_receipt_sha256": seal["review_receipt_sha256"],
            },
        },
        "manifest_hash",
    )
    _write(p5 / "dataset_v2.manifest.json", dataset)
    active = {
        "active_contract": "trip-check-p5-v2",
        "formal_evidence_status": "READY",
        "dataset_manifest_hash": dataset["manifest_hash"],
        "blind_seal_v2_sha256": seal_sha,
    }
    _write(p5 / "active_contract.json", active)
    commitment_chain = {
        "active_contract_file_sha256": _sha(p5 / "active_contract.json"),
        "blind_seal_sha256": seal_sha,
        "external_bundle_sha256": seal["external_bundle_sha256"],
        "labels_canonical_sha256": seal["labels_canonical_sha256"],
        "review_receipt_sha256": seal["review_receipt_sha256"],
    }
    formal_receipt = _hashed({
        "schema_version": "trip-check-p5-dataset-validation-v2",
        "status": "PASS",
        "formal": True,
        "subject_commit": "a" * 40,
        "errors": [],
        "manifest_hash": dataset["manifest_hash"],
        "dataset_manifest": {
            "path": "backend/evals/trip_check_v1/p5/dataset_v2.manifest.json",
            "file_sha256": _sha(p5 / "dataset_v2.manifest.json"),
            "manifest_hash": dataset["manifest_hash"],
        },
        "dataset_files": {
            key: {
                "path": f"backend/{entry['path']}",
                "file_sha256": entry["file_sha256"],
                "content_sha256": entry["content_sha256"],
                "row_count": entry["row_count"],
            }
            for key, entry in file_bindings.items()
        },
        "validator": {
            "path": "backend/scripts/validate_trip_check_p5_dataset_v2.py",
            "code_sha256": _sha(validator_path),
        },
        "counts": {
            "total": 360,
            "screenshots": 171,
            "by_split": {"dev": 180, "frozen_blind": 90, "pilot": 18, "regression": 72},
            "by_city": {"上海": 120, "北京": 120, "杭州": 120},
            "screenshots_by_split": {
                "pilot": 0,
                "dev": 90,
                "regression": 36,
                "frozen_blind": 45,
            },
        },
        "created_at": "2026-08-23T12:00:00Z",
    }, "receipt_hash")
    formal_receipt_path = tmp_path / "formal-receipt.json"
    _write(formal_receipt_path, formal_receipt)
    p4 = _hashed({
        "status": "PASS",
        "p4_phase_status": "PASS",
        "subject_commit": "85368777ca8d2d4e77cf053fc9a74018f9f9fc9a",
        "solver_admission": {"status": "REJECT", "default_strategy": "bounded_repair_v1"},
    }, "manifest_hash")
    p4_path = repo / "backend" / "evidence" / "trip_check_v1" / "p4" / "p4_gate_manifest.json"
    _write(p4_path, p4)
    run_manifests = {}
    paths: dict[str, Path] = {"repo": repo, "formal_receipt": formal_receipt_path}
    for lane, count in (("nonblind", 810), ("frozen_blind", 270)):
        run_dir = tmp_path / f"{lane}-run"
        run_dir.mkdir()
        terminal = run_dir / "terminal_outputs.jsonl"
        terminal.write_text("{}\n", encoding="utf-8")
        manifest = {
            "subject_commit": "a" * 40,
            "manifest_hash": ("b" if lane == "nonblind" else "c") * 64,
            "terminal_outputs_path": terminal.name,
            "terminal_outputs_file_sha256": ("d" if lane == "nonblind" else "e") * 64,
            "terminal_outputs_content_sha256": ("f" if lane == "nonblind" else "1") * 64,
            **(commitment_chain if lane == "frozen_blind" else {}),
        }
        run_manifests[lane] = manifest
        _write(run_dir / "run_group_manifest.json", manifest)
        paths[f"{lane}_run"] = run_dir

    def fake_validate(**kwargs):
        lane = kwargs["expected_lane"]
        count = 810 if lane == "nonblind" else 270
        return run_manifests[lane], [], [SimpleNamespace()] * count

    monkeypatch.setattr(
        "evals.trip_check_v1.p5.gate_v2.validate_run_group_v2", fake_validate
    )
    aggregate = {
        "case_count": 90,
        "task_success_count": 90,
        "mean_score": 100.0,
        "deterministic_failure_count": 0,
        "wrong_city_or_poi_count": 0,
        "hard_finding_miss_count": 0,
        "unknown_failure_count": 0,
        "candidate_receipt_failure_count": 0,
        "concurrency_failure_count": 0,
        "postcheck_failure_count": 0,
        "replay_failure_count": 0,
        "nonpass_finding_count": 90,
        "covered_nonpass_finding_count": 90,
        "nonpass_finding_advice_coverage_rate": 1.0,
        "unsupported_claim_count": 0,
        "unsupported_claim_rate": 0.0,
        "usage_measurement_failure_count": 0,
        "token_count_total": 0,
        "cost_usd_total": 0,
        "quality_dimensions": {
            "location_city_facts": {"case_count": 30, "mean_score": 100.0},
            "time_route_hotel_continuity": {"case_count": 30, "mean_score": 100.0},
            "other_advice": {
                "case_count": 30,
                "minimum_bucket_score": 100.0,
                "buckets": {"OTHER_ADVICE": {"case_count": 30, "mean_score": 100.0}},
            },
        },
    }
    nonblind_score = _hashed(
        {
            "schema_version": "trip-check-p5-nonblind-score-report-v2",
            "status": "PASS",
            "subject_commit": "a" * 40,
            "dataset_manifest_hash": dataset["manifest_hash"],
            "run_group_manifest_hash": run_manifests["nonblind"]["manifest_hash"],
            "case_count": 270,
            "terminal_count": 810,
            "solver_admission_inherited": "REJECT",
            "solver_may_promote_from_p5_score": False,
            "live_provider_evidence": False,
            "public_e2e_evidence": False,
            "human_evidence": False,
            "variant_metrics": {
                "core_b": {
                    "overall": {**aggregate, "case_count": 270, "task_success_count": 270},
                    "by_split": {
                        "pilot": {"case_count": 18, "task_success_count": 18},
                        "dev": {"case_count": 180, "task_success_count": 180},
                        "regression": {"case_count": 72, "task_success_count": 72},
                    },
                }
            },
        },
        "report_hash",
    )
    paths["nonblind_score"] = tmp_path / "nonblind-score.json"
    _write(paths["nonblind_score"], nonblind_score)
    variant_metrics = {
        variant_id: {
            "overall": aggregate,
            "by_city": {},
            "by_input_kind": {},
            "by_difficulty": {},
            "by_fault_profile": {},
            "by_finding": {},
            "by_repair_outcome": {},
        }
        for variant_id in ("legacy_a", "core_b", "solver_c")
    }
    blind_score = {
        "schema_version": "trip-check-p5-isolated-blind-score-v2",
        "status": "PASS",
        "decision": "ACCEPT_BLIND_SCORE",
        "evidence_class": "CONTROLLED_BLIND_ORACLE",
        "truth_provenance": "external_controlled_blind_oracle",
        "case_count": 90,
        "terminal_count": 270,
        "minimum_bucket_size": 5,
        "human_evidence": False,
        "live_provider_evidence": False,
        "public_e2e_evidence": False,
        **commitment_chain,
        "bindings": {
            "subject_commit": "a" * 40,
            "dataset_manifest_hash": dataset["manifest_hash"],
            "run_group_manifest_hash": run_manifests["frozen_blind"]["manifest_hash"],
            "terminal_outputs_file_sha256": run_manifests["frozen_blind"][
                "terminal_outputs_file_sha256"
            ],
        },
        "variant_metrics": variant_metrics,
        "core_gate_checks": {
            "mean_score_gte_88": True,
            "deterministic_failure_zero": True,
            "wrong_city_or_poi_zero": True,
            "hard_finding_miss_zero": True,
            "unknown_failure_zero": True,
            "candidate_receipt_failure_zero": True,
            "concurrency_failure_zero": True,
            "postcheck_failure_zero": True,
            "replay_failure_zero": True,
        },
        "automated_proxy_judge": "NOT_RUN",
    }
    paths["blind_score"] = tmp_path / "blind-score.json"
    _write(paths["blind_score"], blind_score)
    judge = _hashed(
        {
            "schema_version": "trip-check-p5-judge-panel-v2",
            "status": "PASS",
            "evidence_class": "automated_proxy_judge",
            "run_group_manifest_hash": run_manifests["frozen_blind"]["manifest_hash"],
            "terminal_outputs_content_sha256": run_manifests["frozen_blind"][
                "terminal_outputs_content_sha256"
            ],
            "round_count": 3,
            "candidate_count": 270,
            "agreement_threshold": 0.85,
            "verdict_agreement_rate": 1.0,
            "per_dimension_agreement_rate": {
                "clarity": 1.0,
                "actionability": 1.0,
                "evidence_boundary_expression": 1.0,
            },
            "human_calibration_performed": False,
            "deterministic_scorer_priority": True,
            "judge_may_override_deterministic_failure": False,
            "variant_metrics": {
                "legacy_a": {"majority_pass_rate": 1.0},
                "core_b": {"majority_pass_rate": 1.0},
                "solver_c": {"majority_pass_rate": 1.0},
            },
            "unsupported_claim_candidate_count": 0,
            "provenance": [
                {
                    "round_index": index,
                    "evaluator_id": f"evaluator-{index}",
                    "agent_task_id": f"task-{index}",
                    "agent_id": f"agent-{index}",
                    "model_id": "gpt-5.6-sol",
                    "bundle_sha256": str(index) * 64,
                    "rubric_sha256": "4" * 64,
                    "terminal_outputs_content_sha256": "1" * 64,
                    "api_usage_count": 0,
                    "tool_usage_count": 0,
                }
                for index in range(1, 4)
            ],
            "mapping_sha256": "2" * 64,
        },
        "report_hash",
    )
    paths["judge"] = tmp_path / "judge.json"
    _write(paths["judge"], judge)
    paths["output"] = tmp_path / "gate.json"
    return paths


def _build(paths: dict[str, Path]) -> dict:
    return build_p5_gate_manifest_v2(
        repo_root=paths["repo"],
        nonblind_run_dir=paths["nonblind_run"],
        nonblind_score_path=paths["nonblind_score"],
        blind_run_dir=paths["frozen_blind_run"],
        blind_score_path=paths["blind_score"],
        judge_panel_path=paths["judge"],
        formal_validation_receipt_path=paths["formal_receipt"],
        output_path=paths["output"],
        require_current_subject=False,
    )


def test_gate_binds_1080_and_keeps_all_external_evidence_not_run(tmp_path, monkeypatch) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    gate = _build(paths)
    assert gate["status"] == "PASS"
    assert gate["counts"]["total_terminal_outputs"] == 1080
    assert gate["solver_admission"]["status"] == "REJECT"
    assert gate["solver_admission"]["may_be_overridden_by_p5_score"] is False
    assert gate["evidence_boundaries"]["live_provider"] == "NOT_RUN"
    assert gate["evidence_boundaries"]["public_e2e"] == "NOT_RUN"
    assert gate["evidence_boundaries"]["human_evidence"] == "NOT_RUN"
    assert gate["evidence_boundaries"]["p6_candidate_gate"] == "REJECT"
    receipt_artifact = next(
        item
        for item in gate["artifact_index"]
        if item["logical_name"] == "formal_dataset_validation_receipt"
    )
    assert receipt_artifact["storage"] == "external"
    assert receipt_artifact["sha256"] == _sha(paths["formal_receipt"])
    assert {
        "active_contract",
        "blind_seal_v2",
        "schema_contract_commitment",
        "run_spec_template_v2",
        "judge_rubric_v2",
        "external_blind_bundle_commitment",
        "external_blind_review_receipt_commitment",
        "formal_dataset_validation_receipt_schema",
        "formal_dataset_validation_receipt",
    } <= {item["logical_name"] for item in gate["artifact_index"]}


def test_deterministic_failure_has_priority_over_passing_judge(tmp_path, monkeypatch) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    score = json.loads(paths["blind_score"].read_text(encoding="utf-8"))
    score["status"] = "REJECT"
    score["decision"] = "REJECT"
    score["variant_metrics"]["core_b"]["overall"]["unknown_failure_count"] = 1
    score["core_gate_checks"]["unknown_failure_zero"] = False
    _write(paths["blind_score"], score)
    gate = _build(paths)
    assert gate["status"] == "REJECT"
    assert gate["checks"]["judge_semantic_gate"] is True
    assert gate["checks"]["blind_deterministic_gate"] is False
    assert gate["failure_priority"][0] == "NONBLIND_DETERMINISTIC"


def test_gate_rejects_p4_solver_promotion(tmp_path, monkeypatch) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    p4_path = paths["repo"] / "backend/evidence/trip_check_v1/p4/p4_gate_manifest.json"
    p4 = json.loads(p4_path.read_text(encoding="utf-8"))
    p4["solver_admission"]["status"] = "PASS"
    p4.pop("manifest_hash")
    p4 = _hashed(p4, "manifest_hash")
    _write(p4_path, p4)
    with pytest.raises(P5GateErrorV2, match="P4 solver admission"):
        _build(paths)


def test_gate_rejects_missing_acceptance_dimension_evidence(tmp_path, monkeypatch) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    score = json.loads(paths["nonblind_score"].read_text(encoding="utf-8"))
    del score["variant_metrics"]["core_b"]["overall"]["quality_dimensions"]
    score.pop("report_hash")
    score = _hashed(score, "report_hash")
    _write(paths["nonblind_score"], score)
    with pytest.raises(P5GateErrorV2, match="quality dimension evidence missing"):
        _build(paths)


@pytest.mark.parametrize(
    ("path_key", "metric", "value", "check"),
    [
        ("nonblind_score", "mean_score", 87.99, "nonblind_core_overall_score_gte_88"),
        (
            "blind_score",
            "unsupported_claim_count",
            1,
            "blind_core_unsupported_claim_rate_zero",
        ),
    ],
)
def test_gate_rejects_acceptance_c_threshold_failure(
    tmp_path,
    monkeypatch,
    path_key,
    metric,
    value,
    check,
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    score = json.loads(paths[path_key].read_text(encoding="utf-8"))
    score["variant_metrics"]["core_b"]["overall"][metric] = value
    if metric == "unsupported_claim_count":
        score["variant_metrics"]["core_b"]["overall"]["unsupported_claim_rate"] = 1 / 90
    if path_key == "nonblind_score":
        score.pop("report_hash")
        score = _hashed(score, "report_hash")
    _write(paths[path_key], score)
    gate = _build(paths)
    assert gate["status"] == "REJECT"
    assert gate["checks"][check] is False


def test_gate_rejects_commitment_chain_drift(tmp_path, monkeypatch) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    score = json.loads(paths["blind_score"].read_text(encoding="utf-8"))
    score["review_receipt_sha256"] = "0" * 64
    _write(paths["blind_score"], score)
    with pytest.raises(P5GateErrorV2, match="blind aggregate commitment chain mismatch"):
        _build(paths)


def test_gate_rejects_p4_subject_and_self_hash_tampering(tmp_path, monkeypatch) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    p4_path = paths["repo"] / "backend/evidence/trip_check_v1/p4/p4_gate_manifest.json"
    p4 = json.loads(p4_path.read_text(encoding="utf-8"))
    p4["subject_commit"] = "0" * 40
    _write(p4_path, p4)
    with pytest.raises(P5GateErrorV2, match="P4 gate hash mismatch"):
        _build(paths)

    p4.pop("manifest_hash")
    p4 = _hashed(p4, "manifest_hash")
    _write(p4_path, p4)
    with pytest.raises(P5GateErrorV2, match="P4 solver admission inheritance"):
        _build(paths)


def _mutate_receipt(paths: dict[str, Path], mutation, *, rehash: bool = True) -> None:
    receipt = json.loads(paths["formal_receipt"].read_text(encoding="utf-8"))
    mutation(receipt)
    if rehash:
        receipt.pop("receipt_hash", None)
        receipt = _hashed(receipt, "receipt_hash")
    _write(paths["formal_receipt"], receipt)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("validator"),
        lambda value: value.__setitem__("unexpected", True),
        lambda value: value["counts"].__setitem__("total", 359),
        lambda value: value.__setitem__("errors", ["controlled failure"]),
        lambda value: value.__setitem__("formal", False),
        lambda value: value.__setitem__("status", "FAIL"),
    ],
)
def test_gate_rejects_formal_receipt_schema_drift(tmp_path, monkeypatch, mutation) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    _mutate_receipt(paths, mutation)
    with pytest.raises(P5GateErrorV2, match="receipt schema rejected"):
        _build(paths)


def test_gate_rejects_formal_receipt_bad_self_hash(tmp_path, monkeypatch) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    _mutate_receipt(
        paths,
        lambda value: value.__setitem__("receipt_hash", "0" * 64),
        rehash=False,
    )
    with pytest.raises(P5GateErrorV2, match="receipt hash mismatch"):
        _build(paths)


def test_gate_rejects_formal_receipt_wrong_run_subject(tmp_path, monkeypatch) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    _mutate_receipt(paths, lambda value: value.__setitem__("subject_commit", "0" * 40))
    with pytest.raises(P5GateErrorV2, match="receipt binding rejected"):
        _build(paths)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value.__setitem__("manifest_hash", "0" * 64),
            "receipt binding rejected",
        ),
        (
            lambda value: value["dataset_manifest"].__setitem__("file_sha256", "0" * 64),
            "dataset manifest binding rejected",
        ),
        (
            lambda value: value["validator"].__setitem__("code_sha256", "0" * 64),
            "dataset validator binding rejected",
        ),
    ],
)
def test_gate_rejects_formal_receipt_contract_binding_drift(
    tmp_path,
    monkeypatch,
    mutation,
    message,
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    _mutate_receipt(paths, mutation)
    with pytest.raises(P5GateErrorV2, match=message):
        _build(paths)


@pytest.mark.parametrize(
    ("dataset_key", "field", "value"),
    [
        (
            "nonblind_cases",
            "path",
            "backend/evals/trip_check_v1/p5/materializations_nonblind_v2.jsonl",
        ),
        ("nonblind_materializations", "file_sha256", "0" * 64),
        ("blind_cases", "content_sha256", "0" * 64),
        ("blind_materializations", "row_count", 89),
    ],
)
def test_gate_rejects_each_formal_dataset_file_binding_drift(
    tmp_path,
    monkeypatch,
    dataset_key,
    field,
    value,
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    _mutate_receipt(
        paths,
        lambda receipt: receipt["dataset_files"][dataset_key].__setitem__(field, value),
    )
    with pytest.raises(P5GateErrorV2, match="dataset file binding rejected"):
        _build(paths)


def test_gate_rejects_non_external_formal_receipt_paths(tmp_path, monkeypatch) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    inside = paths["repo"] / "formal-receipt.json"
    inside.write_bytes(paths["formal_receipt"].read_bytes())
    paths["formal_receipt"] = inside
    with pytest.raises(P5GateErrorV2, match="outside the repository"):
        _build(paths)

    paths = _fixture(tmp_path / "relative", monkeypatch)
    paths["formal_receipt"] = Path("formal-receipt.json")
    with pytest.raises(P5GateErrorV2, match="path must be absolute"):
        _build(paths)


def test_gate_cli_requires_formal_validation_receipt() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_trip_check_p5_v2_gate",
            "--nonblind-run-dir",
            "missing",
            "--nonblind-score",
            "missing",
            "--blind-run-dir",
            "missing",
            "--blind-score",
            "missing",
            "--judge-panel",
            "missing",
        ],
        cwd=backend_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode != 0
    assert "--formal-validation-receipt" in completed.stderr
