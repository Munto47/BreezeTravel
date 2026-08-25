from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.judge_v5 import (
    P5JudgeErrorV5,
    _evidence_summary,
    aggregate_judge_rounds_v5,
    export_judge_bundles_v5,
)


HEX64 = "a" * 64
SUBJECT = "b" * 40
UPSTREAM_REF = "origin/codex/p5-judge-test"
SOURCE_P5 = Path(__file__).parents[1] / "evals" / "trip_check_v1" / "p5"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _rubric() -> dict[str, object]:
    dimensions = {
        name: {"description": name, "minimum": 0, "maximum": 4}
        for name in (
            "clarity",
            "actionability",
            "evidence_boundary_expression",
        )
    }
    return {
        "schema_version": "trip-check-p5-judge-rubric-v2",
        "judge_class": "automated_proxy_judge",
        "fact_authority": "DETERMINISTIC_ORACLE_ONLY",
        "human_evidence": False,
        "human_calibration_performed": False,
        "input_policy": {},
        "judge_may_decide": list(dimensions),
        "judge_must_not_decide": ["place_or_city_correctness"],
        "dimensions": dimensions,
    }


def _protocol() -> dict[str, object]:
    dimensions = (
        "clarity",
        "actionability",
        "evidence_boundary_expression",
    )
    return {
        "schema_version": "trip-check-p5-judge-protocol-v1",
        "rubric_schema_version": "trip-check-p5-judge-rubric-v2",
        "judge_class": "automated_proxy_judge",
        "fact_authority": "DETERMINISTIC_SCORER_ONLY",
        "human_evidence": False,
        "human_calibration_performed": False,
        "structured_expression_fields": [
            "finding_reason",
            "action",
            "uncertainty",
            "has_repair",
            "candidate_set_bound",
        ],
        "dimension_anchors": {
            dimension: {str(score): f"{dimension}-{score}" for score in range(5)}
            for dimension in dimensions
        },
        "evaluator_instruction": ["Apply every anchor to every item."],
        "verdict_rule": {
            "minimum_dimension_score": 2,
            "unsupported_claim_candidate_count": 0,
        },
        "agreement_rule": {
            "dimension_max_spread": 1,
            "minimum_rate": 0.85,
            "required_dimensions": list(dimensions),
            "verdict_unanimity_required_rate": 0.85,
        },
        "preblind_calibration": {
            "minimum_dimension_agreement_rate": 0.85,
            "minimum_verdict_agreement_rate": 0.85,
            "required": True,
            "source_lane": "NONBLIND_SYNTHETIC_ANCHORS",
        },
    }


def _validated_run() -> tuple[dict[str, object], list[dict], list[dict], dict]:
    manifest = {
        "status": "PASS",
        "formal_evidence": True,
        "lane": "frozen_blind",
        "subject_commit": SUBJECT,
        "upstream_ref": UPSTREAM_REF,
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
        "dataset_manifest_hash": HEX64,
        "manifest_hash": "c" * 64,
        "artifact_index_hash": "d" * 64,
        "terminal_outputs_file_sha256": "e" * 64,
        "terminal_outputs_content_sha256": "f" * 64,
        "run_spec_template_sha256": "1" * 64,
        "case_count": 90,
        "terminal_count": 270,
        "replay_executed": True,
        "replay_readback_count": 270,
        "replay_mismatches": [],
        "blind_labels_read": False,
    }
    cases = [
        {
            "case_id": f"p5.blind.case-{index:03d}",
            "input_kind": "TEXT",
            "product_input": {
                "source_type": "PASTED_TEXT",
                "raw_text": f"第 {index} 个匿名行程",
            },
        }
        for index in range(90)
    ]
    outputs = []
    for case in cases:
        for variant_id in ("legacy_a", "core_b", "solver_c"):
            outputs.append(
                {
                    "case_id": case["case_id"],
                    "variant_id": variant_id,
                    "terminal_status": "SUCCEEDED",
                    "advice": [
                        {
                            "finding_reason": "ROUTE_GAP",
                            "action": "增加换乘时间",
                            "uncertainty": "LOW",
                            "has_repair": True,
                            "candidate_set_bound": True,
                        }
                    ],
                    "evaluation_projection": {"requires_user_resolution": False},
                    "findings": [
                        {
                            "reason_code": "ROUTE_GAP",
                            "severity": "HIGH",
                            "status": "VIOLATED",
                        }
                    ],
                    "postcheck": (
                        None
                        if variant_id == "legacy_a"
                        else {
                            "overall_status": "PASS",
                            "new_high_count": 0,
                            "new_unknown_count": 0,
                            "replay_side_effect_counts_equal": True,
                            "private_field": "not-exported",
                        }
                    ),
                }
            )
    materializations = {
        case["case_id"]: {
            "case_id": case["case_id"],
            "evidence_snapshot": {
                "snapshot": {
                    "facts": [{"freshness_status": "FRESH"}],
                }
            },
        }
        for case in cases
    }
    return manifest, cases, outputs, materializations


