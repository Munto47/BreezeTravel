from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.contracts_v2 import (
    P5CaseV2,
    P5OracleV2,
    P5TerminalOutputV2,
    P5VariantRunSpecV2,
    TerminalStatusV2,
    VARIANT_IDS_V2,
)
from evals.trip_check_v1.p5.concurrency_materialization_v2 import build_concurrency_fault_script
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.scorer_v2 import (
    P5V2ScoringError,
    case_set_hash_v2,
    materialization_set_hash_v2,
    score_case_v2,
    score_run_group_v2,
    semantic_output_hash_v2,
    variant_output_hashes_v2,
)


def _oracle(**updates: object) -> P5OracleV2:
    payload = {
        "task_success_required": True,
        "requires_user_resolution": False,
        "required_reason_codes": ["TIME_CHAIN_CONFLICT"],
        "wrong_city_or_poi_max": 0,
        "max_new_blocker_high_unknown": 0,
        "unknown_must_be_preserved": True,
        "advice_required": True,
        "specific_place_allowed": True,
        "candidate_receipt_mode": "REQUIRED",
        "expected_strategy_outcome": "FEASIBLE",
        "concurrency_expectation": "IDEMPOTENT_REPLAY",
        "ocr_required": False,
    }
    payload.update(updates)
    return P5OracleV2.model_validate(payload)


def _candidate_artifact(case_id: str) -> dict:
    candidate_set = {
        "candidate_set_id": f"candidate-set-{case_id}",
        "candidates": [
            {
                "canonical_place_id": "poi-1",
                "display_name": "故宫博物院",
                "place_receipt_id": "place-receipt-1",
                "route_receipt_ids": ["route-receipt-1"],
            }
        ],
    }
    candidate_set["content_hash"] = digest(candidate_set)
    body = {
        "artifact_id": f"candidate-set-{case_id}",
        "schema_version": "trip-check-p5-candidate-set-v2",
        "candidate_set": candidate_set,
    }
    return {**body, "content_sha256": digest(body)}


def _fault_artifact(case_id: str) -> dict:
    script = build_concurrency_fault_script(
        case_id=case_id,
        workspace_id=f"workspace-{case_id}",
        repair_id=f"repair-{case_id}",
        base_revision=1,
        fault_profile_id="duplicate_apply",
    )
    body = {
        "artifact_id": f"fault-{case_id}",
        "schema_version": "trip-check-p5-fault-artifact-v2",
        "fault_profile_id": "duplicate_apply",
        "script": script,
    }
    return {**body, "content_sha256": digest(body)}


def _case(case_id: str = "p5.dev.bj.001", *, split: str = "dev") -> P5CaseV2:
    oracle = None if split == "frozen_blind" else _oracle()
    candidate_artifact = _candidate_artifact(case_id)
    fault_artifact = _fault_artifact(case_id)
    materialization_row = {
        "schema_version": "trip-check-p5-materialization-v2",
        "materialization_id": f"materialization-{case_id}",
        "case_id": case_id,
        "source_payload": {
            "artifact_id": "source",
            "schema_version": "source-v2",
            "content_sha256": "3" * 64,
        },
        "render_receipt": None,
        "ocr_baseline_receipt": None,
        "provider_snapshot": {
            "artifact_id": "provider",
            "schema_version": "provider-v2",
            "content_sha256": "4" * 64,
        },
        "evidence_snapshot": {
            "artifact_id": "evidence",
            "schema_version": "evidence-v2",
            "content_sha256": "5" * 64,
        },
        "candidate_sets": [candidate_artifact],
        "fault_script": fault_artifact,
        "receipts": [],
    }
    materialization_hash = digest(materialization_row)

    def binding(artifact: dict) -> dict:
        return {key: artifact[key] for key in ("artifact_id", "schema_version", "content_sha256")}

    materialization = {
        "schema_version": "trip-check-p5-materialization-binding-v2",
        "materialization_id": materialization_row["materialization_id"],
        "materialization_sha256": materialization_hash,
        "source_payload": materialization_row["source_payload"],
        "render_receipt": None,
        "ocr_baseline_receipt": None,
        "provider_snapshot": materialization_row["provider_snapshot"],
        "evidence_snapshot": materialization_row["evidence_snapshot"],
        "candidate_sets": [binding(candidate_artifact)],
        "fault_script": binding(fault_artifact),
    }
    payload = {
        "schema_version": "trip-check-p5-eval-case-v2",
        "case_id": case_id,
        "split": split,
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": "TEXT",
        "difficulty": "HARD",
        "coverage_tags": ["route", "unknown", "duplicate_apply"],
        "product_input": {"source_type": "MANUAL_TEXT", "raw_text": "北京2日"},
        "normalized_input_sha256": "1" * 64,
        "materialization": materialization,
        "runner_control": {"fault_profile_id": "duplicate_apply"},
        "lineage": {},
        "source_ref": {},
        "provenance": {},
        "oracle": oracle.model_dump(mode="json") if oracle else None,
        "oracle_sha256": digest(oracle.model_dump(mode="json")) if oracle else None,
    }
    payload["case_hash"] = digest(payload)
    return P5CaseV2.model_validate(payload)


