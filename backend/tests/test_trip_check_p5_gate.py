from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.gate import P5GateError, build_p5_gate_manifest


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run_dir(root: Path, lane: str, cases: int, terminals: int) -> tuple[Path, dict]:
    run_dir = root / f"{lane}-run"
    run_dir.mkdir(parents=True)
    terminal_path = run_dir / "terminal_outputs.jsonl"
    terminal_path.write_text('{"sealed":true}\n', encoding="utf-8")
    manifest = {
        "schema_version": "trip-check-p5-run-group-v1",
        "status": "PASS",
        "formal_evidence": True,
        "executable_evidence_status": "PASS",
        "lane": lane,
        "subject_commit": "a" * 40,
        "dirty_tree": False,
        "case_count": cases,
        "variant_count": 3,
        "terminal_count": terminals,
        "expected_terminal_count": terminals,
        "replay_executed": True,
        "replay_match_count": terminals,
        "replay_mismatches": [],
        "external_api_calls": 0,
        "human_evidence": False,
        "terminal_outputs_path": terminal_path.name,
        "terminal_outputs_file_sha256": _sha(terminal_path),
    }
    manifest["manifest_hash"] = digest(manifest)
    _write_json(run_dir / "run_group_manifest.json", manifest)
    return run_dir, manifest


def _fixture(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "repo"
    dataset = {
        "schema_version": "trip-check-p5-dataset-manifest-v1",
        "counts": {
            "total": 360,
            "by_split": {"pilot": 18, "dev": 180, "regression": 72, "frozen_blind": 90},
            "by_city": {"北京": 120, "上海": 120, "杭州": 120},
        },
    }
    dataset["manifest_hash"] = digest(dataset)
    dataset_path = repo / "backend" / "evals" / "trip_check_v1" / "p5" / "dataset_v1.manifest.json"
    _write_json(dataset_path, dataset)
    p4_gate = {
        "schema_version": "trip-check-p4-gate-manifest-v1",
        "subject_commit": "b" * 40,
        "status": "PASS",
        "p4_phase_status": "PASS",
        "solver_admission": {
            "status": "REJECT",
            "default_strategy": "bounded_repair_v1",
        },
    }
    p4_path = repo / "backend" / "evidence" / "trip_check_v1" / "p4" / "p4_gate_manifest.json"
    _write_json(p4_path, p4_gate)
    nonblind_run_dir, nonblind_run = _run_dir(tmp_path, "nonblind", 270, 810)
    blind_run_dir, blind_run = _run_dir(tmp_path, "frozen_blind", 90, 270)
    nonblind_score = {
        "schema_version": "trip-check-p5-nonblind-score-report-v1",
        "status": "PASS",
        "subject_commit": "a" * 40,
        "run_group_manifest_hash": nonblind_run["manifest_hash"],
        "case_count": 270,
        "terminal_count": 810,
    }
    nonblind_score["report_hash"] = digest(nonblind_score)
    nonblind_score_path = tmp_path / "nonblind_score.json"
    _write_json(nonblind_score_path, nonblind_score)
    blind_score = {
        "schema_version": "trip-check-p5-isolated-blind-score-v1",
        "status": "PASS",
        "case_count": 90,
        "terminal_count": 270,
        "human_evidence": False,
        "bindings": {
            "subject_commit": "a" * 40,
            "run_group_manifest_hash": blind_run["manifest_hash"],
        },
    }
    blind_score_path = tmp_path / "blind_score.json"
    _write_json(blind_score_path, blind_score)
    judge_panel = {
        "schema_version": "trip-check-p5-judge-panel-v1",
        "status": "PASS",
        "run_group_manifest_hash": blind_run["manifest_hash"],
        "round_count": 3,
        "candidate_count": 270,
        "human_calibration_performed": False,
        "judge_may_override_deterministic_failure": False,
    }
    judge_path = tmp_path / "judge_panel.json"
    _write_json(judge_path, judge_panel)
    return {
        "repo": repo,
        "p4": p4_path,
        "nonblind_run": nonblind_run_dir,
        "blind_run": blind_run_dir,
        "nonblind_score": nonblind_score_path,
        "blind_score": blind_score_path,
        "judge": judge_path,
        "output": tmp_path / "p5_gate_manifest.json",
    }


def _build(paths: dict[str, Path]) -> dict:
    return build_p5_gate_manifest(
        repo_root=paths["repo"],
        nonblind_run_dir=paths["nonblind_run"],
        nonblind_score_path=paths["nonblind_score"],
        blind_run_dir=paths["blind_run"],
        blind_score_path=paths["blind_score"],
        judge_panel_path=paths["judge"],
        output_path=paths["output"],
        require_current_subject=False,
    )


def test_gate_binds_1080_outputs_and_keeps_cp_sat_rejected(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    manifest = _build(paths)
    assert manifest["status"] == "PASS"
    assert manifest["counts"]["total_terminal_outputs"] == 1080
    assert manifest["promotion_decision"] == "KEEP_CORE_B"
    assert manifest["solver_admission"]["status"] == "REJECT"
    assert manifest["solver_admission"]["may_be_overridden_by_p5_score"] is False
    assert manifest["evidence_boundaries"]["live_provider"] == "NOT_RUN"
    assert manifest["evidence_boundaries"]["human_evidence"] == "NOT_RUN"


def test_deterministic_failure_precedes_judge_and_rejects_all(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    blind_score = json.loads(paths["blind_score"].read_text(encoding="utf-8"))
    blind_score["status"] = "REJECT"
    _write_json(paths["blind_score"], blind_score)
    manifest = _build(paths)
    assert manifest["status"] == "REJECT"
    assert manifest["checks"]["judge_semantic_gate"] is True
    assert manifest["checks"]["blind_deterministic_gate"] is False
    assert manifest["promotion_decision"] == "REJECT_ALL_CANDIDATES"


def test_gate_rejects_any_attempt_to_change_p4_solver_admission(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    p4 = json.loads(paths["p4"].read_text(encoding="utf-8"))
    p4["solver_admission"]["status"] = "PASS"
    _write_json(paths["p4"], p4)
    with pytest.raises(P5GateError, match="P4 solver admission"):
        _build(paths)
