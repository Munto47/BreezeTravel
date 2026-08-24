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
    _parse_run_manifest_v5,
    build_p5_gate_manifest_v5,
    parse_nonblind_score_v5,
)
from evals.trip_check_v1.p5.judge_v5 import judge_rubric_projection_hash_v5


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


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )


def _run_manifest(
    lane: str,
    dataset_hash: str,
    run_spec_hash: str,
    rubric_hash: str,
    run_dir: Path,
) -> dict:
    cases, terminals = (270, 810) if lane == "nonblind" else (90, 270)
    terminal_path = run_dir / "terminal_outputs.jsonl"
    replay_path = run_dir / "replay_readback.jsonl"
    terminal_rows = [{"row": index} for index in range(terminals)]
    replay_rows = [{"row": index} for index in range(terminals)]
    _write_jsonl(terminal_path, terminal_rows)
    _write_jsonl(replay_path, replay_rows)
    index = {
        "schema_version": f"trip-check-p5-{lane}-artifact-index-v5",
        "entries": [
            {
                "path": terminal_path.name,
                "byte_size": terminal_path.stat().st_size,
                "sha256": _file_sha(terminal_path),
                "content_sha256": digest(terminal_rows),
            },
            {
                "path": replay_path.name,
                "byte_size": replay_path.stat().st_size,
                "sha256": _file_sha(replay_path),
                "content_sha256": digest(replay_rows),
            },
        ],
    }
    index["artifact_index_hash"] = digest(index)
    _write_json(run_dir / "artifact_index.json", index)
    return {
        "schema_version": (
            "trip-check-p5-run-group-v5"
            if lane == "nonblind"
            else "trip-check-p5-blind-run-group-v5"
        ),
        "run_id": f"run-{lane}",
        "status": "PASS",
        "formal_evidence": True,
        "lane": lane,
        "subject_commit": SUBJECT,
        "upstream_ref": UPSTREAM,
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
        "dataset_manifest_hash": dataset_hash,
        "artifact_index_path": "artifact_index.json",
        "artifact_index_hash": index["artifact_index_hash"],
        "terminal_outputs_path": "terminal_outputs.jsonl",
        "terminal_outputs_file_sha256": _file_sha(terminal_path),
        "terminal_outputs_content_sha256": digest(terminal_rows),
        "replay_outputs_path": "replay_readback.jsonl",
        "replay_outputs_file_sha256": _file_sha(replay_path),
        "replay_outputs_content_sha256": digest(replay_rows),
        "run_spec_template_sha256": run_spec_hash,
        "rubric_sha256": rubric_hash,
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
            "terminal_outputs_content_sha256": run["terminal_outputs_content_sha256"],
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


def _judge_panel(run: dict, rubric_path: Path) -> dict:
    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
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
                "source_rubric_sha256": _file_sha(rubric_path),
                "judge_input_rubric_sha256": judge_rubric_projection_hash_v5(
                    rubric
                ),
                "terminal_outputs_content_sha256": run["terminal_outputs_content_sha256"],
            }
            for index in range(1, 4)
        ],
        "mapping_sha256": "2" * 64,
        "subject_commit": SUBJECT,
        "dataset_manifest_hash": run["dataset_manifest_hash"],
        "run_group_manifest_hash": run["manifest_hash"],
        "artifact_index_hash": run["artifact_index_hash"],
        "terminal_outputs_content_sha256": run["terminal_outputs_content_sha256"],
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
    source_rubric = (
        Path(__file__).parents[1]
        / "evals"
        / "trip_check_v1"
        / "p5"
        / "judge_rubric_v2.json"
    )
    _write_json(rubric, json.loads(source_rubric.read_text(encoding="utf-8")))
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
    nonce_path = tmp_path / "nonce" / "nonce.json"
    nonce = {
        "schema_version": "trip-check-p5-blind-run-nonce-v5",
        "purpose": "execute_frozen_blind_once",
        "dataset_id": "trip-check-p5-360-v5",
        "active_contract": "trip-check-p5-v5",
        "nonce": "9" * 64,
    }
    _write_json(nonce_path, nonce)
    mint_receipt_path = tmp_path / "nonce" / "mint.json"
    mint = {
        "schema_version": "trip-check-p5-blind-run-nonce-mint-receipt-v5",
        "status": "MINTED_NOT_CONSUMED",
        "subject_commit": SUBJECT,
        "upstream_ref": UPSTREAM,
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
        "nonce_file_path": str(nonce_path.resolve()),
        "nonce_file_sha256": _file_sha(nonce_path),
        "nonce_sha256": digest(nonce["nonce"]),
        "label_payload_present": False,
    }
    mint["receipt_hash"] = digest(mint)
    _write_json(mint_receipt_path, mint)
    nonblind_core = _run_manifest(
        "nonblind",
        dataset["manifest_hash"],
        _file_sha(run_spec),
        _file_sha(rubric),
        nonblind_run_path.parent,
    )
    nonblind_run = _write_hashed(nonblind_run_path, nonblind_core, "manifest_hash")
    blind_core = _run_manifest(
        "frozen_blind",
        dataset["manifest_hash"],
        _file_sha(run_spec),
        _file_sha(rubric),
        blind_run_path.parent,
    )
    blind_core["nonce_sha256"] = digest(nonce["nonce"])
    run_binding_hash = digest(blind_core)
    consumption_path = tmp_path / "nonce" / "consumed.json"
    _write_json(
        consumption_path,
        {
            "schema_version": "trip-check-p5-blind-run-consumption-receipt-v5",
            "status": "CONSUMED",
            "dataset_id": "trip-check-p5-360-v5",
            "dataset_manifest_hash": dataset["manifest_hash"],
            "nonce_sha256": digest(nonce["nonce"]),
            "claimed_at": "2026-08-24T00:00:00+00:00",
            "completed_at": "2026-08-24T00:01:00+00:00",
            "run_id": blind_core["run_id"],
            "run_binding_hash": run_binding_hash,
            "artifact_index_hash": blind_core["artifact_index_hash"],
            "failure_reason_code": None,
        },
    )
    blind_core["run_binding_hash"] = run_binding_hash
    blind_core["nonce_consumption_receipt_sha256"] = _file_sha(consumption_path)
    blind_run = _write_hashed(blind_run_path, blind_core, "manifest_hash")
    nonblind_score_path = tmp_path / "scores" / "nonblind.json"
    blind_score_path = tmp_path / "scores" / "blind.json"
    panel_path = tmp_path / "scores" / "panel.json"
    _write_json(nonblind_score_path, _nonblind_score(nonblind_run))
    _write_json(blind_score_path, _blind_score(blind_run, _file_sha(seal_path)))
    _write_json(panel_path, _judge_panel(blind_run, rubric))
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
        "nonblind_terminal_outputs": nonblind_run_path.parent / "terminal_outputs.jsonl",
        "nonblind_replay_outputs": nonblind_run_path.parent / "replay_readback.jsonl",
        "nonblind_artifact_index": nonblind_run_path.parent / "artifact_index.json",
        "blind_terminal_outputs": blind_run_path.parent / "terminal_outputs.jsonl",
        "blind_replay_outputs": blind_run_path.parent / "replay_readback.jsonl",
        "blind_artifact_index": blind_run_path.parent / "artifact_index.json",
        "blind_nonce": nonce_path,
        "blind_nonce_mint_receipt": mint_receipt_path,
        "blind_nonce_consumption_receipt": consumption_path,
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
            expected_artifacts=({"p4_gate_manifest": p4_gate.resolve()} if kind == "p4" else {}),
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