def _materialization_row(case: P5CaseV2) -> dict:
    binding = case.materialization.model_dump(mode="json")
    row = {
        "schema_version": "trip-check-p5-materialization-v2",
        "materialization_id": binding["materialization_id"],
        "case_id": case.case_id,
        "source_payload": binding["source_payload"],
        "render_receipt": binding["render_receipt"],
        "ocr_baseline_receipt": binding["ocr_baseline_receipt"],
        "provider_snapshot": binding["provider_snapshot"],
        "evidence_snapshot": binding["evidence_snapshot"],
        "candidate_sets": [_candidate_artifact(case.case_id)],
        "fault_script": _fault_artifact(case.case_id),
        "receipts": [],
    }
    assert digest(row) == binding["materialization_sha256"]
    return {**row, "materialization_hash": binding["materialization_sha256"]}


def _spec(case: P5CaseV2, variant_id: str, dataset_hash: str) -> P5VariantRunSpecV2:
    versions = {
        "legacy_a": ("legacy-a-v2", "legacy_native_only"),
        "core_b": ("core-b-v2", "bounded_repair_v1"),
        "solver_c": ("solver-c-v2", "cp_sat_v1"),
    }
    adapter, strategy = versions[variant_id]
    return P5VariantRunSpecV2(
        subject_commit="a" * 40,
        dirty_tree=False,
        lane="nonblind" if case.split != "frozen_blind" else "frozen_blind",
        dataset_manifest_hash=dataset_hash,
        case_set_hash=case_set_hash_v2([case]),
        materialization_set_hash=materialization_set_hash_v2([case]),
        run_spec_template_hash="9" * 64,
        rubric_hash="b" * 64,
        renderer_version="renderer-v2",
        ocr_engine_version="ocr-v2",
        evidence_policy_version="evidence-v2",
        fault_registry_version="fault-v2",
        random_seed=7,
        budget={"timeout_seconds": 30},
        variant_id=variant_id,
        adapter_version=adapter,
        repair_strategy=strategy,
    )


def _concurrency_receipt(case: P5CaseV2) -> dict:
    script = _fault_artifact(case.case_id)["script"]
    attempts = [
        {key: attempt[key] for key in ("attempt_id", "ordinal", "repair_id", "idempotency_key")}
        for attempt in script["attempts"]
    ]
    projection = {
        "case_id": case.case_id,
        "fault_profile_id": "duplicate_apply",
        "script_sha256": script["script_sha256"],
        "outcome_counts": {"APPLIED": 1, "IDEMPOTENT_REPLAY": 1},
        "all_invariants_passed": True,
    }
    receipt = {
        "type": "concurrency",
        "schema_version": "trip-check-p5-apply-fault-receipt-v2",
        "status": "PASS",
        "case_id": case.case_id,
        "workspace_id": script["workspace_id"],
        "fault_profile_id": "duplicate_apply",
        "script_sha256": script["script_sha256"],
        "barrier": script["barrier"],
        "attempts": attempts,
        "side_effects": {},
        "error_categories": [],
        "semantic_projection": projection,
        "semantic_hash": digest(projection),
    }
    return {**receipt, "receipt_sha256": digest(receipt)}


