from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.contracts import P5TerminalOutput, TerminalStatus, VARIANT_IDS
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.judge import (
    P5JudgeError,
    aggregate_judge_rounds,
    export_judge_bundles,
)
from evals.trip_check_v1.p5.runner import write_jsonl_atomic


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    cases = []
    for index, city in enumerate(("北京", "上海", "杭州")):
        product_input = {"source_type": "MANUAL_TEXT", "raw_text": f"{city}2人2天。"}
        cases.append(
            {
                "case_id": f"p5.dev.case-{index}",
                "split": "dev",
                "city": city,
                "trip_days": 2,
                "group_size": 2,
                "input_kind": "TEXT",
                "difficulty": "CLEAN",
                "product_input": product_input,
                "normalized_input_sha256": digest(product_input),
                "runner_control": {
                    "provider_snapshot_id": "snapshot-v1",
                    "fault_profile_id": "advice_completeness",
                    "seed": index,
                },
                "oracle": {"must_not_export": True},
            }
        )
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
        encoding="utf-8",
    )
    versions = {
        "legacy_a": ("legacy-a-v1", "legacy_native_only"),
        "core_b": ("core-b-v1", "bounded_repair_v1"),
        "solver_c": ("solver-c-v1", "cp_sat_v1"),
    }
    run_specs = {}
    for variant_id, (adapter_version, strategy) in versions.items():
        run_specs[variant_id] = {
            "variant_id": variant_id,
            "adapter_version": adapter_version,
            "repair_strategy": strategy,
        }
    outputs = []
    for case in cases:
        for variant_id in VARIANT_IDS:
            adapter_version, strategy = versions[variant_id]
            outputs.append(
                P5TerminalOutput(
                    case_id=case["case_id"],
                    split="dev",
                    city=case["city"],
                    input_kind="TEXT",
                    input_hash=case["normalized_input_sha256"],
                    provider_snapshot_id="snapshot-v1",
                    fault_profile_id="advice_completeness",
                    case_seed=case["runner_control"]["seed"],
                    run_spec_hash=digest(run_specs[variant_id]),
                    variant_id=variant_id,
                    adapter_version=adapter_version,
                    repair_strategy=strategy,
                    terminal_status=TerminalStatus.SUCCEEDED,
                    capability_outcomes={"input_stage": "NATIVE_TEXT"},
                    native_output={
                        "recommendation_text": "先确认时间，再按证据调整行程。"
                        if variant_id == "legacy_a"
                        else None
                    },
                    evaluation_projection={},
                    findings=[
                        {"reason_code": "TIME_CHAIN_CONFLICT", "severity": "HIGH", "status": "OPEN"}
                    ],
                    advice=[
                        {
                            "finding_reason_code": "TIME_CHAIN_CONFLICT",
                            "action_type": "CONFIRM",
                            "requires_user_confirmation": True,
                            "has_repair": False,
                        }
                    ],
                    postcheck={"new_high_count": 0, "new_unknown_count": 0},
                    trace=[],
                    receipts=[],
                    latency_ms=1,
                    token_count=0,
                    cost_usd=0,
                    error_category=None,
                    raw_artifact_hash="1" * 64,
                    semantic_output_hash="2" * 64,
                    replay_hash="2" * 64,
                )
            )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    terminal_path = run_dir / "terminal_outputs.jsonl"
    content_hash = write_jsonl_atomic(terminal_path, outputs)
    manifest = {
        "schema_version": "trip-check-p5-run-group-v1",
        "run_id": "judge-test",
        "lane": "nonblind",
        "subject_commit": "a" * 40,
        "dirty_tree": False,
        "blind_labels_read": False,
        "variant_ids": list(VARIANT_IDS),
        "terminal_count": 9,
        "run_specs": run_specs,
        "terminal_outputs_path": terminal_path.name,
        "terminal_outputs_file_sha256": _sha(terminal_path),
        "terminal_outputs_content_sha256": content_hash,
    }
    manifest["manifest_hash"] = digest(manifest)
    (run_dir / "run_group_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    rubric = {
        "schema_version": "trip-check-p5-judge-rubric-v1",
        "fact_authority": "DETERMINISTIC_ORACLE_ONLY",
        "human_evidence": False,
        "dimensions": {
            "clarity": {"minimum": 0, "maximum": 4},
            "actionability": {"minimum": 0, "maximum": 4},
            "evidence_boundary_expression": {"minimum": 0, "maximum": 4},
        },
    }
    rubric_path = tmp_path / "rubric.json"
    rubric_path.write_text(json.dumps(rubric, ensure_ascii=False) + "\n", encoding="utf-8")
    return repo, run_dir, cases_path, rubric_path


