from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.gate_v4 import (
    P5GateErrorV4,
    build_p5_gate_manifest_v4,
    parse_nonblind_score_v4,
)


SUBJECT = "a" * 40
UPSTREAM = "origin/codex/p5-v4"
HEX64 = "b" * 64


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_hashed(path: Path, value: dict, field: str) -> dict:
    payload = dict(value)
    payload[field] = digest(payload)
    _write_json(path, payload)
    return payload


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_manifest(lane: str, dataset_hash: str, run_spec_hash: str) -> dict:
    cases, terminals = (270, 810) if lane == "nonblind" else (90, 270)
    return {
        "schema_version": "trip-check-p5-run-group-v4",
        "status": "PASS",
        "formal_evidence": True,
        "lane": lane,
        "subject_commit": SUBJECT,
        "upstream_ref": UPSTREAM,
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
        "dataset_manifest_hash": dataset_hash,
        "artifact_index_hash": ("c" if lane == "nonblind" else "d") * 64,
        "terminal_outputs_file_sha256": "e" * 64,
        "terminal_outputs_content_sha256": ("f" if lane == "nonblind" else "1")
        * 64,
        "run_spec_template_sha256": run_spec_hash,
        "case_count": cases,
        "terminal_count": terminals,
        "replay_executed": True,
        "replay_match_count": terminals,
        "replay_readback_count": terminals,
        "replay_mismatches": [],
        "blind_labels_read": False,
    }


def _nonblind_score(run: dict, decision: str = "KEEP_CORE_B") -> dict:
    payload = {
        "schema_version": "trip-check-p5-nonblind-score-report-v4",
        "status": "PASS",
        "subject_commit": SUBJECT,
        "dataset_manifest_hash": run["dataset_manifest_hash"],
        "run_group_manifest_hash": run["manifest_hash"],
        "artifact_index_hash": run["artifact_index_hash"],
        "case_count": 270,
        "terminal_count": 810,
        "replay_readback_count": 810,
        "variant_metrics": {key: {"overall": {"case_count": 270}} for key in ("legacy_a", "core_b", "solver_c")},
        "paired_comparisons": {"legacy_a": {}, "solver_c": {}},
        "zero_tolerance_checks": {"deterministic_failure_zero": True},
        "stage_gate_checks": {"pilot_18_of_18": True},
        "promotion_decision": decision,
        "solver_admission_inherited": "REJECT",
        "solver_may_promote_from_p5_score": False,
        "evidence_boundary": {"automated_proxy_judge": "NOT_RUN"},
    }
    return {**payload, "report_hash": digest(payload)}


def _blind_score(run: dict, seal_sha: str) -> dict:
    payload = {
        "schema_version": "trip-check-p5-isolated-blind-score-v4",
        "status": "PASS",
        "bindings": {
            "subject_commit": SUBJECT,
            "dataset_manifest_hash": run["dataset_manifest_hash"],
            "run_group_manifest_hash": run["manifest_hash"],
            "terminal_outputs_file_sha256": run["terminal_outputs_file_sha256"],
            "terminal_outputs_content_sha256": run[
                "terminal_outputs_content_sha256"
            ],
            "artifact_index_hash": run["artifact_index_hash"],
            "blind_seal_sha256": seal_sha,
            "run_spec_template_sha256": run["run_spec_template_sha256"],
        },
        "case_count": 90,
        "terminal_count": 270,
        "replay_readback_count": 270,
        "variant_metrics": {key: {"overall": {"case_count": 90}} for key in ("legacy_a", "core_b", "solver_c")},
        "zero_tolerance_checks": {"deterministic_failure_zero": True},
        "human_calibration_performed": False,
        "human_evidence": False,
        "live_provider_evidence": False,
        "public_e2e_evidence": False,
    }
    return {**payload, "report_hash": digest(payload)}


def _judge_panel(run: dict) -> dict:
    payload = {
        "schema_version": "trip-check-p5-judge-panel-v4",
        "status": "PASS",
        "evidence_class": "automated_proxy_judge",
        "automated_proxy_judge": True,
        "human_calibration_performed": False,
        "round_count": 3,
        "candidate_count": 270,
        "agreement_threshold": 0.85,
        "verdict_agreement_rate": 1.0,
        "per_dimension_agreement_rate": {
            "clarity": 1.0,
            "actionability": 1.0,
            "evidence_boundary_expression": 1.0,
        },
        "variant_metrics": {key: {} for key in ("legacy_a", "core_b", "solver_c")},
        "provenance": [
            {
                "round_index": index,
                "evaluator_id": f"e-{index}",
                "agent_task_id": f"t-{index}",
                "agent_id": f"a-{index}",
                "context_id": f"c-{index}",
            }
            for index in range(1, 4)
        ],
        "mapping_sha256": "2" * 64,
        "subject_commit": SUBJECT,
        "dataset_manifest_hash": run["dataset_manifest_hash"],
        "run_group_manifest_hash": run["manifest_hash"],
        "artifact_index_hash": run["artifact_index_hash"],
        "terminal_outputs_content_sha256": run[
            "terminal_outputs_content_sha256"
        ],
        "deterministic_scorer_priority": True,
        "judge_may_override_deterministic_failure": False,
        "unsupported_claim_candidate_count": 0,
    }
    return {**payload, "report_hash": digest(payload)}


