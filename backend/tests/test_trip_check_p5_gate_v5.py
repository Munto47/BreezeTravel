from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.formal_receipts_v5 import (
    RepoBindingV5,
    build_dataset_formal_validation_receipt_v5,
    build_formal_gate_receipt_v5,
    build_verification_receipt_v5,
    execute_command_receipt_v5,
)
from evals.trip_check_v1.p5.gate_v5 import (
    P5GateErrorV5,
    build_p5_gate_manifest_v5,
    parse_nonblind_score_v5,
)


SUBJECT = "a" * 40
UPSTREAM = "origin/codex/p5-v5"
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
        "schema_version": "trip-check-p5-run-group-v5",
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
        "schema_version": "trip-check-p5-nonblind-score-report-v5",
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
        "schema_version": "trip-check-p5-isolated-blind-score-v5",
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
        "schema_version": "trip-check-p5-judge-panel-v5",
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
        path = p5 / f"{key}.v5.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        dataset_files[key] = {
            "path": f"evals/trip_check_v1/p5/{path.name}",
            "row_count": count,
            "file_sha256": _file_sha(path),
            "content_sha256": HEX64,
        }
    seal_path = p5 / "sealed" / "frozen_blind.v5.seal.json"
    candidate_dataset_hash = "4" * 64
    _write_json(
        seal_path,
        {
            "schema_version": "trip-check-p5-blind-seal-v5",
            "candidate_dataset_manifest_hash": candidate_dataset_hash,
            "candidate_freeze_commit": "3" * 40,
            "case_count": 90,
            "split": "frozen_blind",
            "scoring_payload_present": False,
            "human_evidence": False,
            "run_spec_template_sha256": _file_sha(run_spec),
            "rubric_sha256": _file_sha(rubric),
            "contracts_v3_sha256": _file_sha(contracts),
            "dataset_contracts_v5_sha256": "5" * 64,
            "external_bundle_sha256": "6" * 64,
            "labels_canonical_sha256": "7" * 64,
            "review_receipt_sha256": "8" * 64,
        },
    )
    dataset_path = p5 / "dataset_v5.manifest.json"
    dataset = _write_hashed(
        dataset_path,
        {
            "schema_version": "trip-check-p5-dataset-manifest-v5",
            "dataset_id": "trip-check-p5-360-v5",
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
                "dataset_contracts_v5_sha256": "5" * 64,
            },
            "frozen": True,
            "formal_validation_eligible": True,
            "seal_status": "SEALED",
            "sealing_commitment": {
                "status": "SEALED",
                "blind_seal_file_sha256": _file_sha(seal_path),
                "candidate_dataset_manifest_hash": candidate_dataset_hash,
                "external_bundle_sha256": "6" * 64,
                "labels_canonical_sha256": "7" * 64,
                "review_receipt_sha256": "8" * 64,
            },
        },
        "manifest_hash",
    )
    active_path = p5 / "active_contract.json"
    _write_json(
        active_path,
        {
            "active_contract": "trip-check-p5-v5",
            "formal_evidence_status": "READY",
            "dataset_manifest_hash": dataset["manifest_hash"],
            "blind_seal_v5_sha256": _file_sha(seal_path),
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
    binding = RepoBindingV5(SUBJECT, UPSTREAM, SUBJECT, False)
    verification_paths = {}
    p4_gate = tmp_path / "evidence" / "p4_gate.json"
    _write_json(
        p4_gate,
        {
            "status": "PASS",
            "solver_admission": {
                "status": "REJECT",
                "default_strategy": "bounded_repair_v1",
            },
        },
    )
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
        command = execute_command_receipt_v5(
            repo_root=root,
            kind=kind,
            command=[sys.executable, "-c", "print('verified')"],
            command_cwd=root,
            config_artifacts={"run_spec": run_spec.resolve()},
            expected_artifacts=(
                {"p4_gate_manifest": p4_gate.resolve()} if kind == "p4" else {}
            ),
            output_dir=tmp_path / "commands" / kind,
            repo_binding=binding,
        )
        receipt_path = tmp_path / "verification" / f"{kind}.json"
        build_verification_receipt_v5(
            repo_root=root,
            command_result_path=Path(command["receipt_path"]),
            output_path=receipt_path,
        )
        verification_paths[kind] = receipt_path
    validator = root / "backend" / "scripts" / "validate_trip_check_p5_dataset_v5.py"
    validator.parent.mkdir(parents=True, exist_ok=True)
    validator.write_text("# validator fixture\n", encoding="utf-8")
    validation_output = {
        "schema_version": "trip-check-p5-dataset-validation-v5",
        "status": "PASS",
        "formal": True,
        "errors": [],
        "manifest_hash": dataset["manifest_hash"],
    }
    dataset_command = execute_command_receipt_v5(
        repo_root=root,
        kind="dataset_formal",
        command=[
            sys.executable,
            "-c",
            f"import json; print(json.dumps({validation_output!r}))",
        ],
        command_cwd=root,
        config_artifacts={"dataset_manifest": dataset_path.resolve()},
        expected_artifacts={},
        output_dir=tmp_path / "commands" / "dataset_formal",
        repo_binding=binding,
    )
    dataset_receipt_path = tmp_path / "formal" / "dataset_receipt.json"
    build_dataset_formal_validation_receipt_v5(
        repo_root=root,
        command_result_path=Path(dataset_command["receipt_path"]),
        dataset_manifest_path=dataset_path,
        validator_path=validator,
        output_path=dataset_receipt_path,
    )
    formal_path = tmp_path / "formal" / "receipt.json"
    build_formal_gate_receipt_v5(
        repo_root=root,
        dataset_receipt_path=dataset_receipt_path,
        verification_receipts=verification_paths,
        primary_artifacts=primary,
        output_path=formal_path,
        repo_binding=binding,
    )
    return {
        "repo_root": root,
        **primary,
        "formal_receipt": formal_path,
    }


def test_v5_gate_binds_full_replay_judges_and_verification_receipts(
    tmp_path: Path,
) -> None:
    paths = _formal_fixture(tmp_path)
    schema = Path(__file__).parents[1] / "evals" / "trip_check_v1" / "p5" / "gate_v5.schema.json"
    manifest = build_p5_gate_manifest_v5(
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
    assert len(manifest["artifact_index"]) == 20


def test_nonblind_parser_rejects_missing_replay_readback() -> None:
    run = _run_manifest("nonblind", HEX64, "c" * 64)
    run["manifest_hash"] = digest(run)
    score = _nonblind_score(run)
    score.pop("replay_readback_count")
    score["report_hash"] = digest(
        {key: value for key, value in score.items() if key != "report_hash"}
    )
    with pytest.raises(P5GateErrorV5, match="V5_NONBLIND_SCORE_FIELDS_MISSING"):
        parse_nonblind_score_v5(score, run=run)


def test_nonblind_parser_never_allows_rejected_solver_promotion() -> None:
    run = _run_manifest("nonblind", HEX64, "c" * 64)
    run["manifest_hash"] = digest(run)
    score = _nonblind_score(run, "PROMOTE_ADMITTED_CHALLENGER")
    score["admitted_challenger_variant_id"] = "solver_c"
    score["challenger_admission"] = {"status": "ADMITTED", "variant_id": "solver_c"}
    score["report_hash"] = digest(
        {key: value for key, value in score.items() if key != "report_hash"}
    )
    with pytest.raises(P5GateErrorV5, match="V5_SOLVER_PROMOTION_FORBIDDEN"):
        parse_nonblind_score_v5(score, run=run)


def test_gate_output_is_limited_to_external_or_local_artifacts(tmp_path: Path) -> None:
    paths = _formal_fixture(tmp_path)
    schema = (
        Path(__file__).parents[1]
        / "evals"
        / "trip_check_v1"
        / "p5"
        / "gate_v5.schema.json"
    )
    with pytest.raises(P5GateErrorV5, match="V5_GATE_OUTPUT_PATH_FORBIDDEN"):
        build_p5_gate_manifest_v5(
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
            output_path=paths["repo_root"] / "gate.json",
            gate_schema_path=schema,
            require_current_subject=False,
        )


def test_gate_rechecks_repository_state_after_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _formal_fixture(tmp_path)
    schema = (
        Path(__file__).parents[1]
        / "evals"
        / "trip_check_v1"
        / "p5"
        / "gate_v5.schema.json"
    )
    clean = {
        "subject_commit": SUBJECT,
        "upstream_ref": UPSTREAM,
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
    }
    states = [clean, clean, {**clean, "dirty_tree": True}]
    monkeypatch.setattr(
        "evals.trip_check_v1.p5.gate_v5._repo_state_v5",
        lambda _root: states.pop(0),
    )
    with pytest.raises(
        P5GateErrorV5, match="V5_GATE_POST_WRITE_REPOSITORY_STATE_CHANGED"
    ):
        build_p5_gate_manifest_v5(
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
            output_path=tmp_path / "external" / "gate.json",
            gate_schema_path=schema,
            require_current_subject=True,
        )
