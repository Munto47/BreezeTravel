from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from evals.trip_check_v1.p5 import nonblind_scorer_v5 as scorer
from evals.trip_check_v1.p5 import runner_v4, runner_v5
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.gate_v5 import parse_nonblind_score_v5
from evals.trip_check_v1.p5.runner_v5 import (
    P5TerminalOutputV5,
    VARIANT_IDS_V5,
    semantic_output_hash_v5,
)
from evals.trip_check_v1.p5.scorer_v3 import semantic_output_hash_v3


SUBJECT = "a" * 40
HEX64 = "b" * 64


def _terminal_v5() -> P5TerminalOutputV5:
    payload: dict[str, Any] = {
        "schema_version": "trip-check-p5-terminal-output-v5",
        "case_id": "p5.pilot.bj.001",
        "split": "pilot",
        "city": "北京",
        "input_kind": "TEXT",
        "input_hash": "1" * 64,
        "materialization_hash": "2" * 64,
        "render_receipt_hash": None,
        "ocr_receipt_hash": None,
        "provider_snapshot_hash": "3" * 64,
        "evidence_snapshot_hash": "4" * 64,
        "candidate_set_hashes": ["5" * 64],
        "fault_script_hash": "6" * 64,
        "run_spec_hash": "7" * 64,
        "variant_id": "core_b",
        "adapter_version": "core-b-v4",
        "repair_strategy": "bounded_repair_v1",
        "terminal_status": "SUCCEEDED",
        "capability_outcomes": {
            "authoritative_oracle_access": "DENIED",
            "blind_label_access": "DENIED",
            "external_api_calls": "0",
        },
        "native_output": {},
        "evaluation_projection": {"replay_side_effect_counts_equal": True},
        "findings": [],
        "advice": [],
        "postcheck": None,
        "receipts": [],
        "latency_ms": 0.0,
        "token_count": 0,
        "cost_usd": 0.0,
        "error_category": None,
        "raw_artifact_hash": "8" * 64,
    }
    semantic_hash = semantic_output_hash_v5(payload)
    payload["semantic_output_hash"] = semantic_hash
    payload["replay_hash"] = semantic_hash
    return P5TerminalOutputV5.model_validate(payload)


def test_v5_semantic_projection_only_versions_the_hash_domain() -> None:
    terminal = _terminal_v5().model_dump(mode="json")
    v5_projection = runner_v5._semantic_payload_v5(terminal)
    v4_projection = runner_v4._semantic_payload_v4(terminal)

    assert set(v5_projection) == set(v4_projection)
    assert {
        key: value
        for key, value in v5_projection.items()
        if key != "replay_hash_policy"
    } == {
        key: value
        for key, value in v4_projection.items()
        if key != "replay_hash_policy"
    }
    assert v4_projection["replay_hash_policy"] == "p5-semantic-projection-v4"
    assert v5_projection["replay_hash_policy"] == "p5-semantic-projection-v5"


def _manifest(*, formal: bool) -> dict[str, Any]:
    payload = {
        "schema_version": "trip-check-p5-run-group-v5",
        "status": "PASS",
        "formal_evidence": formal,
        "lane": "nonblind",
        "subject_commit": SUBJECT,
        "upstream_ref": "origin/codex/p5-v5",
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
        "dataset_id": "trip-check-p5-360-v5",
        "dataset_manifest_hash": "1" * 64,
        "artifact_index_hash": "2" * 64,
        "run_spec_template_sha256": "3" * 64,
        "rubric_sha256": "4" * 64,
        "terminal_outputs_file_sha256": "5" * 64,
        "terminal_outputs_content_sha256": "6" * 64,
        "case_count": 270,
        "terminal_count": 810,
        "replay_readback_count": 810,
        "variant_ids": list(VARIANT_IDS_V5),
    }
    return {**payload, "manifest_hash": digest(payload)}


def _case_score(index: int) -> dict[str, Any]:
    variant_id = VARIANT_IDS_V5[index % len(VARIANT_IDS_V5)]
    return {
        "schema_version": "trip-check-p5-score-v3",
        "case_id": f"case-{index // 3:03d}",
        "split": "dev",
        "city": "北京",
        "input_kind": "TEXT",
        "difficulty": "CLEAN",
        "fault_profile_id": "NONE",
        "variant_id": variant_id,
        "terminal_status": "SUCCEEDED",
        "task_success": True,
        "deterministic_pass": True,
        "score": 100.0,
        "terminal_ok": True,
        "resolution_match": True,
        "required_reason_codes": [],
        "missing_reason_codes": [],
        "wrong_city_or_poi_count": 0,
        "unknown_preservation": "NOT_REQUIRED",
        "advice_coverage": "NOT_REQUIRED",
        "nonpass_finding_count": 0,
        "covered_nonpass_finding_count": 0,
        "unsupported_claim_count": 0,
        "candidate_receipt_coverage": "NOT_REQUIRED",
        "concurrency_result": "NOT_REQUIRED",
        "repair_postcheck": "NOT_REQUIRED",
        "replay_hash_match": True,
        "strategy_outcome_match": True,
        "ocr_receipt_result": "NOT_REQUIRED",
        "token_count": 0,
        "cost_usd": 0.0,
        "usage_measurement": "PASS",
        "deterministic_failure_codes": [],
    }


