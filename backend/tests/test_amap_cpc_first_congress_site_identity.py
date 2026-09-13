"""One official site alias; saved real responses, no new model or map calls.

The new canonical search is explicitly served the unchanged candidate response
captured for the old shorthand. This tests downstream identity handling, not
whether a fresh canonical-name request would return the same provider response.
"""
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.city_knowledge import CityKnowledge, load_city_knowledge
from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_anonymous_meal_context import replay_saved_shanghai_poi_response
from tests.test_semantic_supplement_budget import Client


SOURCE_URL = "https://www.zgyd1921.com/about.html"
ENTITY_ID = "city-v2:shanghai:cpc-first-congress-site"
CANONICAL = "中国共产党第一次全国代表大会会址"
ALIAS = "中共一大会址"
SITE_ID = "B0MGOCVPM6"
SAVED = json.loads((Path(__file__).parent / "fixtures/live_owner_shanghai_meal_context.json").read_text(encoding="utf-8"))


def source_rows():
    return deepcopy(next(c["response"]["pois"] for c in SAVED["place_calls"]
                         if c["query"].get("keywords") == ALIAS and c["query"].get("types")))


def fixed_client(calls, rows=None):
    def reply(request):
        params = {k: v for k, v in request.url.params.multi_items() if k != "key"}
        if rows is not None:
            calls.append(params)
            return httpx.Response(200, json={"status": "1", "infocode": "10000", "pois": rows})
        return replay_saved_shanghai_poi_response(request, calls, saved=SAVED)
    return httpx.AsyncClient(transport=httpx.MockTransport(reply))


def test_official_site_alias_is_distinct_from_memorial_and_other_managed_sites():
    knowledge = load_city_knowledge()
    assert knowledge.issues == ()
    for name in (ALIAS, CANONICAL):
        entry = knowledge.query_lookup(city="上海", name=name).unique
        assert entry and entry.entry_id == ENTITY_ID and entry.canonical_name == CANONICAL
        assert entry.district == "黄浦区" and entry.aliases == (ALIAS,)
        assert any(s["url"] == SOURCE_URL for s in entry.sources)
    for name in ("中共一大纪念馆", "中共一大纪念馆新馆", "中国共产党第一次全国代表大会纪念馆",
                 "中共一大纪念馆一大广场", "中共一大宿舍旧址", "中共一大会址纪念馆保管部",
                 "中共二大会址", "中共代表团驻沪办事处旧址", "中共一大会址停车场"):
        assert all(x.entry_id != ENTITY_ID for x in knowledge.query_lookup(city="上海", name=name).matches)
    assert knowledge.query_lookup(city="北京", name=ALIAS).unique is None


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [ALIAS, CANONICAL])
async def test_recorded_candidates_confirm_only_the_site_at_xingye_road_76(query):
    calls = []
    async with fixed_client(calls) as client:
        result = await AmapPlaceResolver(api_key="fixed-only", client=client).resolve(
            city="上海", atomic_place_name=query, category_hint="景点")
    assert result.place and result.place.canonical_place_id == SITE_ID
    assert result.place.name == CANONICAL and result.place.area_or_address == "兴业路76号"
    assert result.receipt["adcode"] == "310101" and result.receipt["lexicon_district_constraint"] is True
    assert len(calls) == 1 and calls[0]["request_query"]["keywords"] == CANONICAL
    assert calls[0]["request_query"]["region"] == "上海" and calls[0]["response_policy"] == "FIXED_RESPONSE_REUSE"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "missing", "new_museum_only", "new_museum_alias", "wrong_city", "wrong_district",
    "parking", "internal", "duplicate", "query_new_museum", "query_other_city",
    "other_congress_alias", "unrelated_organization",
])
async def test_official_site_alias_keeps_candidate_boundaries(mutation):
    rows = source_rows()
    site = next(r for r in rows if r["id"] == SITE_ID)
    query, city = ALIAS, "上海"
    if mutation == "missing":
        rows = [r for r in rows if r["id"] != SITE_ID]
    elif mutation in {"new_museum_only", "new_museum_alias"}:
        rows = [r for r in rows if r["id"] == "B0MGOCVX94"]
        if mutation == "new_museum_alias":
            rows[0]["business"] = {"alias": ALIAS}
    elif mutation == "wrong_city":
        site.update(cityname="北京市", pname="北京市", adname="东城区", adcode="110101", location="116.4,39.9")
    elif mutation == "wrong_district":
        site.update(adname="浦东新区", adcode="310115")
    elif mutation in {"parking", "internal"}:
        rows = [site]
        site["name"] += "-停车场" if mutation == "parking" else "-会议室"
        site["business"] = {"alias": ALIAS}
        if mutation == "parking":
            site.update(typecode="150904", type="交通设施服务;停车场;公共停车场")
    elif mutation == "duplicate":
        rows.append({**site, "id": "controlled-other-site"})
    elif mutation == "query_new_museum":
        query = "中共一大纪念馆新馆"
    elif mutation == "query_other_city":
        city = "北京"
    elif mutation == "other_congress_alias":
        rows = [r for r in rows if r["name"] == "中国共产党第二次全国代表大会会址"]
        assert rows
        rows[0]["business"] = {"alias": ALIAS}
    elif mutation == "unrelated_organization":
        rows = [{**site, "name": "中国共产党第一次全国代表大会研究中心", "business": {"alias": ALIAS}}]
    async with fixed_client([], rows) as client:
        result = await AmapPlaceResolver(api_key="fixed-only", client=client).resolve(
            city=city, atomic_place_name=query, category_hint="景点")
    assert result.place is None


