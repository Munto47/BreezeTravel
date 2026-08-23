from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.gate_v2 import P5GateErrorV2, build_p5_gate_manifest_v2


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def _hashed(value: dict, field: str) -> dict:
    value[field] = digest(value)
    return value


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    repo = tmp_path / "repo"
    p5 = repo / "backend" / "evals" / "trip_check_v1" / "p5"
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
        },
        "manifest_hash",
    )
    _write(p5 / "dataset_v2.manifest.json", dataset)
    for name in (
        "cases_nonblind_v2.jsonl",
        "materializations_nonblind_v2.jsonl",
        "frozen_blind.v2.inputs.jsonl",
        "frozen_blind.v2.materializations.jsonl",
    ):
        (p5 / name).write_text("{}\n", encoding="utf-8")
    p4 = {
        "status": "PASS",
        "p4_phase_status": "PASS",
        "subject_commit": "9" * 40,
        "solver_admission": {"status": "REJECT", "default_strategy": "bounded_repair_v1"},
    }
    p4_path = repo / "backend" / "evidence" / "trip_check_v1" / "p4" / "p4_gate_manifest.json"
    _write(p4_path, p4)
    run_manifests = {}
    paths: dict[str, Path] = {"repo": repo}
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
        },
        "report_hash",
    )
    paths["nonblind_score"] = tmp_path / "nonblind-score.json"
    _write(paths["nonblind_score"], nonblind_score)
    aggregate = {
        "case_count": 90,
        "mean_score": 100.0,
        "deterministic_failure_count": 0,
        "wrong_city_or_poi_count": 0,
        "hard_finding_miss_count": 0,
        "unknown_failure_count": 0,
        "candidate_receipt_failure_count": 0,
        "concurrency_failure_count": 0,
        "postcheck_failure_count": 0,
        "replay_failure_count": 0,
    }
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
            "variant_metrics": {"legacy_a": {}, "core_b": {}, "solver_c": {}},
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
    _write(p4_path, p4)
    with pytest.raises(P5GateErrorV2, match="P4 solver admission"):
        _build(paths)
