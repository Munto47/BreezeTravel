import copy
import json
from pathlib import Path

import pytest

from scripts.agent_judge_panel import (
    SCORE_NAMES,
    aggregate_panel,
    export_blind_bundle,
    validate_round,
)


DATASET = Path(__file__).resolve().parents[1] / "eval_data" / "daily_queries" / "cases.json"


def _source_report(tmp_path: Path) -> Path:
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    rows = []
    for case in dataset["cases"]:
        rows.append({
            "id": case["id"],
            "city": case["city"],
            "intent": case["intent"],
            "persona": case["persona"],
            "dimensions": case.get("dimensions") or [],
            "query": case["query"],
            "passed": True,
            "evaluation_status": "judge_skipped",
            "deterministic": {
                "passed": True,
                "failures": [],
                "retrieval_integrity": {},
                "high_risk_honesty": {
                    "pending_place_count": 0,
                    "pending_with_action_count": 0,
                    "confirmation_action_coverage": 1.0,
                    "unsupported_affirmative_claim_count": 0,
                },
            },
            "judge": {"passed": True, "skipped": True},
            "output": {"places": [], "text": "可用结果", "done": {}},
        })
    report = {
        "schema_version": "5.0",
        "mode": {"llm_judge": False, "retrieval": "frozen_snapshot"},
        "reproducibility": {"backend_execution_tree": {"sha256": "a" * 64}},
        "api_usage": {"provider_calls": 0, "generation_llm_calls": 0, "judge_api_calls": 0},
        "cases": rows,
    }
    path = tmp_path / "source.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


def _judgment(bundle: dict, round_number: int) -> dict:
    return {
        "schema_version": "1.0",
        "kind": "codex_subagent_judge_round",
        "evaluator": {
            "kind": "codex_subagent",
            "model": "gpt-5.6-sol",
            "evaluator_id": f"judge-{round_number}",
            "round": round_number,
            "blind": True,
        },
        "bindings": copy.deepcopy(bundle["bindings"]),
        "cases": [{
            "id": case["id"],
            "scores": {name: 5 for name in SCORE_NAMES},
            "critical_violations": [],
            "passed": True,
            "root_cause_hint": "none",
            "summary": "满足请求",
        } for case in bundle["cases"]],
    }


def test_blind_bundle_is_three_city_zero_api_and_has_no_legacy_judge(tmp_path):
    source = _source_report(tmp_path)
    bundle = export_blind_bundle(source, DATASET)
    assert bundle["scope"] == {
        "cities": ["北京", "上海", "杭州"], "cases_per_city": 50, "total": 150,
    }
    assert bundle["provenance_policy"]["judge_api_calls"] == 0
    assert len(bundle["cases"]) == 150
    assert all("judge" not in case and "human_label" not in case for case in bundle["cases"])


def test_round_validation_rejects_human_label_and_binding_drift(tmp_path):
    source = _source_report(tmp_path)
    bundle = export_blind_bundle(source, DATASET)
    judgment = _judgment(bundle, 1)
    validate_round(judgment, bundle)

    contaminated = copy.deepcopy(judgment)
    contaminated["cases"][0]["human_label"] = "pass"
    with pytest.raises(ValueError, match="human_label"):
        validate_round(contaminated, bundle)

    drifted = copy.deepcopy(judgment)
    drifted["bindings"]["dataset_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="bindings"):
        validate_round(drifted, bundle)


def test_three_round_panel_aggregates_without_api_or_human_claim(tmp_path):
    source = _source_report(tmp_path)
    bundle = export_blind_bundle(source, DATASET)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    judgment_paths = []
    for round_number in (1, 2, 3):
        path = tmp_path / f"judge-{round_number}.json"
        path.write_text(json.dumps(_judgment(bundle, round_number), ensure_ascii=False), encoding="utf-8")
        judgment_paths.append(path)

    panel = aggregate_panel(
        bundle_path, source, DATASET, judgment_paths, tmp_path / "round-reports",
    )
    assert panel["panel_summary"]["passed"] is True
    assert panel["panel_summary"]["unanimous_agreement_rate"] == 1.0
    assert panel["provenance"]["judge_api_calls"] == 0
    assert panel["provenance"]["human_calibration_performed"] is False
