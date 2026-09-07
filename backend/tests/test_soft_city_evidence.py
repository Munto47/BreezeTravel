"""Synthetic city-lane checks; reviewed names never establish a live POI."""
from __future__ import annotations

import hashlib

import httpx
import pytest

from app.trip_understanding import _three_city_place_lexicon as lexicon_module
from app.trip_understanding import landmark_hints
from app.trip_understanding._three_city_place_lexicon import PlaceLexiconEntry, ThreeCityPlaceLexicon
from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.models import ActivityRole, DestinationBasis, InferenceProposal, ProposedMention
from app.trip_understanding.pipeline import TripUnderstandingPipeline, _model_activity_cities


PENDING = ("目的地待确认",)


def proposal_for(names, *, destination="上海", prefix="", roles=None):
    source = prefix + "；".join(names) + "。"
    mentions = []
    cursor = len(prefix)
    for index, name in enumerate(names):
        start = source.index(name, cursor)
        mentions.append(ProposedMention(
            mention_id=f"mention-{index}", raw_text=name, span_start=start, span_end=start + len(name),
            role=(roles or {}).get(index, ActivityRole.PLANNED), day_index=1, sequence_index=index,
            atomic_place_name=name, category_hint="景点",
        ))
        cursor = start + len(name)
    return source, InferenceProposal(
        source_hash=hashlib.sha256(source.encode()).hexdigest(), destination_name=destination,
        destination_basis=DestinationBasis.SOFT_ASSUMPTION, mentions=mentions,
        binding={"semantic_policy": "MODEL_MEANING_SOURCE_VALIDATED_V1"}, day_count=1,
    )


def entry(name, *, city="上海", aliases=(), category="attraction"):
    return PlaceLexiconEntry(
        entry_id=f"{city}:{name}", city=city, canonical_name=name, aliases=aliases, category=category,
        district=None, sources=(), verified_at="2026-09-06",
    )


def use_entries(monkeypatch, entries, *, available=True, hints=()):
    monkeypatch.setattr(lexicon_module, "get_three_city_place_lexicon", lambda: ThreeCityPlaceLexicon(
        entries=tuple(entries), available=available,
    ))
    monkeypatch.setattr(landmark_hints, "HINTS", hints)


def test_two_reviewed_shanghai_landmarks_support_only_the_soft_query_city():
    source, proposal = proposal_for(["上海博物馆（人民广场馆）", "豫园", "武康路"])
    assert all(_model_activity_cities(source, proposal, item) == ("上海",) for item in proposal.mentions)
    assert proposal.destination_basis == DestinationBasis.SOFT_ASSUMPTION
    assert all(item.city_hint is None and item.city_evidence is None for item in proposal.mentions)


@pytest.mark.parametrize("city", ["北京", "上海", "杭州"])
@pytest.mark.parametrize("use_alias", [False, True])
def test_two_distinct_exact_or_reviewed_alias_entries_support_the_existing_city(monkeypatch, city, use_alias):
    use_entries(monkeypatch, [entry("甲园", city=city, aliases=("甲园旧称",)),
                              entry("乙站", city=city, category="transport")])
    source, proposal = proposal_for([f"{city}文化馆", "甲园旧称" if use_alias else "甲园", "乙站"], destination=city)
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == (city,)


@pytest.mark.parametrize("names", [
    ["北京路步行街"], ["上海锦江酒店", "如家酒店"],
    ["上海博物馆（人民广场馆）", "武康路"],
])
def test_embedded_city_or_one_landmark_alone_remains_pending(names):
    city = "北京" if names[0].startswith("北京") else "上海"
    source, proposal = proposal_for(names, destination=city)
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


@pytest.mark.parametrize("category", ["food", "hotel"])
def test_food_and_hotel_names_do_not_support_a_soft_city(monkeypatch, category):
    use_entries(monkeypatch, [entry("甲店", category=category), entry("乙店", category=category)])
    source, proposal = proposal_for(["上海文化馆", "甲店", "乙店"])
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


def test_two_aliases_of_the_same_entry_are_one_support(monkeypatch):
    use_entries(monkeypatch, [entry("甲园", aliases=("甲园旧称",))])
    source, proposal = proposal_for(["上海文化馆", "甲园", "甲园旧称"])
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