def _formal_fixture(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "repo"
    p5 = root / "backend" / "evals" / "trip_check_v1" / "p5"
    p5.mkdir(parents=True)
    run_spec = p5 / "run_spec_template_v3.json"
    rubric = p5 / "judge_rubric_v2.json"
    contracts = p5 / "contracts_v3.py"
    _write_json(run_spec, {"schema_version": "trip-check-p5-run-spec-v3"})
    _write_json(rubric, {"schema_version": "trip-check-p5-judge-rubric-v2"})
    contracts.write_text("# contracts v3\n", encoding="utf-8")
    dataset_files = {}
    for key, count in {
        "nonblind_cases": 270,
        "blind_cases": 90,
        "nonblind_materializations": 270,
        "blind_materializations": 90,
    }.items():
        path = p5 / f"{key}.v4.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        dataset_files[key] = {
            "path": f"evals/trip_check_v1/p5/{path.name}",
            "row_count": count,
            "file_sha256": _file_sha(path),
            "content_sha256": HEX64,
        }
    seal_path = p5 / "sealed" / "frozen_blind.v4.seal.json"
    candidate_dataset_hash = "4" * 64
    _write_json(
        seal_path,
        {
            "schema_version": "trip-check-p5-blind-seal-v4",
            "candidate_dataset_manifest_hash": candidate_dataset_hash,
            "candidate_freeze_commit": "3" * 40,
            "case_count": 90,
            "split": "frozen_blind",
            "scoring_payload_present": False,
            "human_evidence": False,
            "run_spec_template_sha256": _file_sha(run_spec),
            "rubric_sha256": _file_sha(rubric),
            "contracts_v3_sha256": _file_sha(contracts),
            "dataset_contracts_v4_sha256": "5" * 64,
        },
    )
    dataset_path = p5 / "dataset_v4.manifest.json"
    dataset = _write_hashed(
        dataset_path,
        {
            "schema_version": "trip-check-p5-dataset-manifest-v4",
            "dataset_id": "trip-check-p5-360-v4",
            "counts": {
                "total": 360,
                "by_split": {
                    "pilot": 18,
                    "dev": 180,
                    "regression": 72,
                    "frozen_blind": 90,
                },
            },
            "files": dataset_files,
            "lanes": {
                "nonblind": {"case_count": 270},
                "frozen_blind": {"case_count": 90},
            },
            "contract_hashes": {
                "run_spec_template_sha256": _file_sha(run_spec),
                "judge_rubric_sha256": _file_sha(rubric),
                "judge_rubric_semantics_changed": False,
                "contracts_v3_sha256": _file_sha(contracts),
                "dataset_contracts_v4_sha256": "5" * 64,
            },
            "frozen": True,
            "formal_validation_eligible": True,
            "seal_status": "SEALED",
            "sealing_commitment": {
                "status": "SEALED",
                "blind_seal_file_sha256": _file_sha(seal_path),
                "candidate_dataset_manifest_hash": candidate_dataset_hash,
            },
        },
        "manifest_hash",
    )
    active_path = p5 / "active_contract.json"
    _write_json(
        active_path,
        {
            "active_contract": "trip-check-p5-v4",
            "formal_evidence_status": "READY",
            "dataset_manifest_hash": dataset["manifest_hash"],
            "blind_seal_v4_sha256": _file_sha(seal_path),
            "candidate_freeze_commit": "3" * 40,
        },
    )
    nonblind_run_path = tmp_path / "nonblind" / "run_group_manifest.json"
    blind_run_path = tmp_path / "blind" / "run_group_manifest.json"
    nonblind_run = _write_hashed(
        nonblind_run_path,
        _run_manifest("nonblind", dataset["manifest_hash"], _file_sha(run_spec)),
        "manifest_hash",
    )
    blind_run = _write_hashed(
        blind_run_path,
        _run_manifest("frozen_blind", dataset["manifest_hash"], _file_sha(run_spec)),
        "manifest_hash",
    )
    nonblind_score_path = tmp_path / "scores" / "nonblind.json"
    blind_score_path = tmp_path / "scores" / "blind.json"
    panel_path = tmp_path / "scores" / "panel.json"
    _write_json(nonblind_score_path, _nonblind_score(nonblind_run))
    _write_json(blind_score_path, _blind_score(blind_run, _file_sha(seal_path)))
    _write_json(panel_path, _judge_panel(blind_run))
    primary = {
        "dataset_manifest": dataset_path,
        "active_contract": active_path,
        "blind_seal": seal_path,
        "run_spec": run_spec,
        "judge_rubric": rubric,
        "nonblind_run_manifest": nonblind_run_path,
        "nonblind_score": nonblind_score_path,
        "blind_run_manifest": blind_run_path,
        "blind_score": blind_score_path,
        "judge_panel": panel_path,
    }
    receipt_entries = {}
    for kind in (
        "p1",
        "p2",
        "p3",
        "p4",
        "backend_pytest",
        "ruff",
        "frontend_build",
        "dual_entry",
    ):
        receipt_path = tmp_path / "verification" / f"{kind}.json"
        receipt = {
            "schema_version": "trip-check-p5-verification-receipt-v4",
            "receipt_kind": kind,
            "status": "PASS",
            "subject_commit": SUBJECT,
            "upstream_ref": UPSTREAM,
            "upstream_commit": SUBJECT,
            "dirty_tree": False,
            "readback_verified": True,
        }
        if kind == "p4":
            receipt["solver_admission"] = {
                "status": "REJECT",
                "default_strategy": "bounded_repair_v1",
            }
        _write_json(receipt_path, receipt)
        receipt_entries[kind] = {
            "path": str(receipt_path.resolve()),
            "sha256": _file_sha(receipt_path),
            "status": "PASS",
            "subject_commit": SUBJECT,
            "upstream_ref": UPSTREAM,
            "upstream_commit": SUBJECT,
            "dirty_tree": False,
            "readback_verified": True,
        }
    formal_path = tmp_path / "formal" / "receipt.json"
    formal = {
        "schema_version": "trip-check-p5-formal-validation-receipt-v4",
        "status": "PASS",
        "formal": True,
        "subject_commit": SUBJECT,
        "upstream_ref": UPSTREAM,
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
        "dataset_id": "trip-check-p5-360-v4",
        "dataset_manifest_hash": dataset["manifest_hash"],
        "bindings": {f"{key}_sha256": _file_sha(path) for key, path in primary.items()},
        "counts": {
            "nonblind_cases": 270,
            "blind_cases": 90,
            "nonblind_terminals": 810,
            "blind_terminals": 270,
            "replay_readback": 1080,
            "judge_rounds": 3,
            "judge_provenance": 3,
        },
        "verification_receipts": receipt_entries,
        "errors": [],
    }
    _write_hashed(formal_path, formal, "receipt_hash")
    return {
        "repo_root": root,
        **primary,
        "formal_receipt": formal_path,
    }