def _export(tmp_path: Path) -> tuple[Path, dict[str, object], list[Path]]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    rubric_path = repo_root / "rubric.json"
    _write_json(rubric_path, _rubric())
    protocol_path = repo_root / "protocol.json"
    _write_json(protocol_path, _protocol())
    calibration_panel_path = tmp_path / "calibration" / "panel.json"
    calibration_panel = {
        "report_hash": "9" * 64,
        "subject_commit": SUBJECT,
        "upstream_ref": UPSTREAM_REF,
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
    }
    _write_json(calibration_panel_path, calibration_panel)
    round_dirs = [tmp_path / f"judge-{index}" for index in range(1, 4)]
    custody = tmp_path / "custody"
    receipt = export_judge_bundles_v5(
        repo_root=repo_root,
        run_dir=tmp_path / "run",
        round_output_dirs=round_dirs,
        custody_output_dir=custody,
        rubric_path=rubric_path,
        protocol_path=protocol_path,
        calibration_panel_path=calibration_panel_path,
        blind_run_validator=lambda **_: _validated_run(),
        calibration_panel_validator=lambda **_: calibration_panel,
    )
    return repo_root, receipt, round_dirs


def _export_v2(
    tmp_path: Path, version: str = "v2"
) -> tuple[Path, dict[str, object], list[Path]]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    rubric_path = repo_root / "rubric.json"
    _write_json(rubric_path, _rubric())
    protocol_path = repo_root / "protocol.json"
    protocol_path.write_bytes(
        (SOURCE_P5 / f"judge_protocol_{version}.json").read_bytes()
    )
    commitment_path = repo_root / "holdout-commitment.json"
    _write_json(commitment_path, {"status": "SEALED"})
    holdout_panel_path = tmp_path / "holdout" / "panel.json"
    holdout_panel = {
        "report_hash": "9" * 64,
        "subject_commit": SUBJECT,
        "upstream_ref": UPSTREAM_REF,
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
    }
    _write_json(holdout_panel_path, holdout_panel)
    round_dirs = [tmp_path / f"judge-{index}" for index in range(1, 4)]
    custody = tmp_path / "custody"
    receipt = export_judge_bundles_v5(
        repo_root=repo_root,
        run_dir=tmp_path / "run",
        round_output_dirs=round_dirs,
        custody_output_dir=custody,
        rubric_path=rubric_path,
        protocol_path=protocol_path,
        calibration_panel_path=holdout_panel_path,
        holdout_commitment_path=commitment_path,
        blind_run_validator=lambda **_: _validated_run(),
        calibration_panel_validator=lambda **_: holdout_panel,
    )
    return repo_root, receipt, round_dirs


