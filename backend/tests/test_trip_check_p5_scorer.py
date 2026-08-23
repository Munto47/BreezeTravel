from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.contracts import P5TerminalOutput, TerminalStatus
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.runner import write_jsonl_atomic
from evals.trip_check_v1.p5.scorer import score_case, score_run_group, validate_run_group


def _case(*, unknown: bool = False, reason: str = "P4_DUPLICATE_APPLY") -> dict:
    return {
        "case_id": "p5.dev.bj.001",
        "split": "dev",
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": "TEXT",
        "difficulty": "HARD",
        "normalized_input_sha256": "1" * 64,
        "runner_control": {
            "provider_snapshot_id": "snapshot-v1",
            "fault_profile_id": "duplicate_apply",
            "seed": 7,
        },
        "oracle": {
            "task_success_required": True,
            "requires_user_resolution": False,
            "required_reason_codes": [reason],
            "wrong_city_or_poi_max": 0,
            "max_new_blocker_high_unknown": 0,
            "unknown_must_be_preserved": unknown,
            "advice_required": True,
            "specific_place_allowed": True,
            "expected_strategy_outcome": "FEASIBLE",
        },
    }


def _output(*, run_spec_hash: str = "2" * 64) -> P5TerminalOutput:
    return P5TerminalOutput(
        case_id="p5.dev.bj.001",
        split="dev",
        city="北京",
        input_kind="TEXT",
        input_hash="1" * 64,
        provider_snapshot_id="snapshot-v1",
        fault_profile_id="duplicate_apply",
        case_seed=7,
        run_spec_hash=run_spec_hash,
        variant_id="core_b",
        adapter_version="core-b-v1",
        repair_strategy="bounded_repair_v1",
        terminal_status=TerminalStatus.SUCCEEDED,
        capability_outcomes={},
        native_output={
            "requires_user_resolution": False,
            "wrong_poi_auto_accept_count": 0,
            "replay_side_effect_counts_equal": True,
        },
        evaluation_projection={
            "advice_action_count": 1,
            "candidate_receipt_coverage": 1.0,
            "unverified_specific_place_claim_count": 0,
            "unknown_preserved": True,
        },
        findings=[{"reason_code": "TIME_CHAIN_CONFLICT", "severity": "HIGH", "status": "OPEN"}],
        advice=[{"finding_reason_code": "TIME_CHAIN_CONFLICT"}],
        postcheck={
            "new_high_count": 0,
            "new_unknown_count": 0,
            "replay_side_effect_counts_equal": True,
        },
        trace=[],
        receipts=[],
        latency_ms=10,
        token_count=0,
        cost_usd=0,
        error_category=None,
        raw_artifact_hash="3" * 64,
        semantic_output_hash="4" * 64,
        replay_hash="4" * 64,
    )


def _write_run_group(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    spec = {
        "schema_version": "trip-check-p5-variant-run-spec-v1",
        "subject_commit": "a" * 40,
        "dirty_tree": False,
        "lane": "nonblind",
        "dataset_manifest_hash": "5" * 64,
        "case_set_hash": digest(["p5.dev.bj.001"]),
        "run_spec_template_hash": "6" * 64,
        "provider_snapshot_id": "snapshot-v1",
        "execution_mode": "controlled_snapshot",
        "random_seed": 7,
        "budget": {"timeout_seconds": 30},
        "replay_hash_policy": "p5-semantic-projection-v1",
        "variant_id": "core_b",
        "adapter_version": "core-b-v1",
        "repair_strategy": "bounded_repair_v1",
    }
    output = _output(run_spec_hash=digest(spec))
    terminal_path = run_dir / "terminal_outputs.jsonl"
    content_hash = write_jsonl_atomic(terminal_path, [output])
    manifest = {
        "schema_version": "trip-check-p5-run-group-v1",
        "run_id": "test-run",
        "status": "PASS",
        "formal_evidence": False,
        "lane": "nonblind",
        "subject_commit": "a" * 40,
        "dirty_tree": False,
        "case_count": 1,
        "case_set_hash": digest(["p5.dev.bj.001"]),
        "variant_ids": ["core_b"],
        "variant_count": 1,
        "terminal_count": 1,
        "expected_terminal_count": 1,
        "run_specs": {"core_b": spec},
        "terminal_outputs_path": terminal_path.name,
        "terminal_outputs_file_sha256": hashlib.sha256(terminal_path.read_bytes()).hexdigest(),
        "terminal_outputs_content_sha256": content_hash,
        "replay_executed": True,
        "replay_match_count": 1,
        "replay_mismatches": [],
        "blind_labels_read": False,
        "external_api_calls": 0,
        "human_evidence": False,
    }
    manifest["manifest_hash"] = digest(manifest)
    (run_dir / "run_group_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(json.dumps(_case(), ensure_ascii=False) + "\n", encoding="utf-8")
    return run_dir, cases_path


def test_case_score_requires_explicit_evidence_and_passes_complete_receipts() -> None:
    passing = score_case(_case(unknown=True), _output())
    assert passing.task_success is True
    assert passing.score == 100

    payload = _output().model_dump(mode="json")
    payload["evaluation_projection"].pop("unknown_preserved")
    missing_unknown = score_case(_case(unknown=True), P5TerminalOutput.model_validate(payload))
    assert missing_unknown.task_success is False
    assert "UNKNOWN_NOT_PRESERVED" in missing_unknown.safety_failure_codes


def test_case_score_does_not_treat_missing_concurrency_evidence_as_pass() -> None:
    scored = score_case(_case(reason="P4_CONCURRENT_APPLY"), _output())
    assert scored.task_success is False
    assert scored.missing_obligations == ["P4_CONCURRENT_APPLY"]
    assert "HARD_FINDING_MISS" in scored.safety_failure_codes


def test_run_group_binding_and_score_report_fail_closed(tmp_path: Path) -> None:
    run_dir, cases_path = _write_run_group(tmp_path)
    report = score_run_group(
        run_dir=run_dir,
        cases_path=cases_path,
        require_full_nonblind=False,
    )
    assert report["status"] == "PASS"
    assert report["terminal_count"] == 1
    assert report["automated_proxy_judge"] == "NOT_RUN"
    assert report["human_evidence"] is False

    terminal_path = run_dir / "terminal_outputs.jsonl"
    terminal_path.write_text(terminal_path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="file hash mismatch"):
        validate_run_group(
            run_dir=run_dir,
            cases=[_case()],
            require_full_nonblind=False,
        )


def test_formal_scorer_rejects_partial_case_set(tmp_path: Path) -> None:
    run_dir, cases_path = _write_run_group(tmp_path)
    cases = [_case(), {**_case(), "case_id": "p5.dev.bj.002"}]
    cases_path.write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exact 270-case set"):
        score_run_group(run_dir=run_dir, cases_path=cases_path, require_full_nonblind=True)
