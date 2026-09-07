"""A day's destination heading does not assign that city to origin-side visits."""
import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.pipeline import _model_activity_cities
from tests.test_semantic_partial_recovery import activity


@pytest.mark.parametrize("heading", ["Day3 上海", "第三天 上海", "Day3：上海"])
@pytest.mark.parametrize("quote", ["HEADING", "WHOLE_LINE", "从北京乘高铁到上海"])
def test_destination_heading_and_arrival_scope_survive_a_transport_origin_city(heading, quote):
    line = heading + "：从北京乘高铁到上海，游览外滩、豫园，今晚酒店未确定。"
    source = "北京、上海三日游。\nDay1 北京：故宫博物院。\nDay2 北京：天坛公园。\n" + line
    evidence = heading if quote == "HEADING" else line if quote == "WHOLE_LINE" else quote
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京、上海",
        "day_labels": [None] * 3, "activities": [activity("故宫博物院"), activity("天坛公园", 2),
            activity("外滩", 3, city="上海", city_evidence=evidence), activity("豫园", 3, city="上海", city_evidence=evidence)]}))
    assert proposal.unprocessed_count == 0
    assert [item.city_hint for item in proposal.mentions[-2:]] == ["上海", "上海"]
    assert [_model_activity_cities(source, proposal, item) for item in proposal.mentions[-2:]] == [("上海",), ("上海",)]


def test_origin_station_before_arrival_cannot_inherit_the_destination_city():
    source = "北京、上海两日游。\nDay1 北京：故宫博物院。\nDay2 上海：从北京南站出发，乘高铁到上海，游览外滩。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京、上海", "activities": [
        activity("故宫博物院"), activity("北京南站", 2, category="交通节点", city="上海", city_evidence="Day2 上海"),
        activity("外滩", 2, city="上海", city_evidence="Day2 上海")]}))
    assert proposal.mentions[1].city_hint is None and proposal.unprocessed_count == 1
    assert proposal.mentions[2].city_hint == "上海"


def test_a_day_with_actual_visits_in_two_cities_keeps_local_city_evidence_separate():
    source = "Day1 北京：上午在北京游览故宫博物院，然后到上海游览外滩。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京、上海", "activities": [
        activity("故宫博物院", city="北京", city_evidence="在北京游览故宫博物院"),
        activity("外滩", city="上海", city_evidence="到上海游览外滩")]}))
    assert [item.city_hint for item in proposal.mentions] == ["北京", "上海"]
    assert proposal.unprocessed_count == 0
    invalid = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京、上海", "activities": [
        activity("故宫博物院", city="北京", city_evidence="Day1 北京"),
        activity("外滩", city="北京", city_evidence="Day1 北京")]}))
    assert invalid.mentions[1].city_hint is None and invalid.unprocessed_count == 1


def test_a_conflicting_visit_without_explicit_arrival_stays_pending():
    source = "Day1 上海：先在北京游览故宫博物院，然后去外滩。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京、上海", "activities": [
        activity("故宫博物院", city="上海", city_evidence="Day1 上海"),
        activity("外滩", city="上海", city_evidence="Day1 上海")]}))
    assert [item.city_hint for item in proposal.mentions] == [None, None]
    assert proposal.unprocessed_count == 2


def test_a_destination_name_in_an_aside_does_not_move_actual_visits_from_another_city():
    source = "Day1 上海：先在北京游览故宫博物院，同行者说上海人多，然后去前门大街。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京、上海", "activities": [
        activity("故宫博物院", city="北京", city_evidence="在北京游览故宫博物院"),
        activity("前门大街", city="上海", city_evidence="Day1 上海")]}))
    assert proposal.mentions[0].city_hint == "北京"
    assert proposal.mentions[1].city_hint is None and proposal.unprocessed_count == 1


def test_beijing_road_is_not_a_city_conflict_or_a_beijing_city_claim():
    source = "Day1 广州：先游览北京路步行街，再去沙面岛。"
    good = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "广州", "activities": [
        activity("北京路步行街", city="广州", city_evidence="Day1 广州"), activity("沙面岛")]}))
    assert good.mentions[0].city_hint == "广州" and good.unprocessed_count == 0
    bad = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [
        activity("北京路步行街", city="北京", city_evidence="北京路步行街"), activity("沙面岛")]}))
    assert bad.mentions[0].city_hint is None and bad.unprocessed_count == 1


def test_city_metadata_without_a_literal_city_quote_does_not_gain_day_header_authority():
    source = "Day1 上海：游览外滩。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "上海", "activities": [
        activity("外滩", city="上海", city_evidence="游览外滩")]}))
    assert proposal.mentions[0].city_hint is None and proposal.unprocessed_count == 1