def _base_report() -> dict[str, Any]:
    return {
        "schema_version": "trip-check-p5-nonblind-score-report-v3",
        "status": "PASS",
        "variant_metrics": {
            variant_id: {"overall": {"case_count": 270}}
            for variant_id in VARIANT_IDS_V5
        },
        "paired_comparisons": {"legacy_a": {}, "solver_c": {}},
        "zero_tolerance_checks": {"deterministic_failure_zero": True},
        "stage_gate_checks": {"pilot_18_of_18": True},
        "promotion_decision": "KEEP_CORE_B",
        "solver_admission_inherited": "REJECT",
        "solver_may_promote_from_p5_score": False,
        "evidence_boundary": {},
        "case_scores": [_case_score(index) for index in range(810)],
    }


def _validated_shape() -> tuple[list[Any], list[Any], dict[str, dict[str, Any]]]:
    cases = [SimpleNamespace(case_id=f"case-{index:03d}") for index in range(270)]
    outputs = [
        SimpleNamespace(case_id=case.case_id, variant_id=variant_id)
        for case in cases
        for variant_id in VARIANT_IDS_V5
    ]
    materializations = {case.case_id: {} for case in cases}
    return cases, outputs, materializations


def test_terminal_projection_rebinds_to_v3_semantic_hash_domain() -> None:
    terminal_v5 = _terminal_v5()
    projected = scorer._project_terminal_to_v3(terminal_v5)

    assert projected.schema_version == "trip-check-p5-terminal-output-v3"
    assert projected.semantic_output_hash == semantic_output_hash_v3(projected)
    assert projected.replay_hash == projected.semantic_output_hash
    assert projected.semantic_output_hash != terminal_v5.semantic_output_hash


def test_formal_report_is_v5_hash_bound_and_gate_compatible(monkeypatch) -> None:
    cases, outputs, materializations = _validated_shape()
    monkeypatch.setattr(scorer, "_project_terminal_to_v3", lambda output: output)
    monkeypatch.setattr(scorer._scorer_v3, "build_score_report_v3", lambda **_: _base_report())
    manifest = _manifest(formal=True)

    report = scorer.build_nonblind_score_report_v5(
        manifest=manifest,
        cases=cases,
        outputs=outputs,
        materializations=materializations,
        formal_validation_performed=True,
    )

    assert report["status"] == "PASS"
    assert report["promotion_decision"] == "KEEP_CORE_B"
    assert report["solver_admission_inherited"] == "REJECT"
    assert report["solver_may_promote_from_p5_score"] is False
    assert len(report["case_scores"]) == 810
    assert {item["schema_version"] for item in report["case_scores"]} == {
        "trip-check-p5-case-score-v5"
    }
    assert report["report_hash"] == digest(
        {key: value for key, value in report.items() if key != "report_hash"}
    )
    assert parse_nonblind_score_v5(report, run=manifest)["status"] == "PASS"


def test_development_report_always_rejects_even_when_metrics_pass(monkeypatch) -> None:
    cases, outputs, materializations = _validated_shape()
    monkeypatch.setattr(scorer, "_project_terminal_to_v3", lambda output: output)
    monkeypatch.setattr(scorer._scorer_v3, "build_score_report_v3", lambda **_: _base_report())

    report = scorer.build_nonblind_score_report_v5(
        manifest=_manifest(formal=False),
        cases=cases,
        outputs=outputs,
        materializations=materializations,
        formal_validation_performed=False,
    )

    assert report["status"] == "REJECT"
    assert report["development_only"] is True
    assert report["promotion_decision"] == "REJECT_ALL_CANDIDATES"
    assert report["evidence_boundary"][
        "formal_v5_active_seal_git_artifact_validation"
    ] == "DIAGNOSTIC_ONLY"


def test_score_entrypoint_calls_runner_validator_before_build(monkeypatch) -> None:
    calls: list[bool] = []

    def validator(**kwargs):
        calls.append(kwargs["require_formal"])
        return ({"validated": True}, [], [], {})

    monkeypatch.setattr(
        scorer,
        "build_nonblind_score_report_v5",
        lambda **kwargs: {"status": "PASS", "manifest": kwargs["manifest"]},
    )
    report = scorer.score_nonblind_run_group_v5(
        run_dir="external-run",
        repo_root="repo",
        require_formal=True,
        run_validator=validator,
    )

    assert calls == [True]
    assert report == {"status": "PASS", "manifest": {"validated": True}}