def test_same_identity_in_hint_and_lexicon_is_one_support(monkeypatch):
    hint = next(item for item in landmark_hints.HINTS if item.name == "武康路")
    use_entries(monkeypatch, [entry("武康路")], hints=(hint,))
    source, proposal = proposal_for(["上海文化馆", "武康路"])
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


def test_one_distinct_dictionary_entry_and_one_hint_can_support_the_city(monkeypatch):
    hint = next(item for item in landmark_hints.HINTS if item.name == "武康路")
    use_entries(monkeypatch, [entry("甲园")], hints=(hint,))
    source, proposal = proposal_for(["上海文化馆", "武康路", "甲园"])
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("上海",)


@pytest.mark.parametrize("category", ["attraction", "hotel"])
def test_cross_city_dictionary_ambiguity_cannot_count_as_support(monkeypatch, category):
    use_entries(monkeypatch, [entry("甲园"), entry("乙园"), entry("甲园", city="杭州", category=category)])
    source, proposal = proposal_for(["上海文化馆", "甲园", "乙园"])
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


def test_hint_is_checked_against_other_cities_in_the_dictionary(monkeypatch):
    hints = landmark_hints.HINTS
    use_entries(monkeypatch, [entry("武康路", city="杭州")], hints=hints)
    source, proposal = proposal_for(["上海文化馆", "武康路", "豫园"])
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


def test_unavailable_dictionary_does_not_allow_hints_to_bypass_ambiguity_check(monkeypatch):
    hints = landmark_hints.HINTS
    use_entries(monkeypatch, [], available=False, hints=hints)
    source, proposal = proposal_for(["上海文化馆", "武康路", "豫园"])
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


@pytest.mark.parametrize("role", [ActivityRole.OPTIONAL, ActivityRole.REFERENCE,
                                  ActivityRole.EXCLUDED, ActivityRole.PASS_THROUGH])
def test_non_planned_names_cannot_supply_a_second_support(role):
    source, proposal = proposal_for(["上海文化馆", "武康路", "豫园"], roles={2: role})
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


@pytest.mark.parametrize("mutation", ["wrong_span", "raw_mismatch", "no_day", "invalid_city_evidence"])
def test_unbound_or_invalid_support_does_not_count(mutation):
    source, proposal = proposal_for(["上海文化馆", "武康路", "豫园"])
    updates = {
        "wrong_span": {"span_start": 0, "span_end": 2},
        "raw_mismatch": {"raw_text": "游览豫园"},
        "no_day": {"day_index": None},
        "invalid_city_evidence": {"city_evidence": "不存在的城市依据"},
    }
    proposal.mentions[2] = proposal.mentions[2].model_copy(update=updates[mutation])
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


@pytest.mark.parametrize("prefix", ["第二天去杭州。", "广州行程。", "从北京出发。"])
def test_two_supports_do_not_bypass_explicit_other_city(prefix):
    source, proposal = proposal_for(["上海文化馆", "武康路", "豫园"], prefix=prefix)
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


@pytest.mark.parametrize("narrative", [
    "园内有昆明湖。",
    "看昆明湖，再去十七孔桥。",
    "苏州街→佛香阁→长廊→昆明湖→十七孔桥。",
    "昆明湖（园内湖泊）。",
    "可以坐昆明湖游船，少走路。",
    "昆明湖；沿岸走走。",
])
def test_city_prefix_inside_a_delimited_lake_name_is_not_another_destination(narrative):
    source, proposal = proposal_for(["故宫博物院", "颐和园", "天坛公园"],
                                    destination="北京", prefix=narrative)
    assert all(_model_activity_cities(source, proposal, item) == ("北京",) for item in proposal.mentions)
    assert proposal.destination_basis == DestinationBasis.SOFT_ASSUMPTION
    assert all(item.city_hint is None for item in proposal.mentions)