async def build_site_alias_result():
    client, calls = Client(SAVED["response"]), []
    provider = ExperienceQwenProvider(api_key="fixed", base_url="https://offline.invalid", model="fixed",
        client=client, deadline_seconds=10, enable_source_visits=False)
    async with fixed_client(calls) as transport:
        result = await TripUnderstandingPipeline(provider, AmapPlaceResolver(api_key="fixed", client=transport)).run(SAVED["source"])
    assert len(client.calls) == 1
    return result, calls


@pytest.mark.asyncio
async def test_saved_full_shanghai_plan_changes_only_this_identity(monkeypatch):
    knowledge = load_city_knowledge()
    original = CityKnowledge(tuple(e for e in knowledge.entities if e.entity_id != ENTITY_ID),
                             knowledge.issues, knowledge.versions)
    with monkeypatch.context() as patch:
        patch.setattr("app.trip_understanding.amap_place.get_city_knowledge", lambda: original)
        before, before_calls = await build_site_alias_result()
    after, after_calls = await build_site_alias_result()
    assert len(before_calls) == 20 and len(after_calls) == 19
    assert before.proposal.mentions == after.proposal.mentions
    assert before.proposal.day_count == after.proposal.day_count
    assert before.proposal.day_labels == after.proposal.day_labels
    assert before.proposal.unprocessed_count == after.proposal.unprocessed_count
    assert [len(d.activities) for d in before.public_result.days] == [7, 10, 0]
    assert [len(d.activities) for d in after.public_result.days] == [7, 10, 0]
    assert sum(c.status == "READY" for d in before.public_result.days for c in d.activities) == 13
    assert sum(c.status == "READY" for d in after.public_result.days for c in d.activities) == 14
    for old, new in zip(before.activities, after.activities, strict=True):
        assert old.compiled.mention == new.compiled.mention
        if new.compiled.mention.atomic_place_name == ALIAS:
            assert old.place is None and new.place.canonical_place_id == SITE_ID
        else:
            assert (old.place.canonical_place_id if old.place else None) == (new.place.canonical_place_id if new.place else None)
    # Each run issues fresh opaque edit tokens. Compare their referenced source
    # occurrences, preserving all anchors rather than dropping token fields.
    tokens = {new.compiled.public_activity_token: old.compiled.public_activity_token
              for old, new in zip(before.activities, after.activities, strict=True)}

    def same_occurrence_tokens(value):
        if isinstance(value, str):
            return tokens.get(value, value)
        if isinstance(value, list):
            return [same_occurrence_tokens(v) for v in value]
        if isinstance(value, dict):
            return {k: same_occurrence_tokens(v) for k, v in value.items()}
        return value

    for old, new in zip(before.public_result.days, after.public_result.days, strict=True):
        assert [a.model_dump() for a in old.alternatives] == same_occurrence_tokens([a.model_dump() for a in new.alternatives])
        assert [s.model_dump() for s in old.meal_slots] == same_occurrence_tokens([s.model_dump() for s in new.meal_slots])
        assert old.label == new.label and old.unprocessed_count == new.unprocessed_count
        for left, right in zip(old.activities, new.activities, strict=True):
            if left.name == ALIAS:
                assert right.name == CANONICAL and right.status == "READY" and right.city == "上海"
                assert right.source_details == left.source_details and right.meal_role == left.meal_role
            else:
                assert left.model_dump() == same_occurrence_tokens(right.model_dump())
    assert after.public_result.coverage.complete is False
