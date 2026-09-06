"""Source-qualified synthetic drafts; no model, map or other external calls."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.trip_understanding.experience_inference import (
    ExperienceQwenProvider,
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)
from app.trip_understanding.models import PlaceResolutionOutcome, ResolvedPlace
from app.trip_understanding.pipeline import TripUnderstandingPipeline


def activity(name, *, quote=None, day=1, occurrence=1, city="上海", evidence="Day 1 上海"):
    return dict(source_quote=quote or name, place_name=name, role="PLANNED", day_index=day,
                occurrence=occurrence, category="景点", city=city, city_evidence=evidence)


class DraftProvider:
    def __init__(self, activities, destination="上海"):
        self.draft = SemanticDraft(destination=destination, activities=activities)

    async def propose(self, source):
        return proposal_from_draft(source, self.draft)


class SyntheticResolver:
    def __init__(self, code, binding_code=None):
        self.code = code
        self.binding_code = code if binding_code is None else binding_code
        self.calls = []

    async def resolve(self, **query):
        self.calls.append(query)
        binding = {"city": query["city"], "external_calls": 0}
        if self.binding_code is not None:
            binding["adcode"] = self.binding_code
        return PlaceResolutionOutcome(
            place=ResolvedPlace(canonical_place_id="synthetic-place", name=query["atomic_place_name"],
                category="景点", area_or_address="合成地址", provider_binding=binding),
            receipt={"city": query["city"], "adcode": self.code, "external_calls": 0},
        )


@pytest.mark.parametrize("qualified,shortened", [
    ("故宫北门", "故宫"),
    ("故宫东北门", "故宫"),
    ("上海博物馆东馆", "上海博物馆"),
    ("上海博物馆人民广场馆", "上海博物馆"),
    ("浙江省博物馆之江馆区", "浙江省博物馆"),
    ("浙江省博物馆（孤山馆区）", "浙江省博物馆"),
    ("全聚德前门店", "全聚德"),
    ("星河酒店王府井店", "星河酒店"),
    ("星河展览馆2号馆", "星河展览馆"),
    ("星河书店长宁分店", "星河书店"),
    ("星河博物馆徐汇分馆", "星河博物馆"),
    ("星河大学东校区", "星河大学"),
    ("星河酒店（王府井店）", "星河酒店"),
    ("故宫(北门)", "故宫"),
    ("**故宫**北门", "故宫"),
    ("故宫北门口见", "故宫"),
    ("星河书店长宁分店喝咖啡", "星河书店"),
])
@pytest.mark.parametrize("quote_complete", [False, True])
def test_short_quote_cannot_discard_attached_entrance_or_branch(qualified, shortened, quote_complete):
    source = f"Day 1 上海，去{qualified}。"
    row = activity(shortened, quote=qualified if quote_complete else shortened)
    with pytest.raises(SourceAnchorValidationError) as raised:
        proposal_from_draft(source, SemanticDraft(destination="上海", activities=[row]))
    assert raised.value.issues == [{"field": "activities[0].place_name", "category": "PLACE_QUALIFIER_OMITTED"}]


@pytest.mark.parametrize("source,names", [
    ("Day 1 上海，上海博物馆东馆出来再去外滩。", ["上海博物馆东馆", "外滩"]),
    ("Day 1 上海，浙江省博物馆之江馆区出来再去外滩。", ["浙江省博物馆之江馆区", "外滩"]),
    ("Day 1 上海，上海博物馆人民广场馆出来再去外滩。", ["上海博物馆人民广场馆", "外滩"]),
    ("Day 1 上海，全聚德前门店吃饭后再去外滩。", ["全聚德前门店", "外滩"]),
    ("Day 1 上海，上海博物馆附近还有别的展览馆，本次只去上海博物馆。", ["上海博物馆"]),
    ("Day 1 上海，上海博物馆是一家博物馆，之后去外滩。", ["上海博物馆", "外滩"]),
    ("Day 1 上海，星河酒店出来再去星河书店长宁分店。", ["星河酒店", "星河书店长宁分店"]),
    ("Day 1 上海，上海博物馆（从北门出），再去外滩。", ["上海博物馆", "外滩"]),
    ("Day 1 上海，星河酒店（王府井店）入住后去外滩。", ["星河酒店（王府井店）", "外滩"]),
    ("Day 1 上海，上海博物馆。东馆出来再去外滩。", ["上海博物馆", "外滩"]),
])
def test_complete_qualifiers_and_later_actions_keep_individual_place_names(source, names):
    proposal = proposal_from_draft(source, SemanticDraft(destination="上海", activities=[activity(name) for name in names]))
    assert [mention.atomic_place_name for mention in proposal.mentions] == names


@pytest.mark.asyncio
async def test_qualifier_validation_uses_existing_single_model_repair():
    bad = dict(destination="北京", activities=[activity("故宫", city=None, evidence=None)])
    good = dict(destination="北京", activities=[activity("故宫北门", city=None, evidence=None)])
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            content=json.dumps(bad if len(calls) == 1 else good, ensure_ascii=False)))],
            usage=SimpleNamespace(prompt_tokens=50, completion_tokens=25), model="synthetic-model")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider = ExperienceQwenProvider(api_key="synthetic-not-used", base_url="https://example.test/v1",
        model="synthetic-model", client=client)
    proposal = await provider.propose("北京一日游。Day 1 故宫北门集合。")
    assert len(calls) == 2
    assert proposal.binding["repair_call_count"] == 1
    assert proposal.mentions[0].atomic_place_name == "故宫北门"
    assert proposal.binding["calls"][0]["validation_errors"][0]["category"] == "PLACE_QUALIFIER_OMITTED"


@pytest.mark.parametrize("source,evidence", [
    ("Day 1 上海，参观位于浦东新区的星河博物馆。", "Day 1 上海"),
    ("Day 1 上海浦东新区，参观星河博物馆。", "Day 1 上海浦东新区"),
    ("Day 1 上海浦东新区\n星河博物馆。", "Day 1 上海浦东新区"),
    ("Day 1 上海，星河博物馆（浦东新区）。", "Day 1 上海"),
    ("Day 1 上海，星河博物馆，位于浦东新区。", "Day 1 上海"),
])
@pytest.mark.parametrize("code,status", [("310115", "READY"), ("310101", "NEEDS_CONFIRMATION"), (None, "NEEDS_CONFIRMATION")])
@pytest.mark.asyncio
async def test_explicit_local_district_requires_matching_recorded_adcode(source, evidence, code, status):
    resolver = SyntheticResolver(code)
    output = await TripUnderstandingPipeline(DraftProvider([activity("星河博物馆", evidence=evidence)]), resolver).run(source)
    card = output.public_result.days[0].activities[0]
    assert card.name == "星河博物馆"
    assert card.status == status
    assert len(resolver.calls) == 1
    if status == "NEEDS_CONFIRMATION":
        assert output.activities[0].place is None
        assert output.activities[0].resolver_receipt["failure_category"].startswith("SOURCE_DISTRICT_")


@pytest.mark.parametrize("source", [
    "Day 1 上海，星河博物馆，之后去浦东新区的世纪公园。",
    "Day 1 上海，浦东新区的世纪公园 → 星河博物馆。",
    "Day 1 上海，星河博物馆。另一天可去浦东新区。",
    "Day 1 上海浦东新区。\nDay 2 上海黄浦区。\n星河博物馆。",
    "Day 1 北京朝阳区。\nDay 2 上海。\n星河博物馆。",
])
@pytest.mark.asyncio
async def test_another_stop_day_or_city_cannot_supply_this_places_district(source):
    second_day = "Day 2" in source
    evidence = "Day 2 上海黄浦区" if "Day 2 上海黄浦区" in source else "Day 2 上海" if second_day else "Day 1 上海"
    resolver = SyntheticResolver("310101")
    rows = [activity("星河博物馆", day=2 if second_day else 1, evidence=evidence)]
    if "世纪公园" in source:
        rows.append(activity("世纪公园"))
        rows.sort(key=lambda row: source.index(row["place_name"]))
    output = await TripUnderstandingPipeline(DraftProvider(rows), resolver).run(source)
    card = next(card for day in output.public_result.days for card in day.activities if card.name == "星河博物馆")
    assert card.status == "READY"
    assert card.city == "上海"


@pytest.mark.asyncio
async def test_repeated_place_lookup_is_shared_but_each_day_checks_its_own_district():
    source = "Day 1 上海黄浦区\n星河博物馆。\nDay 2 上海浦东新区\n星河博物馆。"
    provider = DraftProvider([
        activity("星河博物馆", evidence="Day 1 上海黄浦区"),
        activity("星河博物馆", day=2, occurrence=2, evidence="Day 2 上海浦东新区"),
    ])
    resolver = SyntheticResolver("310101")
    output = await TripUnderstandingPipeline(provider, resolver).run(source)
    assert len(resolver.calls) == 1
    assert [day.activities[0].status for day in output.public_result.days] == ["READY", "NEEDS_CONFIRMATION"]
    assert output.activities[1].resolver_receipt["failure_category"] == "SOURCE_DISTRICT_MISMATCH"


@pytest.mark.parametrize("code,binding_code", [("310115", "310101"), ("not-an-adcode", "310115")])
@pytest.mark.asyncio
async def test_missing_or_conflicting_provider_admin_evidence_cannot_pass(code, binding_code):
    source = "Day 1 上海，参观浦东新区的星河博物馆。"
    output = await TripUnderstandingPipeline(DraftProvider([activity("星河博物馆")]),
        SyntheticResolver(code, binding_code)).run(source)
    assert output.public_result.days[0].activities[0].status == "NEEDS_CONFIRMATION"


@pytest.mark.asyncio
async def test_conflicting_local_district_and_heading_cannot_choose_one_silently():
    source = "Day 1 上海黄浦区\n星河博物馆（浦东新区）。"
    output = await TripUnderstandingPipeline(
        DraftProvider([activity("星河博物馆", evidence="Day 1 上海黄浦区")]),
        SyntheticResolver("310101"),
    ).run(source)
    assert output.public_result.days[0].activities[0].status == "NEEDS_CONFIRMATION"
    assert output.activities[0].resolver_receipt["failure_category"] == "SOURCE_DISTRICT_UNVERIFIED"
