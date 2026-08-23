from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, ValidationError

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.judge_v2 import (
    P5JudgeErrorV2,
    aggregate_judge_rounds_v2,
    export_judge_bundles_v2,
)
from scripts.aggregate_trip_check_p5_v2_judges import _validate_panel_schema


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    cases = []
    outputs = []
    materializations = []
    for index in range(90):
        screenshot = index < 45
        case_id = f"p5.blind.case-{index:03d}"
        product_input = (
            {
                "source_type": "SYNTHETIC_SCREENSHOT",
                "source_text": f"SECRET_RENDER_SOURCE_{index}",
            }
            if screenshot
            else {"source_type": "MANUAL_TEXT", "raw_text": f"public text {index}"}
        )
        cases.append(
            SimpleNamespace(
                case_id=case_id,
                input_kind="SYNTHETIC_SCREENSHOT" if screenshot else "TEXT",
                product_input=product_input,
            )
        )
        materializations.append(
            {
                "case_id": case_id,
                "ocr_baseline_receipt": (
                    {"lines": [{"text": f"actual ocr {index}"}]} if screenshot else None
                ),
                "evidence_snapshot": {
                    "snapshot": {"facts": [{"freshness_status": "FRESH"}]}
                },
            }
        )
        for variant_id in ("legacy_a", "core_b", "solver_c"):
            outputs.append(
                SimpleNamespace(
                    case_id=case_id,
                    variant_id=variant_id,
                    terminal_status=SimpleNamespace(value="SUCCEEDED"),
                    advice=[
                        {
                            "finding_reason": "time conflict",
                            "action": "confirm time",
                            "uncertainty": "route unavailable",
                            "has_repair": False,
                            "candidate_set_bound": False,
                        }
                    ],
                    evaluation_projection={"requires_user_resolution": True},
                    findings=[
                        {
                            "reason_code": "ROUTE_UNKNOWN",
                            "severity": "HIGH",
                            "status": "UNKNOWN",
                        }
                    ],
                    postcheck={"overall_status": "UNKNOWN", "new_unknown_count": 1},
                )
            )
    manifest = {
        "subject_commit": "a" * 40,
        "manifest_hash": "b" * 64,
        "terminal_outputs_file_sha256": "c" * 64,
        "terminal_outputs_content_sha256": "d" * 64,
        "variant_output_sha256": {
            "legacy_a": "1" * 64,
            "core_b": "2" * 64,
            "solver_c": "3" * 64,
        },
    }
    monkeypatch.setattr(
        "evals.trip_check_v1.p5.judge_v2.validate_run_group_v2",
        lambda **_: (manifest, cases, outputs),
    )
    materializations_path = tmp_path / "materializations.jsonl"
    materializations_path.write_text(
        "".join(json.dumps(row) + "\n" for row in materializations), encoding="utf-8"
    )
    rubric = {
        "schema_version": "trip-check-p5-judge-rubric-v2",
        "judge_class": "automated_proxy_judge",
        "fact_authority": "DETERMINISTIC_ORACLE_ONLY",
        "human_evidence": False,
        "human_calibration_performed": False,
        "input_policy": {
            "anonymous_variant": True,
            "blind_label_present": False,
            "deterministic_oracle_present": False,
            "evidence_summary_only": True,
        },
        "judge_may_decide": [
            "clarity",
            "actionability",
            "evidence_boundary_expression",
        ],
        "judge_must_not_decide": ["facts"],
        "dimensions": {
            "clarity": {"minimum": 0, "maximum": 4},
            "actionability": {"minimum": 0, "maximum": 4},
            "evidence_boundary_expression": {"minimum": 0, "maximum": 4},
        },
    }
    rubric_path = tmp_path / "rubric.json"
    rubric_path.write_text(json.dumps(rubric), encoding="utf-8")
    export_dir = tmp_path / "judge-export"
    receipt = export_judge_bundles_v2(
        repo_root=repo,
        run_dir=tmp_path / "run",
        cases_path=tmp_path / "cases.jsonl",
        materializations_path=materializations_path,
        dataset_manifest_path=tmp_path / "dataset.json",
        output_dir=export_dir,
        rubric_path=rubric_path,
    )
    assert receipt["items_per_round"] == 270
    return repo, export_dir, export_dir / "judge_variant_mapping.v2.json"