def test_v4_gate_binds_full_replay_judges_and_verification_receipts(
    tmp_path: Path,
) -> None:
    paths = _formal_fixture(tmp_path)
    schema = Path(__file__).parents[1] / "evals" / "trip_check_v1" / "p5" / "gate_v4.schema.json"
    manifest = build_p5_gate_manifest_v4(
        repo_root=paths["repo_root"],
        dataset_manifest_path=paths["dataset_manifest"],
        active_contract_path=paths["active_contract"],
        blind_seal_path=paths["blind_seal"],
        run_spec_path=paths["run_spec"],
        rubric_path=paths["judge_rubric"],
        nonblind_run_manifest_path=paths["nonblind_run_manifest"],
        nonblind_score_path=paths["nonblind_score"],
        blind_run_manifest_path=paths["blind_run_manifest"],
        blind_score_path=paths["blind_score"],
        judge_panel_path=paths["judge_panel"],
        formal_receipt_path=paths["formal_receipt"],
        output_path=tmp_path / "output" / "gate.json",
        gate_schema_path=schema,
        require_current_subject=False,
    )
    assert manifest["status"] == "PASS"
    assert manifest["promotion_decision"] == "KEEP_CORE_B"
    assert manifest["counts"]["replay_readback"] == 1080
    assert manifest["solver_admission"]["promotion_eligible"] is False
    assert len(manifest["artifact_index"]) == 19


def test_nonblind_parser_rejects_missing_replay_readback() -> None:
    run = _run_manifest("nonblind", HEX64, "c" * 64)
    run["manifest_hash"] = digest(run)
    score = _nonblind_score(run)
    score.pop("replay_readback_count")
    score["report_hash"] = digest(
        {key: value for key, value in score.items() if key != "report_hash"}
    )
    with pytest.raises(P5GateErrorV4, match="V4_NONBLIND_SCORE_FIELDS_MISSING"):
        parse_nonblind_score_v4(score, run=run)


def test_nonblind_parser_never_allows_rejected_solver_promotion() -> None:
    run = _run_manifest("nonblind", HEX64, "c" * 64)
    run["manifest_hash"] = digest(run)
    score = _nonblind_score(run, "PROMOTE_ADMITTED_CHALLENGER")
    score["admitted_challenger_variant_id"] = "solver_c"
    score["challenger_admission"] = {"status": "ADMITTED", "variant_id": "solver_c"}
    score["report_hash"] = digest(
        {key: value for key, value in score.items() if key != "report_hash"}
    )
    with pytest.raises(P5GateErrorV4, match="V4_SOLVER_PROMOTION_FORBIDDEN"):
        parse_nonblind_score_v4(score, run=run)
