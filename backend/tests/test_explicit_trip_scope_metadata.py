"""Explicit trip headings and relative dates survive optional model metadata."""
import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.pipeline import _model_activity_cities
from tests.test_semantic_partial_recovery import activity


@pytest.mark.parametrize("heading", ["北京三日游", "北京三日两晚行程"])
@pytest.mark.parametrize("evidence", ["北京", "HEADING"])
def test_single_city_trip_heading_scopes_all_days_without_false_city_conflict(heading, evidence):
    source = heading + "。\nDay1：故宫博物院。\nDay2：天坛公园。\nDay3：颐和园。"
    draft = SemanticDraft.model_validate({"destination": "北京", "activities": [
        activity(name, day, city="北京", city_evidence=heading if evidence == "HEADING" else evidence)
        for day, name in enumerate(["故宫博物院", "天坛公园", "颐和园"], 1)]})
    proposal = proposal_from_draft(source, draft)
    assert proposal.destination_basis.value == "EXPLICIT"
    assert proposal.unprocessed_count == 0
    assert [_model_activity_cities(source, proposal, item) for item in proposal.mentions] == [("北京",)] * 3


def test_single_city_header_cannot_override_a_later_actual_city():
    source = "北京三日游。\nDay1：故宫博物院。\nDay2 上海：外滩。"
    draft = SemanticDraft.model_validate({"destination": "北京", "activities": [
        activity("外滩", 2, city="北京", city_evidence="北京三日游")]})
    proposal = proposal_from_draft(source, draft)
    assert proposal.mentions[0].city_hint is None and proposal.unprocessed_count == 1
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("目的地待确认",)


@pytest.mark.parametrize("labels,expected_pending", [(["Day 1", "Day 2"], 0), (["Day 2", "Day 1"], 2), (["9月1日", "9月2日"], 2)])
def test_only_correct_source_relative_labels_repeat_the_retained_day_index(labels, expected_pending):
    source = "北京两日游。\nDay 1：故宫博物院。\nDay 2：天坛公园。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "day_labels": labels,
        "activities": [activity("故宫博物院"), activity("天坛公园", 2)]}))
    assert proposal.unprocessed_count == expected_pending
    assert proposal.day_labels == {}


@pytest.mark.parametrize("preamble,evidence", [
    ("欢迎来到杭州，这份行程安排三天。", "欢迎来到杭州"),
    ("本次目的地设为杭州市，按以下安排旅行。", "目的地设为杭州市"),
    ("# 杭州\n第一次来访的三日安排如下。", "杭州"),
])
def test_contiguous_single_city_preamble_can_scope_every_day_without_fixed_wording(preamble, evidence):
    source = preamble + "\n第一天：断桥残雪。\n第二天：灵隐寺。\n第三天：拱宸桥。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "杭州", "activities": [
        activity(name, day, city="杭州", city_evidence=evidence)
        for day, name in enumerate(["断桥残雪", "灵隐寺", "拱宸桥"], 1)]}))
    assert proposal.unprocessed_count == 0
    assert [_model_activity_cities(source, proposal, item) for item in proposal.mentions] == [("杭州",)] * 3


@pytest.mark.parametrize("source,city,evidence", [
    ("杭州和上海的攻略。\nDay1：西湖。\nDay2：外滩。", "杭州", "杭州"),
    ("欢迎来到杭州。\nDay1：西湖。\nDay2 上海：外滩。", "杭州", "欢迎来到杭州"),
    ("先去广州北京路步行街。\nDay1：沙面岛。\nDay2：越秀公园。", "北京", "北京路步行街"),
    ("Day1：杭州西湖。\nDay2：外滩。", "杭州", "杭州"),
    ("欢迎来到杭州。\nDay1：西湖。\nDay2：灵隐寺。", "杭州", "灵隐寺"),
])
def test_preamble_scope_does_not_waive_missing_city_poi_substrings_or_cross_city_days(source, city, evidence):
    name = "越秀公园" if "越秀公园" in source else "灵隐寺" if "灵隐寺" in source else "外滩"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": city,
        "activities": [activity(name, 2, city=city, city_evidence=evidence)]}))
    assert proposal.mentions[0].city_hint is None and proposal.unprocessed_count == 1
