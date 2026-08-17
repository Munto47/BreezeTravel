from scripts.audit_daily_query_goal import audit_goal


def _report(rate=0.9, failures=0):
    cases = []
    for index in range(150):
        failure_reasons = ["缺少必需品类：attraction"] if index == 0 else []
        cases.append({
            "id": f"c{index}",
            "deterministic": {
                "failures": failure_reasons,
                "retrieval_integrity": {
                    "retrieval_audit_count": 1,
                    "failure_categories": {},
                },
            },
        })
    summary = {
        "total": 150,
        "passed": round(150 * rate),
        "pass_rate": rate,
        "by_city": {city: {"pass_rate": rate} for city in ("北京", "上海", "杭州")},
        "by_intent": {intent: {"pass_rate": rate} for intent in ("attraction", "food", "hotel", "mixed", "all")},
        "by_intent_group": {"compound": {"pass_rate": rate}},
        "retrieval_integrity": {
            "fixture_places": 0,
            "fallback_places": 0,
            "canonical_duplicate_count": 0,
            "amap_tool_call_count": 300,
            "amap_failure_count": failures,
        },
        "high_risk_honesty": {
            "unsupported_affirmative_claim_count": 0,
            "confirmation_action_coverage": 1.0,
        },
    }
    return {
        "summary": summary,
        "api_usage": {"provider_calls": 0, "generation_llm_calls": 0, "judge_api_calls": 0},
        "cases": cases,
    }


def _judge_report(rate=0.9):
    report = _report(rate)
    report["judge_chain"] = {
        "kind": "codex_subagent", "model": "gpt-5.6-sol", "network_calls": 0,
    }
    report["summary"].update({
        "judged": 150,
        "judge_errors": 0,
        "average_judge_scores": {
            "constraint_adherence": 4.2,
            "geographic_fit": 4.3,
            "persona_fit": 4.2,
            "practical_usefulness": 4.4,
            "groundedness": 4.7,
        },
    })
    return report


def _panel(agreement=0.9, passed=True):
    return {
        "kind": "codex_subagent_judge_panel",
        "evaluators": [{"model": "gpt-5.6-sol"}] * 3,
        "panel_summary": {
            "total": 150,
            "unanimous_agreement_rate": agreement,
            "quality_thresholds_passed": passed,
        },
        "provenance": {"judge_api_calls": 0, "human_calibration_performed": False},
    }


def test_goal_audit_passes_available_gates_and_blocks_missing_external_evidence():
    result = audit_goal([_report(0.9, 2), _report(0.91, 0)], [_report()] * 3, [])
    assert result["overall_status"] == "blocked"
    assert result["groups"]["data_and_reliability"]["tool_failure_rate"]["status"] == "passed"
    assert result["groups"]["deterministic_contract"]["missing_category"]["status"] == "passed"
    assert result["groups"]["recommendation_quality"]["snapshot_overall"]["status"] == "blocked"
    assert result["groups"]["recommendation_quality"]["semantic_scores"]["status"] == "blocked"
    assert result["groups"]["high_risk_honesty"]["model_panel_agreement"]["status"] == "blocked"


def test_goal_audit_accepts_complete_subagent_panel_without_human_claim():
    judges = [_judge_report() for _ in range(3)]
    result = audit_goal([_report(), _report(0.91)], [_report()] * 3, judges, _panel())
    assert result["overall_status"] == "passed"
    assert result["calibration_disclosure"]["human_calibration"]["status"] == "not_run"
    assert result["calibration_disclosure"]["human_calibration"]["claim_allowed"] is False


def test_goal_audit_never_substitutes_unjudged_deterministic_rate_for_quality():
    judges = [_judge_report(0.82) for _ in range(3)]
    result = audit_goal([_report(), _report()], [_report(1.0)] * 3, judges, _panel())
    quality = result["groups"]["recommendation_quality"]
    assert quality["snapshot_overall"]["status"] == "failed"
    assert quality["snapshot_overall"]["evidence"] == [0.82, 0.82, 0.82]