def _rounds(export_dir: Path) -> list[Path]:
    paths = []
    for round_index in range(1, 4):
        bundle_path = export_dir / f"judge_bundle_round_{round_index}.v2.json"
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        report = {
            "schema_version": "trip-check-p5-judge-round-v2",
            "round_index": round_index,
            "evaluator_id": f"evaluator-{round_index}",
            "agent_task_id": f"task-{round_index}",
            "agent_id": f"agent-{round_index}",
            "model_id": f"gpt-5.6-sol-judge-{round_index}",
            "started_at": f"2026-08-23T00:0{round_index}:00Z",
            "ended_at": f"2026-08-23T00:0{round_index}:30Z",
            "bundle_sha256": _sha(bundle_path),
            "rubric_sha256": bundle["run_binding"]["rubric_sha256"],
            "terminal_outputs_content_sha256": bundle["run_binding"][
                "terminal_outputs_content_sha256"
            ],
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
        path = export_dir / f"judge_round_{round_index}.v2.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        paths.append(path)
    return paths


def test_export_is_exact_balanced_and_leak_free(tmp_path: Path, monkeypatch) -> None:
    _, export_dir, mapping_path = _fixture(tmp_path, monkeypatch)
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert len(mapping["rows"]) == 810
    assert {tuple(row["claim_ids"]) for row in mapping["rows"]} == {("claim_001",)}
    for round_index in range(1, 4):
        rows = [row for row in mapping["rows"] if row["round_index"] == round_index]
        assert set(Counter((row["slot_id"], row["variant_id"]) for row in rows).values()) == {
            30
        }
        bundle_text = (export_dir / f"judge_bundle_round_{round_index}.v2.json").read_text(
            encoding="utf-8"
        )
        assert "variant_id" not in bundle_text
        assert "case_id" not in bundle_text
        assert '"oracle":' not in bundle_text
        assert '"label":' not in bundle_text
        assert "SECRET_RENDER_SOURCE" not in bundle_text
        assert "actual ocr 0" in bundle_text
        assert '"claim_id":"claim_001"' in bundle_text


def test_three_round_panel_enforces_identity_schema_and_zero_tools(tmp_path: Path, monkeypatch) -> None:
    repo, export_dir, mapping_path = _fixture(tmp_path, monkeypatch)
    rounds = _rounds(export_dir)
    panel = aggregate_judge_rounds_v2(
        repo_root=repo,
        mapping_path=mapping_path,
        mapping_sha256=_sha(mapping_path),
        round_paths=rounds,
    )
    assert panel["status"] == "PASS"
    assert panel["candidate_count"] == 270
    assert panel["unsupported_claim_candidate_count"] == 0
    assert panel["verdict_agreement_rate"] == 1
    assert panel["judge_may_override_deterministic_failure"] is False
    assert panel["report_hash"] == digest(
        {key: value for key, value in panel.items() if key != "report_hash"}
    )

    tampered = json.loads(rounds[1].read_text(encoding="utf-8"))
    tampered["unexpected"] = True
    rounds[1].write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(P5JudgeErrorV2, match="JUDGE_ROUND_CONTRACT_INVALID"):
        aggregate_judge_rounds_v2(
            repo_root=repo,
            mapping_path=mapping_path,
            mapping_sha256=_sha(mapping_path),
            round_paths=rounds,
        )


def test_panel_aggregates_explicit_unsupported_claim_ids_without_hiding_them(
    tmp_path: Path, monkeypatch
) -> None:
    repo, export_dir, mapping_path = _fixture(tmp_path, monkeypatch)
    rounds = _rounds(export_dir)
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    blind_item_id = mapping["rows"][0]["blind_item_id"]
    for round_index, path in enumerate(rounds, 1):
        mapping_row = next(
            row
            for row in mapping["rows"]
            if row["round_index"] == round_index
            and row["blind_item_id"] == blind_item_id
            and row["variant_id"] == "legacy_a"
        )
        report = json.loads(path.read_text(encoding="utf-8"))
        score = next(
            score
            for score in report["scores"]
            if score["blind_item_id"] == blind_item_id
            and score["slot_id"] == mapping_row["slot_id"]
        )
        score["unsupported_claim_candidate_ids"] = ["claim_001"]
        score["derived_verdict"] = "NEEDS_REVISION"
        path.write_text(json.dumps(report), encoding="utf-8")

    panel = aggregate_judge_rounds_v2(
        repo_root=repo,
        mapping_path=mapping_path,
        mapping_sha256=_sha(mapping_path),
        round_paths=rounds,
    )

    assert panel["status"] == "PASS"
    assert panel["unsupported_claim_candidate_count"] == 1
    assert panel["report_hash"] == digest(
        {key: value for key, value in panel.items() if key != "report_hash"}
    )


def test_round_missing_claim_field_and_anonymous_id_drift_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:
    repo, export_dir, mapping_path = _fixture(tmp_path, monkeypatch)
    rounds = _rounds(export_dir)
    report = json.loads(rounds[0].read_text(encoding="utf-8"))
    del report["scores"][0]["unsupported_claim_candidate_ids"]
    rounds[0].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(P5JudgeErrorV2, match="JUDGE_SCORE_SCHEMA_INVALID"):
        aggregate_judge_rounds_v2(
            repo_root=repo,
            mapping_path=mapping_path,
            mapping_sha256=_sha(mapping_path),
            round_paths=rounds,
        )

    rounds = _rounds(export_dir)
    report = json.loads(rounds[0].read_text(encoding="utf-8"))
    report["scores"][0]["blind_item_id"] = "unmapped-anonymous-id"
    rounds[0].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(P5JudgeErrorV2, match="JUDGE_SCORE_MAPPING_MISSING"):
        aggregate_judge_rounds_v2(
            repo_root=repo,
            mapping_path=mapping_path,
            mapping_sha256=_sha(mapping_path),
            round_paths=rounds,
        )

    rounds = _rounds(export_dir)
    report = json.loads(rounds[0].read_text(encoding="utf-8"))
    report["scores"][0]["unsupported_claim_candidate_ids"] = ["invented-claim-id"]
    report["scores"][0]["derived_verdict"] = "NEEDS_REVISION"
    rounds[0].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(P5JudgeErrorV2, match="JUDGE_CLAIM_ID_NOT_IN_CANDIDATE"):
        aggregate_judge_rounds_v2(
            repo_root=repo,
            mapping_path=mapping_path,
            mapping_sha256=_sha(mapping_path),
            round_paths=rounds,
        )


def test_mapping_must_be_a_closed_three_round_anonymous_bijection(
    tmp_path: Path, monkeypatch
) -> None:
    repo, export_dir, mapping_path = _fixture(tmp_path, monkeypatch)
    rounds = _rounds(export_dir)
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["rows"][0]["case_id"] = mapping["rows"][3]["case_id"]
    mapping["mapping_commitment"] = digest(mapping["rows"])
    mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

    with pytest.raises(P5JudgeErrorV2, match="JUDGE_ANONYMOUS_MAPPING_NOT_CLOSED"):
        aggregate_judge_rounds_v2(
            repo_root=repo,
            mapping_path=mapping_path,
            mapping_sha256=_sha(mapping_path),
            round_paths=rounds,
        )


def test_round_and_panel_schemas_reject_nested_extra_fields(
    tmp_path: Path, monkeypatch
) -> None:
    repo, export_dir, mapping_path = _fixture(tmp_path, monkeypatch)
    rounds = _rounds(export_dir)
    schema_root = Path(__file__).parents[1] / "evals" / "trip_check_v1" / "p5"
    round_schema = json.loads(
        (schema_root / "judge_round_v2.schema.json").read_text(encoding="utf-8")
    )
    round_payload = json.loads(rounds[0].read_text(encoding="utf-8"))
    Draft202012Validator(round_schema).validate(round_payload)
    round_payload["scores"][0]["unexpected"] = True
    with pytest.raises(ValidationError):
        Draft202012Validator(round_schema).validate(round_payload)

    panel = aggregate_judge_rounds_v2(
        repo_root=repo,
        mapping_path=mapping_path,
        mapping_sha256=_sha(mapping_path),
        round_paths=rounds,
    )
    panel_schema = json.loads(
        (schema_root / "judge_panel_v2.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(panel_schema).validate(panel)
    _validate_panel_schema(panel)
    panel["variant_metrics"]["legacy_a"]["unexpected"] = True
    with pytest.raises(ValidationError):
        Draft202012Validator(panel_schema).validate(panel)
    with pytest.raises(RuntimeError, match="Judge panel schema validation failed"):
        _validate_panel_schema(panel)


def test_agreement_below_85_blocks_only_semantic_panel(tmp_path: Path, monkeypatch) -> None:
    repo, export_dir, mapping_path = _fixture(tmp_path, monkeypatch)
    rounds = _rounds(export_dir)
    third = json.loads(rounds[2].read_text(encoding="utf-8"))
    for score in third["scores"][:46]:
        score["clarity"] = 1
        score["actionability"] = 1
        score["evidence_boundary_expression"] = 1
        score["derived_verdict"] = "NEEDS_REVISION"
    rounds[2].write_text(json.dumps(third), encoding="utf-8")

    panel = aggregate_judge_rounds_v2(
        repo_root=repo,
        mapping_path=mapping_path,
        mapping_sha256=_sha(mapping_path),
        round_paths=rounds,
    )
    assert panel["status"] == "BLOCKED"
    assert panel["verdict_agreement_rate"] < 0.85
    assert panel["deterministic_scorer_priority"] is True
