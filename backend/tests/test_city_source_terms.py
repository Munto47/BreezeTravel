"""Source-city boundaries; saved real model output with explicitly fake identity."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.trip_understanding.experience_inference import (
    ExperienceQwenProvider, SemanticDraft, _proposal_from_live_draft,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline, _model_activity_cities
from tests.test_city_evidence_recovery import FixedCities


FIXTURE = Path(__file__).with_name("fixtures") / "live_city_source_terms.json"


def response_object(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: response_object(item) for key, item in value.items()})
    if isinstance(value, list):
        return [response_object(item) for item in value]
    return value


class SavedCityTermsClient:
    """Match the unchanged source and request purpose, independent of scheduling."""

    def __init__(self, case):
        self.responses = case["model_responses"]
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        source = kwargs["messages"][-1]["content"]
        kind = "STRUCTURE" if prompt.startswith("只分析旅行原文的全局结构") else (
            "CITY_METADATA" if prompt.startswith("只修复已提取活动的城市字段") else "EXTRACT")
        if kind == "CITY_METADATA":
            source = json.loads(source)["source"]
        matches = [item for item in self.responses if item["kind"] == kind and item["source"] == source]
        assert len(matches) == 1, "No saved model response: this test must never request a live service"
        self.calls.append(matches[0]["original_call"])
        return response_object(matches[0]["response"])


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id,city,required_query", [
    ("qwen-beijing-02", "北京", "颐和园"),
    ("qwen-shanghai-06", "上海", "外白渡桥"),
])
async def test_saved_real_source_and_raw_keep_valid_city_through_provider_and_public_result(case_id, city, required_query):
    case = next(item for item in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"] if item["case_id"] == case_id)
    client, identities = SavedCityTermsClient(case), FixedCities()
    provider = ExperienceQwenProvider(api_key="test", base_url="https://test.invalid", model="controlled", client=client)
    output = await TripUnderstandingPipeline(provider, identities).run(case["source"])
    assert not any(issue.category == "UNSUPPORTED_CITY_REMOVED" for issue in output.proposal.diagnostics)
    assert (city, required_query) in identities.calls
    assert all(query_city == city for query_city, _name in identities.calls)
    assert any(card.name == required_query and card.status == "READY"
               for day in output.public_result.days for card in day.activities)
    assert len(output.public_result.days) == 3
    # Real raw omissions, uncertain timing and unsupported day labels stay
    # visible. Simulated place confirmations do not make these trips complete.
    assert output.public_result.coverage.complete is False
    assert output.public_result.coverage.unprocessed_count > 0
    assert len(client.calls) <= len(case["model_responses"])


def plan(source, city="北京", *, evidence=None):
    draft = SemanticDraft.model_validate({"destination": city, "activities": [{
        "source_quote": "星河公园", "place_name": "星河公园", "role": "PLANNED", "day_index": 1,
        "category": "景点", "city": city, "city_evidence": evidence or f"{city}一日游",
    }]})
    return _proposal_from_live_draft(source, draft, allow_partial=True)


@pytest.mark.parametrize("city,narrative", [
    ("北京", "沿昆明湖岸漫步至十七孔桥。"),
    ("北京", "在昆明湖畔散步。"),
    ("北京", "苏州街→昆明湖游船。"),
    ("上海", "从南京西路的外白渡桥开始。"),
    ("上海", "南京东路附近用餐。"),
    ("广州", "北京路步行街附近散步。"),
])
def test_explicit_and_soft_city_checks_share_local_feature_word_boundaries(city, narrative):
    source = f"{city}一日游。\nDay1：星河公园。{narrative}"
    proposal = plan(source, city)
    mention = proposal.mentions[0]
    assert mention.city_hint == city and mention.city_evidence == f"{city}一日游"
    assert not any(issue.category == "UNSUPPORTED_CITY_REMOVED" for issue in proposal.diagnostics)
    assert _model_activity_cities(source, proposal, mention.model_copy(update={"city_hint": None, "city_evidence": None})) == (city,)


@pytest.mark.parametrize("narrative", [
    "去昆明。", "从昆明出发。", "沿昆明湖岸漫步后去昆明。", "昆明湖州两地。",
    "昆明湖北两地。", "昆明湖A方案。", "南京西路，之后去南京。", "苏州街，随后去苏州。",
])
def test_local_feature_exception_never_erases_actual_or_unclear_cross_city(narrative):
    source = f"北京一日游。\nDay1：星河公园。{narrative}"
    proposal = plan(source)
    mention = proposal.mentions[0]
    assert mention.city_hint is None
    assert any(issue.category == "UNSUPPORTED_CITY_REMOVED" for issue in proposal.diagnostics)
    assert _model_activity_cities(source, proposal, mention.model_copy(update={"city_hint": None, "city_evidence": None})) == ("目的地待确认",)


@pytest.mark.parametrize("city,evidence", [("北京", "北京路步行街"), ("苏州", "苏州街"), ("昆明", "昆明湖岸")])
def test_a_local_feature_is_never_positive_city_evidence(city, evidence):
    source = f"Day1：在{evidence}附近游览星河公园。"
    proposal = plan(source, city, evidence=evidence)
    assert proposal.mentions[0].city_hint is None
    assert any(issue.category == "UNSUPPORTED_CITY_REMOVED" for issue in proposal.diagnostics)


def test_unknown_city_still_requires_explicit_administrative_name_in_source():
    source = "泉州一日游。\nDay1：星河公园。"
    proposal = plan(source, "泉州")
    assert proposal.mentions[0].city_hint is None
    assert any(issue.category == "UNSUPPORTED_CITY_REMOVED" for issue in proposal.diagnostics)


def test_a_broad_unnamed_quote_cannot_hide_a_real_city_beside_local_feature_words():
    source = "北京一日游。\nDay1：沿昆明湖岸漫步。\nDay2 昆明：星河公园。"
    draft = SemanticDraft.model_validate({"destination": "北京", "activities": [
        {"source_quote": "Day2 昆明：星河公园", "place_name": None, "role": "REFERENCE", "day_index": 2},
        {"source_quote": "星河公园", "place_name": "星河公园", "role": "PLANNED", "day_index": 2,
         "category": "景点", "city": "北京", "city_evidence": "北京一日游"},
    ]})
    proposal = _proposal_from_live_draft(source, draft, allow_partial=True)
    mention = proposal.mentions[-1]
    assert mention.city_hint is None
    assert _model_activity_cities(source, proposal, mention.model_copy(update={"city_hint": None, "city_evidence": None})) == ("目的地待确认",)
