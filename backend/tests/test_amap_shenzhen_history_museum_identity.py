"""Official venue equivalence; saved real HTTP, controlled negative candidates."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.city_knowledge import CityKnowledge, load_city_knowledge
from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline


FIXTURE = json.loads((Path(__file__).parent / "fixtures/amap_shenzhen_history_museum.json").read_text(encoding="utf-8"))
CANONICAL = "深圳博物馆历史民俗馆"
ALIAS = "深圳博物馆金田路馆"
VENUE_ID = "B02F38IRTR"
OFFICIAL_GUIDE = "https://wtl.sz.gov.cn/ztzl_78228/zdly/ssbwg/zn_81828/content/post_12041865.html"


def fixed_client(observed, *, candidates=None, query=CANONICAL):
    def reply(request):
        observed.append(request)
        assert request.url.host == "restapi.amap.com"
        params = {k: v for k, v in request.url.params.multi_items() if k != "key"}
        # A changed query/candidate is an explicit controlled negative. The
        # positive/default path replays the actual exact original request.
        if request.url.path == "/v5/place/text":
            assert params["keywords"] == query
            params["keywords"] = CANONICAL
        recorded = next(call for call in FIXTURE["calls"]
                        if call["path"] == request.url.path and call["query"] == params)
        response = deepcopy(recorded["response"])
        if candidates is not None and request.url.path == "/v5/place/text":
            response.update(pois=candidates, count=str(len(candidates)))
        return httpx.Response(200, json=response)
    return httpx.AsyncClient(transport=httpx.MockTransport(reply))


def test_official_history_and_jintian_names_identify_one_futian_venue():
    knowledge = load_city_knowledge()
    assert knowledge.issues == ()
    for name in (CANONICAL, ALIAS):
        entry = knowledge.query_lookup(city="深圳", name=name).unique
        assert entry and entry.entry_id == "sz-museum-v1:2"
        assert entry.canonical_name == CANONICAL
        assert entry.district == "福田区"
        assert ALIAS in entry.aliases
        assert any(source["url"] == OFFICIAL_GUIDE for source in entry.sources)
    for name in ("深圳博物馆", "深圳博物馆同心路馆", "深圳博物馆古代艺术馆",
                 "深圳博物馆未知分馆", "深圳博物馆新馆", f"{ALIAS}地下停车点"):
        assert all(entry.entry_id != "sz-museum-v1:2"
                   for entry in knowledge.query_lookup(city="深圳", name=name).matches)
    assert knowledge.query_lookup(city="上海", name=ALIAS).unique is None


@pytest.mark.asyncio
async def test_recorded_original_query_confirms_jintian_venue_without_a_second_poi_search():
    observed = []
    async with fixed_client(observed) as client:
        result = await AmapPlaceResolver(api_key="fixed-only", client=client).resolve(**FIXTURE["query"])
    assert result.place and result.place.canonical_place_id == VENUE_ID
    assert result.place.name == ALIAS
    assert result.place.area_or_address == "福中路市民中心A区(市民中心地铁站B口步行350米)"
    assert result.receipt["selection_tier"] == "SAFE_ALIAS_EXACT"
    assert result.receipt["lexicon_district_constraint"] is True
    assert result.receipt["adcode"] == "440304"
    assert result.receipt["external_calls"] == 1
    assert len(observed) == 3  # Two recorded admin lookups, one recorded POI query.


@pytest.mark.asyncio
async def test_removing_only_official_alias_reproduces_original_pending(monkeypatch):
    knowledge = load_city_knowledge()
    old = CityKnowledge(tuple(replace(e, aliases=()) if e.entity_id == "sz-museum-v1:2" else e
                              for e in knowledge.entities), knowledge.issues, knowledge.versions)
    monkeypatch.setattr("app.trip_understanding.amap_place.get_city_knowledge", lambda: old)
    observed = []
    async with fixed_client(observed) as client:
        result = await AmapPlaceResolver(api_key="fixed-only", client=client).resolve(**FIXTURE["query"])
    assert result.place is None
    assert result.receipt["selection_tier"] == "NO_VALID_CANDIDATE"
    assert result.receipt["provider_result_count"] == 3
    assert len(observed) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "parking_only", "other_branch", "unknown_branch", "wrong_city", "wrong_district",
    "internal_facility", "ambiguous", "wrong_category", "parent_for_unknown_branch",
])
async def test_official_alias_does_not_weaken_branch_or_identity_checks(mutation):
    rows = deepcopy(FIXTURE["calls"][-1]["response"]["pois"])
    parent = next(row for row in rows if row["id"] == VENUE_ID)
    query = CANONICAL
    if mutation == "parking_only":
        rows = [row for row in rows if row["id"] != VENUE_ID]
    elif mutation == "other_branch":
        rows = [{**parent, "name": "深圳博物馆同心路馆", "address": "同心路6号"}]
    elif mutation == "unknown_branch":
        rows = [{**parent, "name": "深圳博物馆未知分馆"}]
    elif mutation == "wrong_city":
        rows = [{**parent, "pname": "上海市", "cityname": "上海市", "adname": "黄浦区",
                 "adcode": "310101", "location": "121.48,31.23"}]
    elif mutation == "wrong_district":
        rows = [{**parent, "adname": "南山区", "adcode": "440305"}]
    elif mutation == "internal_facility":
        rows = [{**parent, "name": f"{ALIAS}-寄存处", "business": {"alias": CANONICAL},
                 "typecode": "200000", "type": "公共设施;公共设施;公共设施"}]
    elif mutation == "ambiguous":
        rows = [parent, {**parent, "id": "controlled-second-venue", "address": "另一个地址"}]
    elif mutation == "wrong_category":
        rows = [{**parent, "typecode": "140500", "type": "科教文化服务;图书馆;图书馆"}]
    elif mutation == "parent_for_unknown_branch":
        query = "深圳博物馆未知分馆"
        rows = [{**parent, "business": {"alias": query}}]
    async with fixed_client([], candidates=rows, query=query) as client:
        result = await AmapPlaceResolver(api_key="fixed-only", client=client).resolve(
            city="深圳", atomic_place_name=query, category_hint="景点")
    assert result.place is None


@pytest.mark.asyncio
async def test_full_public_projection_keeps_source_branch_and_confirmed_visitor_venue():
    source = f"深圳一日游。\nDay1：参观{CANONICAL}。"
    plan = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "深圳", "activities": [
        {"source_quote": CANONICAL, "place_name": CANONICAL, "role": "PLANNED", "day_index": 1, "category": "景点"},
    ]}))
    async with fixed_client([]) as client:
        result = await TripUnderstandingPipeline(None, AmapPlaceResolver(api_key="fixed-only", client=client)).run(
            source, prepared_plan=plan)
    assert result.proposal.mentions[0].atomic_place_name == CANONICAL
    assert result.proposal.mentions[0].role.value == "PLANNED"
    assert result.proposal.mentions[0].day_index == 1
    assert result.activities[0].place.canonical_place_id == VENUE_ID
    assert result.public_result.days[0].activities[0].name == ALIAS
    assert result.public_result.days[0].activities[0].status == "READY"
    assert result.public_result.coverage.complete is True
