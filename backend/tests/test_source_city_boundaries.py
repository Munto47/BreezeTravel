"""Only literal atomic place names may contain otherwise misleading city words."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline, _model_activity_cities
from tests.test_city_evidence_recovery import FixedCities
from tests.test_soft_city_evidence import proposal_for


def activity(name, day=1, **fields):
    return dict(source_quote=name, place_name=name, role="PLANNED", category="景点", day_index=day, **fields)


def cross_city_reference():
    source = "Day1 北京：国家博物馆、前门大街。\nDay2 上海：人民公园。"
    draft = SemanticDraft(destination="北京", activities=[
        activity("国家博物馆", city="北京", city_evidence="Day1 北京"),
        activity("前门大街", city="北京", city_evidence="Day1 北京"),
        dict(source_quote="Day2 上海", place_name=None, role="REFERENCE", day_index=2),
        activity("人民公园", 2),
    ])
    return source, proposal_from_draft(source, draft)


def test_unnamed_reference_cannot_hide_another_days_city():
    source, proposal = cross_city_reference()
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("北京",)
    assert _model_activity_cities(source, proposal, proposal.mentions[1]) == ("北京",)
    assert _model_activity_cities(source, proposal, proposal.mentions[-1]) == ("目的地待确认",)


@pytest.mark.asyncio
async def test_hidden_city_cannot_send_unassigned_park_to_other_city_resolver():
    source, proposal = cross_city_reference()

    class PreparedProvider:
        async def propose(self, _source):
            return proposal

    places = FixedCities()
    output = await TripUnderstandingPipeline(PreparedProvider(), places).run(source)
    assert places.calls == [("北京", "国家博物馆"), ("北京", "前门大街")]
    assert output.public_result.days[1].activities[0].name == "人民公园"
    assert output.public_result.days[1].activities[0].status == "NEEDS_CONFIRMATION"
    assert [card.status for card in output.public_result.days[0].activities] == ["READY", "READY"]


def test_unnamed_city_preamble_is_not_mistaken_for_an_embedded_place_name():
    source = "上海一日游。\nDay1：星河公园。"
    proposal = proposal_from_draft(source, SemanticDraft(destination="上海", activities=[
        dict(source_quote="上海一日游", place_name=None, role="REFERENCE"), activity("星河公园")]))
    assert proposal.destination_basis.value == "EXPLICIT"
    assert _model_activity_cities(source, proposal, proposal.mentions[-1]) == ("上海",)


def test_a_wide_named_reference_only_exempts_its_atomic_name_not_its_whole_quote():
    source, proposal = proposal_for(["上海文化馆", "武康路", "豫园"], prefix="Day2 杭州：")
    proposal.mentions[0] = proposal.mentions[0].model_copy(update={
        "span_start": 0, "raw_text": source[:proposal.mentions[0].span_end]})
    assert _model_activity_cities(source, proposal, proposal.mentions[1]) == ("目的地待确认",)


def test_city_word_cannot_cross_the_end_of_an_atomic_name():
    source, proposal = proposal_for(["上海文化馆", "武康路", "豫园", "云杭"])
    source = source[:-1] + "州一日游。"
    # The literal synthetic name 云杭 contains only the first character of
    # 杭州. That incomplete overlap cannot exempt the whole city word.
    assert _model_activity_cities(source, proposal, proposal.mentions[1]) == ("目的地待确认",)


@pytest.mark.parametrize("narrative", ["园内苏州街散步。", "昆明湖游船。", "苏州街→昆明湖。"])
def test_actual_internal_street_and_lake_names_keep_existing_single_city_lane(narrative):
    source, proposal = proposal_for(["国家博物馆", "前门大街"], destination="北京", prefix=narrative)
    assert all(_model_activity_cities(source, proposal, item) == ("北京",) for item in proposal.mentions)


def test_literal_beijing_road_is_still_a_place_name_not_a_beijing_city_claim():
    source = "Day1 广州：北京路步行街、沙面岛。"
    proposal = proposal_from_draft(source, SemanticDraft(destination="广州", activities=[
        activity("北京路步行街", city="广州", city_evidence="Day1 广州"), activity("沙面岛")]))
    assert all(_model_activity_cities(source, proposal, item) == ("广州",) for item in proposal.mentions)