@pytest.mark.parametrize("narrative", [
    "昆明。", "去昆明。", "从昆明出发。", "昆明湖州两地。", "昆明湖州。",
    "昆明湖北两地。", "昆明湖A方案。", "昆明湖游船昆明两地。",
    "昆明湖，随后去昆明。", "昆明湖；下一天去苏州。",
])
def test_lake_suffix_does_not_hide_real_or_unclear_other_city(narrative):
    source, proposal = proposal_for(["故宫博物院", "颐和园", "天坛公园"],
                                    destination="北京", prefix=narrative)
    assert all(_model_activity_cities(source, proposal, item) == PENDING for item in proposal.mentions)


def test_another_planned_reviewed_city_rejects_a_single_city_inference(monkeypatch):
    use_entries(monkeypatch, [entry("甲园"), entry("乙园"), entry("丙园", city="杭州")])
    source, proposal = proposal_for(["上海文化馆", "甲园", "乙园", "丙园"])
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


def test_invalid_target_city_evidence_is_not_rescued_by_two_other_supports():
    source, proposal = proposal_for(["上海文化馆", "武康路", "豫园"])
    proposal.mentions[0].city_evidence = "错误依据"
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


def test_another_activitys_validated_city_keeps_ambiguous_activity_pending():
    source, proposal = proposal_for(["上海文化馆", "武康路", "豫园", "杭州乙园"])
    proposal.mentions[3].city_hint = "杭州"
    proposal.mentions[3].city_evidence = "杭州乙园"
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING
    assert _model_activity_cities(source, proposal, proposal.mentions[3]) == ("杭州",)


def test_support_cannot_promote_an_embedded_city_to_explicit_fact():
    source, proposal = proposal_for(["上海文化馆", "武康路", "豫园"])
    proposal.destination_basis = DestinationBasis.EXPLICIT
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


def test_venue_suffix_guess_does_not_supply_a_second_support(monkeypatch):
    use_entries(monkeypatch, [entry("甲博物馆"), entry("乙园")])
    source, proposal = proposal_for(["上海文化馆", "甲", "乙园"])
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == PENDING


@pytest.mark.asyncio
async def test_city_support_never_fabricates_resolved_cards():
    source = "第一天：上海博物馆（人民广场馆）；豫园；武康路。"
    names = ["上海博物馆（人民广场馆）", "豫园", "武康路"]
    draft = SemanticDraft.model_validate({"destination": "上海", "activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1, "category": "景点"}
        for name in names
    ]})

    class DraftProvider:
        async def propose(self, text):
            return proposal_from_draft(text, draft)

    class UnavailableResolver:
        def __init__(self):
            self.calls = []

        async def resolve(self, **values):
            self.calls.append(values)
            return None

    resolver = UnavailableResolver()
    output = await TripUnderstandingPipeline(DraftProvider(), resolver).run(source)
    assert [call["city"] for call in resolver.calls] == ["上海"] * 3
    assert [call["atomic_place_name"] for call in resolver.calls] == names
    assert [card.name for card in output.public_result.days[0].activities] == names
    assert all(card.status == "NEEDS_CONFIRMATION" for card in output.public_result.days[0].activities)
    assert output.public_result.assumptions[0].value == "暂按 上海"


@pytest.mark.asyncio
@pytest.mark.parametrize("typecode,label,matched", [
    ("110202", "风景名胜;风景名胜;国家级景点", True),
    ("190205", "地名地址信息;自然地名;湖泊", False),
    ("100100", "住宿服务;宾馆酒店;宾馆酒店", False),
])
async def test_yuyuan_hint_retains_live_identity_and_category_requirements(typecode, label, matched):
    hint = landmark_hints.landmark_hint("上海", "豫园")
    assert hint is not None and hint.district == "黄浦区"
    assert hint.typecode == "110202" and hint.technical_label is None and hint.aliases == ()
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "1", "pois": [{
            "id": "synthetic-yuyuan", "name": "上海豫园", "typecode": typecode, "type": label,
            "cityname": "上海市", "pname": "上海市", "adname": "黄浦区", "adcode": "310101",
            "location": "121.493,31.227", "address": "黄浦区",
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="上海", atomic_place_name="豫园", category_hint="景点",
        )
    assert (outcome.place is not None) is matched
    # The existing resolver retries an unfiltered query after a type mismatch.
    assert len(calls) == (1 if matched else 2)
    assert "110202" in calls[0].url.params["types"]
