from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.contracts import P5TerminalOutput, TerminalStatus, VARIANT_IDS
from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest
from evals.trip_check_v1.p5.final_blind_scorer import (
    P5BlindScoringError,
    _canonical_labels_hash,
    score_external_blind_run_group,
)
from evals.trip_check_v1.p5.runner import write_jsonl_atomic


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    repo = tmp_path / "repo"
    p5 = repo / "backend" / "evals" / "trip_check_v1" / "p5"
    (p5 / "sealed").mkdir(parents=True)
    template = p5 / "run_spec_template_v1.json"
    rubric = p5 / "judge_rubric_v1.json"
    template.write_text('{"schema_version":"test-template"}\n', encoding="utf-8")
    rubric.write_text('{"schema_version":"test-rubric"}\n', encoding="utf-8")

    inputs = []
    cities = ("北京", "上海", "杭州")
    input_kinds = ("TEXT", "SYNTHETIC_SCREENSHOT")
    difficulties = ("CLEAN", "MEDIUM", "HARD")
    for index in range(90):
        product_input = {"raw_text": f"controlled-{index}"}
        inputs.append(
            {
                "case_id": f"p5.blind.case-{index:03d}",
                "split": "frozen_blind",
                "city": cities[index % 3],
                "trip_days": 2,
                "group_size": 2,
                "input_kind": input_kinds[index % 2],
                "difficulty": difficulties[index % 3],
                "product_input": product_input,
                "normalized_input_sha256": digest(product_input),
                "runner_control": {
                    "provider_snapshot_id": "snapshot-v1",
                    "fault_profile_id": "duplicate_apply",
                    "seed": 1000 + index,
                },
            }
        )
    inputs_path = p5 / "frozen_blind.inputs.jsonl"
    inputs_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in inputs),
        encoding="utf-8",
        newline="\n",
    )
    labels = [
        {
            "schema_version": "trip-check-p5-blind-label-v1",
            "case_id": item["case_id"],
            "oracle": {
                "task_success_required": True,
                "requires_user_resolution": False,
                "required_reason_codes": ["P4_DUPLICATE_APPLY"],
                "wrong_city_or_poi_max": 0,
                "max_new_blocker_high_unknown": 0,
                "unknown_must_be_preserved": False,
                "advice_required": True,
                "specific_place_allowed": True,
                "expected_strategy_outcome": "FEASIBLE",
            },
        }
        for item in inputs
    ]
    seal_base = {
        "schema_version": "trip-check-p5-blind-seal-v1",
        "split": "frozen_blind",
        "case_count": 90,
        "case_ids_sha256": digest(sorted(item["case_id"] for item in inputs)),
        "inputs_file_sha256": _sha(inputs_path),
        "inputs_content_sha256": digest(inputs),
        "labels_canonical_sha256": _canonical_labels_hash(labels),
        "rubric_sha256": _sha(rubric),
        "run_spec_template_sha256": _sha(template),
        "variant_ids_sha256": digest(list(VARIANT_IDS)),
        "review_receipt_sha256": "9" * 64,
        "label_storage": "external_bundle_only",
        "label_access": "isolated_scorer_only",
        "scoring_payload_present": False,
        "human_evidence": False,
    }
    bundle = {
        "schema_version": "trip-check-p5-blind-label-bundle-v1",
        "evidence_class": "controlled_blind_oracle",
        "human_evidence": False,
        "dataset_binding": {
            "case_count": 90,
            "case_ids_sha256": seal_base["case_ids_sha256"],
            "inputs_content_sha256": seal_base["inputs_content_sha256"],
            "inputs_file_sha256": seal_base["inputs_file_sha256"],
            "rubric_sha256": seal_base["rubric_sha256"],
            "run_spec_template_sha256": seal_base["run_spec_template_sha256"],
            "variant_ids_sha256": seal_base["variant_ids_sha256"],
        },
        "labels": labels,
    }
    bundle_path = tmp_path / "external_bundle.json"
    bundle_path.write_bytes(canonical_bytes(bundle) + b"\n")
    bundle_hash = _sha(bundle_path)
    seal = {**seal_base, "external_bundle_sha256": bundle_hash}
    (p5 / "sealed" / "frozen_blind.seal.json").write_text(
        json.dumps(seal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    case_set_hash = digest(sorted(item["case_id"] for item in inputs))
    run_specs = {}
    versions = {
        "legacy_a": ("legacy-a-v1", "legacy_native_only"),
        "core_b": ("core-b-v1", "bounded_repair_v1"),
        "solver_c": ("solver-c-v1", "cp_sat_v1"),
    }
    for variant_id, (adapter_version, strategy) in versions.items():
        run_specs[variant_id] = {
            "schema_version": "trip-check-p5-variant-run-spec-v1",
            "subject_commit": "a" * 40,
            "dirty_tree": False,
            "lane": "frozen_blind",
            "dataset_manifest_hash": "1" * 64,
            "case_set_hash": case_set_hash,
            "run_spec_template_hash": seal["run_spec_template_sha256"],
            "provider_snapshot_id": "snapshot-v1",
            "execution_mode": "controlled_snapshot",
            "random_seed": 7,
            "budget": {"timeout_seconds": 30},
            "replay_hash_policy": "p5-semantic-projection-v1",
            "variant_id": variant_id,
            "adapter_version": adapter_version,
            "repair_strategy": strategy,
        }
    outputs = []
    for item in inputs:
        for variant_id in VARIANT_IDS:
            adapter_version, strategy = versions[variant_id]
            outputs.append(
                P5TerminalOutput(
                    case_id=item["case_id"],
                    split="frozen_blind",
                    city=item["city"],
                    input_kind=item["input_kind"],
                    input_hash=item["normalized_input_sha256"],
                    provider_snapshot_id="snapshot-v1",
                    fault_profile_id="duplicate_apply",
                    case_seed=item["runner_control"]["seed"],
                    run_spec_hash=digest(run_specs[variant_id]),
                    variant_id=variant_id,
                    adapter_version=adapter_version,
                    repair_strategy=strategy,
                    terminal_status=TerminalStatus.SUCCEEDED,
                    capability_outcomes={},
                    native_output={
                        "requires_user_resolution": False,
                        "wrong_poi_auto_accept_count": 0,
                        "replay_side_effect_counts_equal": True,
                    },
                    evaluation_projection={
                        "candidate_receipt_coverage": 1.0,
                        "unverified_specific_place_claim_count": 0,
                    },
                    findings=[
                        {"reason_code": "TIME_CHAIN_CONFLICT", "severity": "HIGH", "status": "OPEN"}
                    ],
                    advice=[{"finding_reason_code": "TIME_CHAIN_CONFLICT"}],
                    postcheck={
                        "new_high_count": 0,
                        "new_unknown_count": 0,
                        "replay_side_effect_counts_equal": True,
                    },
                    trace=[],
                    receipts=[],
                    latency_ms=1,
                    token_count=0,
                    cost_usd=0,
                    error_category=None,
                    raw_artifact_hash="2" * 64,
                    semantic_output_hash="3" * 64,
                    replay_hash="3" * 64,
                )
            )
    run_dir = tmp_path / "blind-run"
    run_dir.mkdir()
    terminal_path = run_dir / "terminal_outputs.jsonl"
    content_hash = write_jsonl_atomic(terminal_path, outputs)
    manifest = {
        "schema_version": "trip-check-p5-run-group-v1",
        "run_id": "blind-run",
        "status": "PASS",
        "formal_evidence": True,
        "lane": "frozen_blind",
        "subject_commit": "a" * 40,
        "dirty_tree": False,
        "case_count": 90,
        "case_set_hash": case_set_hash,
        "variant_ids": list(VARIANT_IDS),
        "variant_count": 3,
        "terminal_count": 270,
        "expected_terminal_count": 270,
        "run_specs": run_specs,
        "terminal_outputs_path": terminal_path.name,
        "terminal_outputs_file_sha256": _sha(terminal_path),
        "terminal_outputs_content_sha256": content_hash,
        "replay_executed": True,
        "replay_match_count": 270,
        "replay_mismatches": [],
        "blind_labels_read": False,
        "external_api_calls": 0,
        "human_evidence": False,
    }
    manifest["manifest_hash"] = digest(manifest)
    (run_dir / "run_group_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return repo, run_dir, bundle_path, bundle_hash


def test_blind_scorer_returns_only_sanitized_aggregates(tmp_path: Path) -> None:
    repo, run_dir, bundle_path, bundle_hash = _write_fixture(tmp_path)
    receipt = score_external_blind_run_group(
        repo_root=repo,
        run_dir=run_dir,
        expected_bundle_sha256=bundle_hash,
        bundle_path=bundle_path,
        require_current_subject=False,
    )
    assert receipt["status"] == "PASS"
    assert receipt["case_count"] == 90
    assert receipt["terminal_count"] == 270
    assert receipt["human_evidence"] is False
    assert "case_scores" not in receipt
    serialized = json.dumps(receipt, ensure_ascii=False)
    assert "p5.blind.case-" not in serialized


def test_blind_scorer_rejects_repository_bundle_and_terminal_tamper(tmp_path: Path) -> None:
    repo, run_dir, bundle_path, bundle_hash = _write_fixture(tmp_path)
    inside = repo / "bundle.json"
    inside.write_bytes(bundle_path.read_bytes())
    with pytest.raises(P5BlindScoringError) as inside_error:
        score_external_blind_run_group(
            repo_root=repo,
            run_dir=run_dir,
            expected_bundle_sha256=bundle_hash,
            bundle_path=inside,
            require_current_subject=False,
        )
    assert inside_error.value.reason_code == "BLIND_BUNDLE_INSIDE_REPOSITORY"

    terminal_path = run_dir / "terminal_outputs.jsonl"
    terminal_path.write_bytes(terminal_path.read_bytes() + b"{}\n")
    with pytest.raises(P5BlindScoringError) as tamper_error:
        score_external_blind_run_group(
            repo_root=repo,
            run_dir=run_dir,
            expected_bundle_sha256=bundle_hash,
            bundle_path=bundle_path,
            require_current_subject=False,
        )
    assert tamper_error.value.reason_code == "BLIND_TERMINAL_FILE_HASH_MISMATCH"