def _round_report(round_index: int, bundle_receipt: dict[str, object]) -> dict:
    bundle_dir = Path(str(bundle_receipt["test_bundle_dir"]))
    bundle_path = bundle_dir / str(bundle_receipt["path"])
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    scores = [
        {
            "anonymous_item_id": item["anonymous_item_id"],
            "slot_id": item["slot_id"],
            "clarity": 4,
            "actionability": 4,
            "evidence_boundary_expression": 4,
            "unsupported_claim_candidate_ids": [],
            "derived_verdict": "PASS",
        }
        for item in bundle["items"]
    ]
    report = {
        "schema_version": "trip-check-p5-judge-round-v5",
        "round_index": round_index,
        "evaluator_id": f"evaluator-{round_index}",
        "agent_task_id": f"task-{round_index}",
        "agent_id": f"agent-{round_index}",
        "context_id": f"context-{round_index}",
        "model_id": bundle_receipt.get("model_id", "gpt-test"),
        "started_at": f"2026-08-24T00:0{round_index}:00Z",
        "ended_at": f"2026-08-24T00:0{round_index}:30Z",
        "bundle_sha256": bundle_receipt["sha256"],
        "source_rubric_sha256": bundle_receipt["source_rubric_sha256"],
        "judge_input_rubric_sha256": bundle_receipt[
            "judge_input_rubric_sha256"
        ],
        "source_protocol_sha256": bundle_receipt["source_protocol_sha256"],
        "judge_input_protocol_sha256": bundle_receipt[
            "judge_input_protocol_sha256"
        ],
        "calibration_panel_sha256": bundle_receipt[
            "calibration_panel_sha256"
        ],
        "calibration_panel_report_hash": bundle_receipt[
            "calibration_panel_report_hash"
        ],
        "terminal_outputs_content_sha256": bundle_receipt[
            "terminal_outputs_content_sha256"
        ],
        "api_usage_count": 0,
        "tool_usage_count": 0,
        "automated_proxy_judge": True,
        "human_calibration_performed": False,
        "identity_payload_observed": False,
        "expected_answer_payload_observed": False,
        "custodian_metadata_observed": False,
        "peer_round_output_observed": False,
        "scores": scores,
    }
    if "evaluator_profile_id" in bundle_receipt:
        report.update(
            {
                "evaluator_profile_id": bundle_receipt["evaluator_profile_id"],
                "reasoning_effort": bundle_receipt["reasoning_effort"],
                "formal_attempt_index": bundle_receipt["formal_attempt_index"],
                "formal_attempt_id": bundle_receipt["formal_attempt_id"],
            }
        )
    return report


def _round_paths(
    tmp_path: Path, receipt: dict[str, object], round_dirs: list[Path]
) -> list[Path]:
    paths = []
    for round_index, raw_receipt in enumerate(receipt["bundle_receipts"], 1):
        bundle_receipt = dict(raw_receipt)
        bundle_receipt["test_bundle_dir"] = str(round_dirs[round_index - 1])
        report = _round_report(round_index, bundle_receipt)
        path = tmp_path / f"result-{round_index}" / "round.json"
        _write_json(path, report)
        paths.append(path)
    return paths


def test_v5_export_is_anonymous_and_aggregates_three_independent_rounds(
    tmp_path: Path,
) -> None:
    repo_root, receipt, round_dirs = _export(tmp_path)
    assert receipt["round_count"] == 3
    assert receipt["peer_round_output_exported"] is False
    for round_index, round_dir in enumerate(round_dirs, 1):
        bundle_path = round_dir / f"judge_input_round_{round_index}.v5.json"
        payload = bundle_path.read_text(encoding="utf-8").lower()
        assert len(json.loads(payload)["items"]) == 270
        assert json.loads(payload)["protocol"]["verdict_rule"] == {
            "minimum_dimension_score": 2,
            "unsupported_claim_candidate_count": 0,
        }
        for forbidden in (
            '"case_id"',
            '"variant_id"',
            '"oracle"',
            '"blind_label"',
            "legacy_a",
            "core_b",
            "solver_c",
        ):
            assert forbidden not in payload

    mapping_path = tmp_path / "custody" / "judge_variant_mapping.v5.json"
    round_paths = _round_paths(tmp_path, receipt, round_dirs)
    panel = aggregate_judge_rounds_v5(
        repo_root=repo_root,
        mapping_path=mapping_path,
        mapping_sha256=hashlib.sha256(mapping_path.read_bytes()).hexdigest(),
        round_paths=round_paths,
    )
    assert panel["status"] == "PASS"
    assert panel["automated_proxy_judge"] is True
    assert panel["human_calibration_performed"] is False
    assert panel["verdict_agreement_rate"] == 1.0
    assert len(panel["provenance"]) == 3