def _output(case: P5CaseV2, spec: P5VariantRunSpecV2) -> P5TerminalOutputV2:
    payload = {
        "case_id": case.case_id,
        "split": case.split,
        "city": case.city,
        "input_kind": case.input_kind,
        "input_hash": case.normalized_input_sha256,
        "materialization_hash": case.materialization.materialization_sha256,
        "render_receipt_hash": None,
        "ocr_receipt_hash": None,
        "provider_snapshot_hash": case.materialization.provider_snapshot.content_sha256,
        "evidence_snapshot_hash": case.materialization.evidence_snapshot.content_sha256,
        "candidate_set_hashes": [case.materialization.candidate_sets[0].content_sha256],
        "fault_script_hash": case.materialization.fault_script.content_sha256,
        "run_spec_hash": spec.run_spec_hash,
        "variant_id": spec.variant_id,
        "adapter_version": spec.adapter_version,
        "repair_strategy": spec.repair_strategy,
        "terminal_status": TerminalStatusV2.SUCCEEDED,
        "capability_outcomes": {},
        "native_output": {
            "schema_version": "trip-check-p5-native-output-v2",
            "solver_strategy": {},
        },
        "evaluation_projection": {
            "schema_version": "trip-check-p5-evaluation-projection-v2",
            "import_status": "SUCCEEDED",
            "requires_user_resolution": False,
            "selected_place_ids": ["poi-1"],
            "wrong_city_or_poi_count": 0,
            "unknown_preserved": True,
            "candidate_receipt_coverage": 1.0,
            "replay_side_effect_counts_equal": True,
            "p4_solver_admission": "REJECT",
        },
        "findings": [{"reason_code": "TIME_CHAIN_CONFLICT", "status": "UNKNOWN"}],
        "advice": [{"finding_reason_code": "TIME_CHAIN_CONFLICT"}],
        "postcheck": {
            "schema_version": "trip-check-p5-postcheck-projection-v2",
            "report_id": "postcheck-1",
            "overall_status": "VIOLATED",
            "new_blocker_high_unknown_count": 0,
        },
        "receipts": [
            {"receipt_id": "place-receipt-1", "status": "SUCCEEDED"},
            {"receipt_id": "route-receipt-1", "status": "SUCCEEDED"},
            _concurrency_receipt(case),
        ],
        "latency_ms": 1.0,
        "token_count": 0,
        "cost_usd": 0,
        "error_category": None,
        "raw_artifact_hash": "c" * 64,
        "semantic_output_hash": "d" * 64,
        "replay_hash": "d" * 64,
    }
    output = P5TerminalOutputV2.model_validate(payload)
    semantic_hash = semantic_output_hash_v2(output)
    return output.model_copy(update={"semantic_output_hash": semantic_hash, "replay_hash": semantic_hash})


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )


def _write_run_group(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    case = _case()
    cases_path = tmp_path / "cases.jsonl"
    _write_jsonl(cases_path, [case.model_dump(mode="json")])
    materializations_path = tmp_path / "materializations.jsonl"
    materialization_rows = [_materialization_row(case)]
    _write_jsonl(
        materializations_path,
        materialization_rows,
    )
    dataset = {
        "schema_version": "trip-check-p5-dataset-manifest-v2",
        "files": {
            "nonblind_cases": {
                "path": cases_path.name,
                "row_count": 1,
                "file_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
                "content_sha256": digest([case.model_dump(mode="json")]),
            },
            "nonblind_materializations": {
                "path": materializations_path.name,
                "row_count": 1,
                "file_sha256": hashlib.sha256(materializations_path.read_bytes()).hexdigest(),
                "content_sha256": digest(materialization_rows),
            },
        },
        "lanes": {
            "nonblind": {
                "case_count": 1,
                "materialization_count": 1,
                "case_set_hash": case_set_hash_v2([case]),
                "materialization_set_hash": materialization_set_hash_v2([case]),
            }
        },
    }
    dataset["manifest_hash"] = digest(dataset)
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(dataset) + "\n", encoding="utf-8")
    specs = {variant_id: _spec(case, variant_id, dataset["manifest_hash"]) for variant_id in VARIANT_IDS_V2}
    outputs = [_output(case, specs[variant_id]) for variant_id in VARIANT_IDS_V2]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    terminal_path = run_dir / "terminal_outputs.jsonl"
    _write_jsonl(terminal_path, [item.model_dump(mode="json") for item in outputs])
    manifest = {
        "schema_version": "trip-check-p5-run-group-v2",
        "run_id": "test-v2",
        "status": "PASS",
        "formal_evidence": False,
        "lane": "nonblind",
        "subject_commit": "a" * 40,
        "dirty_tree": False,
        "dataset_manifest_hash": dataset["manifest_hash"],
        "cases_file_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "materializations_file_sha256": hashlib.sha256(materializations_path.read_bytes()).hexdigest(),
        "case_count": 1,
        "case_set_hash": case_set_hash_v2([case]),
        "materialization_set_hash": materialization_set_hash_v2([case]),
        "variant_ids": list(VARIANT_IDS_V2),
        "variant_count": 3,
        "terminal_count": 3,
        "expected_terminal_count": 3,
        "run_specs": {key: value.model_dump(mode="json") for key, value in specs.items()},
        "terminal_outputs_path": terminal_path.name,
        "terminal_outputs_file_sha256": hashlib.sha256(terminal_path.read_bytes()).hexdigest(),
        "terminal_outputs_content_sha256": digest([item.model_dump(mode="json") for item in outputs]),
        "variant_output_sha256": variant_output_hashes_v2(outputs),
        "replay_executed": True,
        "replay_match_count": 3,
        "replay_mismatches": [],
        "blind_labels_read": False,
        "external_api_calls": 0,
        "human_evidence": False,
    }
    manifest["manifest_hash"] = digest(manifest)
    (run_dir / "run_group_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")
    return run_dir, cases_path, materializations_path, dataset_path