def _invoke_gate(paths: dict[str, Path], output_path: Path) -> dict:
    schema = Path(__file__).parents[1] / "evals" / "trip_check_v1" / "p5" / "gate_v5.schema.json"
    return build_p5_gate_manifest_v5(
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
        blind_nonce_path=paths["blind_nonce"],
        blind_nonce_mint_receipt_path=paths["blind_nonce_mint_receipt"],
        blind_nonce_consumption_receipt_path=paths["blind_nonce_consumption_receipt"],
        formal_receipt_path=paths["formal_receipt"],
        output_path=output_path,
        gate_schema_path=schema,
        require_current_subject=False,
    )


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
        blind_nonce_path=paths["blind_nonce"],
        blind_nonce_mint_receipt_path=paths["blind_nonce_mint_receipt"],
        blind_nonce_consumption_receipt_path=paths["blind_nonce_consumption_receipt"],
        formal_receipt_path=paths["formal_receipt"],
        output_path=tmp_path / "output" / "gate.json",
        gate_schema_path=schema,
        require_current_subject=False,
    )
    assert manifest["status"] == "PASS"
    assert manifest["promotion_decision"] == "KEEP_CORE_B"
    assert manifest["counts"]["replay_readback"] == 1080
    assert manifest["solver_admission"]["promotion_eligible"] is False
    assert len(manifest["artifact_index"]) == 29


def test_nonblind_parser_rejects_missing_replay_readback(tmp_path: Path) -> None:
    run = _run_manifest("nonblind", HEX64, "c" * 64, "d" * 64, tmp_path)
    run["manifest_hash"] = digest(run)
    score = _nonblind_score(run)
    score.pop("replay_readback_count")
    score["report_hash"] = digest({key: value for key, value in score.items() if key != "report_hash"})
    with pytest.raises(P5GateErrorV5, match="V5_NONBLIND_SCORE_FIELDS_MISSING"):
        parse_nonblind_score_v5(score, run=run)