def test_v5_v2_export_binds_slots_and_one_formal_attempt(tmp_path: Path) -> None:
    repo_root, receipt, round_dirs = _export_v2(tmp_path)
    assert receipt["formal_attempt_index"] == 1
    assert len(receipt["formal_attempt_id"]) == 64
    for round_index, (bundle_receipt, round_dir) in enumerate(
        zip(receipt["bundle_receipts"], round_dirs, strict=True), 1
    ):
        bundle = json.loads(
            (round_dir / f"judge_input_round_{round_index}.v5.json").read_text(
                encoding="utf-8"
            )
        )
        assert bundle["evaluator_slot"]["round_index"] == round_index
        assert bundle["evaluator_slot"]["model_id"] == bundle_receipt["model_id"]
        assert bundle["formal_attempt_id"] == receipt["formal_attempt_id"]
    mapping_path = tmp_path / "custody" / "judge_variant_mapping.v5.json"
    panel = aggregate_judge_rounds_v5(
        repo_root=repo_root,
        mapping_path=mapping_path,
        mapping_sha256=hashlib.sha256(mapping_path.read_bytes()).hexdigest(),
        round_paths=_round_paths(tmp_path, receipt, round_dirs),
    )
    assert panel["status"] == "PASS"
    assert panel["formal_attempt_index"] == 1
    assert {item["model_id"] for item in panel["provenance"]} == {
        "gpt-5.4",
        "gpt-5.5",
        "gpt-5.6-sol",
    }


def test_v5_v2_rejects_formal_model_substitution(tmp_path: Path) -> None:
    repo_root, receipt, round_dirs = _export_v2(tmp_path)
    round_paths = _round_paths(tmp_path, receipt, round_dirs)
    report = json.loads(round_paths[1].read_text(encoding="utf-8"))
    report["model_id"] = "substituted-model"
    _write_json(round_paths[1], report)
    mapping_path = tmp_path / "custody" / "judge_variant_mapping.v5.json"
    with pytest.raises(P5JudgeErrorV5, match="JUDGE_ROUND_CONTRACT_INVALID"):
        aggregate_judge_rounds_v5(
            repo_root=repo_root,
            mapping_path=mapping_path,
            mapping_sha256=hashlib.sha256(mapping_path.read_bytes()).hexdigest(),
            round_paths=round_paths,
        )


def test_v5_v3_export_binds_v3_rubric_protocol_and_slots(tmp_path: Path) -> None:
    repo_root, receipt, round_dirs = _export_v2(tmp_path, "v3")
    bundle = json.loads(
        (round_dirs[0] / "judge_input_round_1.v5.json").read_text(
            encoding="utf-8"
        )
    )
    assert bundle["rubric"]["schema_version"] == (
        "trip-check-p5-judge-rubric-projection-v5"
    )
    assert bundle["protocol"]["schema_version"] == (
        "trip-check-p5-judge-protocol-projection-v3"
    )
    assert set(
        bundle["protocol"]["clarity_decision_tree"]["component_rules"]
    ) == {"consequence", "intended_response", "relevant_scope"}
    assert bundle["evaluator_slot"]["evaluator_profile_id"] == (
        "p5-judge-v3-slot-1"
    )
    mapping_path = tmp_path / "custody" / "judge_variant_mapping.v5.json"
    panel = aggregate_judge_rounds_v5(
        repo_root=repo_root,
        mapping_path=mapping_path,
        mapping_sha256=hashlib.sha256(mapping_path.read_bytes()).hexdigest(),
        round_paths=_round_paths(tmp_path, receipt, round_dirs),
    )
    assert panel["status"] == "PASS"


@pytest.mark.parametrize(
    "terminal_status",
    ("SUCCEEDED", "NEEDS_USER_RESOLUTION", "ERROR", "TIMEOUT", "UNSUPPORTED_CAPABILITY"),
)
def test_v5_evidence_summary_accepts_contractual_absent_postcheck(
    terminal_status: str,
) -> None:
    summary = _evidence_summary(
        {"terminal_status": terminal_status, "findings": [], "postcheck": None},
        {},
    )
    assert summary["postcheck_boundary"] == {"availability": "NOT_PRESENT"}


def test_v5_evidence_summary_keeps_postcheck_projection_bounded() -> None:
    summary = _evidence_summary(
        {
            "findings": [],
            "postcheck": {
                "overall_status": "PASS",
                "new_high_count": 0,
                "new_unknown_count": 0,
                "replay_side_effect_counts_equal": True,
                "private_field": "not-exported",
            },
        },
        {},
    )
    assert summary["postcheck_boundary"] == {
        "overall_status": "PASS",
        "new_high_count": 0,
        "new_unknown_count": 0,
        "replay_side_effect_counts_equal": True,
    }


@pytest.mark.parametrize("postcheck", ([], "invalid", 1))
def test_v5_evidence_summary_rejects_invalid_postcheck_shape(postcheck: object) -> None:
    with pytest.raises(P5JudgeErrorV5, match="JUDGE_EVIDENCE_SUMMARY_INVALID"):
        _evidence_summary({"findings": [], "postcheck": postcheck}, {})