def test_v2_case_score_passes_complete_deterministic_receipts() -> None:
    case = _case()
    spec = _spec(case, "core_b", "e" * 64)
    score = score_case_v2(case, _output(case, spec), materialization=_materialization_row(case))

    assert score.task_success is True
    assert score.deterministic_failure_codes == []
    assert score.candidate_receipt_coverage == "PASS"
    assert score.concurrency_result == "PASS"


def test_v2_unknown_unavailable_can_never_be_promoted_to_pass() -> None:
    case = _case()
    output = _output(case, _spec(case, "core_b", "e" * 64))
    payload = output.model_dump(mode="json")
    payload["findings"][0].update({"source_status": "UNAVAILABLE", "result_status": "PASS"})
    changed = P5TerminalOutputV2.model_validate(payload)
    semantic_hash = semantic_output_hash_v2(changed)
    changed = changed.model_copy(update={"semantic_output_hash": semantic_hash, "replay_hash": semantic_hash})

    score = score_case_v2(case, changed)

    assert score.task_success is False
    assert "UNKNOWN_OR_UNAVAILABLE_NOT_PRESERVED" in score.deterministic_failure_codes


def test_v2_exact_three_variant_group_is_hash_bound(tmp_path: Path) -> None:
    run_dir, cases_path, materializations_path, dataset_path = _write_run_group(tmp_path)
    report = score_run_group_v2(
        run_dir=run_dir,
        cases_path=cases_path,
        materializations_path=materializations_path,
        dataset_manifest_path=dataset_path,
        require_formal=False,
    )

    assert report["status"] == "PASS"
    assert report["terminal_count"] == 3
    assert len(report["case_scores"]) == 3

    manifest_path = run_dir / "run_group_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_specs"]["solver_c"]["budget"]["timeout_seconds"] = 31
    manifest["manifest_hash"] = digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    with pytest.raises(P5V2ScoringError, match="RUN_SPEC_VARIANT_WHITELIST_VIOLATION"):
        score_run_group_v2(
            run_dir=run_dir,
            cases_path=cases_path,
            materializations_path=materializations_path,
            dataset_manifest_path=dataset_path,
            require_formal=False,
        )


def test_v2_run_group_rejects_extra_manifest_field_before_scoring(tmp_path: Path) -> None:
    run_dir, cases_path, materializations_path, dataset_path = _write_run_group(tmp_path)
    manifest_path = run_dir / "run_group_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["case_scores"] = [{"case_id": "leak"}]
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(P5V2ScoringError, match="RUN_GROUP_MANIFEST_FIELDS_INVALID"):
        score_run_group_v2(
            run_dir=run_dir,
            cases_path=cases_path,
            materializations_path=materializations_path,
            dataset_manifest_path=dataset_path,
            require_formal=False,
        )