def _round_reports(export_dir: Path) -> list[Path]:
    paths = []
    for round_index in range(1, 4):
        bundle_path = export_dir / f"judge_bundle_round_{round_index}.json"
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        report = {
            "schema_version": "trip-check-p5-judge-round-v1",
            "round_index": round_index,
            "evaluator_id": f"evaluator-{round_index}",
            "agent_task_id": f"task-{round_index}",
            "model_id": f"judge-model-{round_index}",
            "started_at": f"2026-08-23T00:0{round_index}:00Z",
            "ended_at": f"2026-08-23T00:0{round_index}:30Z",
            "bundle_sha256": _sha(bundle_path),
            "rubric_sha256": bundle["run_binding"]["rubric_sha256"],
            "api_usage_count": 0,
            "tool_usage_count": 0,
            "scores": [
                {
                    "blind_item_id": item["blind_item_id"],
                    "slot_id": item["slot_id"],
                    "clarity": 3,
                    "actionability": 3,
                    "evidence_boundary_expression": 3,
                    "unsupported_claim_candidate_ids": [],
                    "derived_verdict": "PASS",
                }
                for item in bundle["items"]
            ],
        }
        path = export_dir / f"judge_round_{round_index}.json"
        path.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def test_judge_export_hides_variant_oracle_and_balances_slots(tmp_path: Path) -> None:
    repo, run_dir, cases_path, rubric_path = _source_fixture(tmp_path)
    export_dir = tmp_path / "judge-export"
    receipt = export_judge_bundles(
        repo_root=repo,
        run_dir=run_dir,
        cases_path=cases_path,
        output_dir=export_dir,
        rubric_path=rubric_path,
    )
    assert receipt["round_count"] == 3
    assert receipt["items_per_round"] == 9
    mapping = json.loads((export_dir / "judge_variant_mapping.json").read_text(encoding="utf-8"))
    for round_index in range(1, 4):
        rows = [row for row in mapping["rows"] if row["round_index"] == round_index]
        counts = Counter((row["slot_id"], row["variant_id"]) for row in rows)
        assert set(counts.values()) == {1}
        bundle_text = (export_dir / f"judge_bundle_round_{round_index}.json").read_text(
            encoding="utf-8"
        )
        assert "variant_id" not in bundle_text
        assert "case_id" not in bundle_text
        assert "must_not_export" not in bundle_text


def test_three_round_panel_requires_unique_zero_api_provenance(tmp_path: Path) -> None:
    repo, run_dir, cases_path, rubric_path = _source_fixture(tmp_path)
    export_dir = tmp_path / "judge-export"
    receipt = export_judge_bundles(
        repo_root=repo,
        run_dir=run_dir,
        cases_path=cases_path,
        output_dir=export_dir,
        rubric_path=rubric_path,
    )
    rounds = _round_reports(export_dir)
    report = aggregate_judge_rounds(
        repo_root=repo,
        mapping_path=export_dir / "judge_variant_mapping.json",
        mapping_sha256=receipt["mapping_file_sha256"],
        round_paths=rounds,
    )
    assert report["status"] == "PASS"
    assert report["candidate_count"] == 9
    assert report["verdict_agreement_rate"] == 1
    assert report["human_calibration_performed"] is False
    assert report["judge_may_override_deterministic_failure"] is False

    changed = json.loads(rounds[1].read_text(encoding="utf-8"))
    changed["evaluator_id"] = "evaluator-1"
    rounds[1].write_text(json.dumps(changed) + "\n", encoding="utf-8")
    with pytest.raises(P5JudgeError, match="JUDGE_ROUND_IDENTITY_NOT_UNIQUE"):
        aggregate_judge_rounds(
            repo_root=repo,
            mapping_path=export_dir / "judge_variant_mapping.json",
            mapping_sha256=receipt["mapping_file_sha256"],
            round_paths=rounds,
        )