@pytest.mark.parametrize("findings", (None, {}, "invalid"))
def test_v5_evidence_summary_rejects_invalid_findings_shape(findings: object) -> None:
    with pytest.raises(P5JudgeErrorV5, match="JUDGE_EVIDENCE_SUMMARY_INVALID"):
        _evidence_summary({"findings": findings, "postcheck": None}, {})


def test_v5_aggregation_rejects_shared_round_context(tmp_path: Path) -> None:
    repo_root, receipt, round_dirs = _export(tmp_path)
    mapping_path = tmp_path / "custody" / "judge_variant_mapping.v5.json"
    round_paths = _round_paths(tmp_path, receipt, round_dirs)
    second = json.loads(round_paths[1].read_text(encoding="utf-8"))
    second["context_id"] = "context-1"
    _write_json(round_paths[1], second)
    with pytest.raises(P5JudgeErrorV5, match="JUDGE_ROUND_INDEPENDENCE_INVALID"):
        aggregate_judge_rounds_v5(
            repo_root=repo_root,
            mapping_path=mapping_path,
            mapping_sha256=hashlib.sha256(mapping_path.read_bytes()).hexdigest(),
            round_paths=round_paths,
        )


def test_v5_aggregation_rejects_any_observed_peer_result(tmp_path: Path) -> None:
    repo_root, receipt, round_dirs = _export(tmp_path)
    mapping_path = tmp_path / "custody" / "judge_variant_mapping.v5.json"
    round_paths = _round_paths(tmp_path, receipt, round_dirs)
    first = json.loads(round_paths[0].read_text(encoding="utf-8"))
    first["peer_round_output_observed"] = True
    _write_json(round_paths[0], first)
    with pytest.raises(P5JudgeErrorV5, match="JUDGE_ROUND_CONTRACT_INVALID"):
        aggregate_judge_rounds_v5(
            repo_root=repo_root,
            mapping_path=mapping_path,
            mapping_sha256=hashlib.sha256(mapping_path.read_bytes()).hexdigest(),
            round_paths=round_paths,
        )


def test_v5_aggregation_rejects_protocol_provenance_substitution(
    tmp_path: Path,
) -> None:
    repo_root, receipt, round_dirs = _export(tmp_path)
    mapping_path = tmp_path / "custody" / "judge_variant_mapping.v5.json"
    round_paths = _round_paths(tmp_path, receipt, round_dirs)
    first = json.loads(round_paths[0].read_text(encoding="utf-8"))
    first["judge_input_protocol_sha256"] = "0" * 64
    _write_json(round_paths[0], first)
    with pytest.raises(P5JudgeErrorV5, match="JUDGE_ROUND_CONTRACT_INVALID"):
        aggregate_judge_rounds_v5(
            repo_root=repo_root,
            mapping_path=mapping_path,
            mapping_sha256=hashlib.sha256(mapping_path.read_bytes()).hexdigest(),
            round_paths=round_paths,
        )


def test_v5_export_rejects_calibration_subject_substitution(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    rubric_path = repo_root / "rubric.json"
    protocol_path = repo_root / "protocol.json"
    _write_json(rubric_path, _rubric())
    _write_json(protocol_path, _protocol())
    calibration_panel_path = tmp_path / "calibration" / "panel.json"
    calibration_panel = {
        "report_hash": "9" * 64,
        "subject_commit": "c" * 40,
        "upstream_ref": UPSTREAM_REF,
        "upstream_commit": "c" * 40,
        "dirty_tree": False,
    }
    _write_json(calibration_panel_path, calibration_panel)
    with pytest.raises(P5JudgeErrorV5, match="JUDGE_CALIBRATION_PANEL_INVALID"):
        export_judge_bundles_v5(
            repo_root=repo_root,
            run_dir=tmp_path / "run",
            round_output_dirs=[
                tmp_path / f"judge-{index}" for index in range(1, 4)
            ],
            custody_output_dir=tmp_path / "custody",
            rubric_path=rubric_path,
            protocol_path=protocol_path,
            calibration_panel_path=calibration_panel_path,
            blind_run_validator=lambda **_: _validated_run(),
            calibration_panel_validator=lambda **_: calibration_panel,
        )
