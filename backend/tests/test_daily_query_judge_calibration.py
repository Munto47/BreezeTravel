import pytest

from scripts.calibrate_daily_query_judge import prepare_calibration, score_agreement


def _report(count=45):
    cities = ("北京", "上海", "杭州")
    intents = ("attraction", "food", "hotel", "mixed", "all")
    return {"cases": [{
        "id": f"case-{index:02d}",
        "city": cities[index % 3],
        "intent": intents[index % 5],
        "persona": "测试",
        "query": "轮椅请求" if index % 4 == 0 else "普通请求",
        "output": {"places": [], "text": "结果"},
    } for index in range(count)]}


def test_prepare_calibration_is_blind_stratified_and_deterministic():
    first = prepare_calibration(_report(), 40)
    second = prepare_calibration(_report(), 40)
    assert [row["id"] for row in first["cases"]] == [row["id"] for row in second["cases"]]
    assert first["sample_size"] == 40
    assert set(first["strata"]["by_city"]) == {"北京", "上海", "杭州"}
    assert max(first["strata"]["by_city"].values()) - min(first["strata"]["by_city"].values()) <= 1
    assert set(first["strata"]["by_intent"]) == {"attraction", "food", "hotel", "mixed", "all"}
    assert set(first["strata"]["by_intent"].values()) == {8}
    assert first["strata"]["high_risk"] > 0
    assert all(row["human_label"] is None and "judge" not in row for row in first["cases"])


def test_score_agreement_requires_complete_human_and_judge_evidence():
    labels = {"cases": [{"id": f"c{i}", "human_label": "pass"} for i in range(30)]}
    judge = {"cases": [{"id": f"c{i}", "judge": {"passed": i < 27}} for i in range(30)]}
    scored = score_agreement(labels, judge)
    assert scored["agreement_rate"] == 0.9
    assert scored["passed"] is True
    assert scored["confusion"]["human_pass_judge_fail"] == 3

    labels["cases"][0]["human_label"] = None
    with pytest.raises(ValueError, match="human_label"):
        score_agreement(labels, judge)


def test_score_agreement_rejects_judge_errors():
    labels = {"cases": [{"id": f"c{i}", "human_label": "fail"} for i in range(30)]}
    judge = {"cases": [{"id": f"c{i}", "judge": {"passed": False}} for i in range(30)]}
    judge["cases"][4]["judge"] = {"error": "402"}
    with pytest.raises(ValueError, match="not valid"):
        score_agreement(labels, judge)