def test_blind_run_parser_requires_blind_runner_schema(tmp_path: Path) -> None:
    run = _run_manifest("frozen_blind", HEX64, "c" * 64, "d" * 64, tmp_path)
    run.update(
        {
            "nonce_sha256": "e" * 64,
            "run_binding_hash": "f" * 64,
            "nonce_consumption_receipt_sha256": "1" * 64,
        }
    )
    run["schema_version"] = "trip-check-p5-run-group-v5"
    run["manifest_hash"] = digest(run)
    with pytest.raises(P5GateErrorV5, match="V5_RUN_MANIFEST_CONTRACT_INVALID"):
        _parse_run_manifest_v5(run, lane="frozen_blind", dataset_hash=HEX64)


def test_nonblind_parser_never_allows_rejected_solver_promotion(
    tmp_path: Path,
) -> None:
    run = _run_manifest("nonblind", HEX64, "c" * 64, "d" * 64, tmp_path)
    run["manifest_hash"] = digest(run)
    score = _nonblind_score(run, "PROMOTE_ADMITTED_CHALLENGER")
    score["admitted_challenger_variant_id"] = "solver_c"
    score["challenger_admission"] = {"status": "ADMITTED", "variant_id": "solver_c"}
    score["report_hash"] = digest({key: value for key, value in score.items() if key != "report_hash"})
    with pytest.raises(P5GateErrorV5, match="V5_SOLVER_PROMOTION_FORBIDDEN"):
        parse_nonblind_score_v5(score, run=run)


def test_gate_rejects_run_artifact_mutated_after_manifest(tmp_path: Path) -> None:
    paths = _formal_fixture(tmp_path)
    with paths["blind_replay_outputs"].open("a", encoding="utf-8") as handle:
        handle.write("{}\n")
    with pytest.raises(P5GateErrorV5, match="V5_RUN_OUTPUT_READBACK_MISMATCH"):
        _invoke_gate(paths, tmp_path / "output" / "gate.json")


def test_gate_rejects_nonce_consumption_receipt_substitution(tmp_path: Path) -> None:
    paths = _formal_fixture(tmp_path)
    consumed = json.loads(paths["blind_nonce_consumption_receipt"].read_text(encoding="utf-8"))
    consumed["completed_at"] = "2026-08-24T00:02:00+00:00"
    _write_json(paths["blind_nonce_consumption_receipt"], consumed)
    with pytest.raises(P5GateErrorV5, match="V5_BLIND_NONCE_CONSUMPTION_BINDING_INVALID"):
        _invoke_gate(paths, tmp_path / "output" / "gate.json")


def test_gate_rejects_judge_rubric_provenance_substitution(tmp_path: Path) -> None:
    paths = _formal_fixture(tmp_path)
    panel = json.loads(paths["judge_panel"].read_text(encoding="utf-8"))
    panel["provenance"][0]["source_rubric_sha256"] = "0" * 64
    panel["report_hash"] = digest({key: value for key, value in panel.items() if key != "report_hash"})
    _write_json(paths["judge_panel"], panel)
    with pytest.raises(P5GateErrorV5, match="V5_JUDGE_RUBRIC_PROVENANCE_INVALID"):
        _invoke_gate(paths, tmp_path / "output" / "gate.json")


def test_gate_output_is_limited_to_external_or_local_artifacts(tmp_path: Path) -> None:
    paths = _formal_fixture(tmp_path)
    schema = Path(__file__).parents[1] / "evals" / "trip_check_v1" / "p5" / "gate_v5.schema.json"
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
            blind_nonce_path=paths["blind_nonce"],
            blind_nonce_mint_receipt_path=paths["blind_nonce_mint_receipt"],
            blind_nonce_consumption_receipt_path=paths["blind_nonce_consumption_receipt"],
            formal_receipt_path=paths["formal_receipt"],
            output_path=paths["repo_root"] / "gate.json",
            gate_schema_path=schema,
            require_current_subject=False,
        )


def test_gate_rechecks_repository_state_after_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _formal_fixture(tmp_path)
    schema = Path(__file__).parents[1] / "evals" / "trip_check_v1" / "p5" / "gate_v5.schema.json"
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
    with pytest.raises(P5GateErrorV5, match="V5_GATE_POST_WRITE_REPOSITORY_STATE_CHANGED"):
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
            blind_nonce_path=paths["blind_nonce"],
            blind_nonce_mint_receipt_path=paths["blind_nonce_mint_receipt"],
            blind_nonce_consumption_receipt_path=paths["blind_nonce_consumption_receipt"],
            formal_receipt_path=paths["formal_receipt"],
            output_path=tmp_path / "external" / "gate.json",
            gate_schema_path=schema,
            require_current_subject=True,
        )
