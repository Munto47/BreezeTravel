"""Synthetic POI identity boundaries; no network, user text, or live credentials."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.schemas.place import PlaceCategory
from app.trip_understanding import amap_place, candidates
from app.trip_understanding._three_city_place_lexicon import ThreeCityPlaceLexicon


_CITIES = {
    "北京": ("北京市", "朝阳区", "110105", "116.40,39.99"),
    "上海": ("上海市", "黄浦区", "310101", "121.48,31.23"),
    "杭州": ("浙江省", "上城区", "330102", "120.17,30.25"),
}


def _poi(name: object = "星河博物馆", *, city: str = "上海", **overrides) -> dict:
    province, district, adcode, location = _CITIES[city]
    return {
        "id": "synthetic-museum",
        "name": name,
        "typecode": "140100",
        "type": "科教文化服务;博物馆;博物馆",
        "cityname": city + "市",
        "pname": province,
        "adname": district,
        "adcode": adcode,
        "location": location,
        "address": "合成测试地址",
        **overrides,
    }


def _decision(rows: list[dict], *, city: str = "上海", query: str = "星河博物馆",
              category: PlaceCategory = PlaceCategory.ATTRACTION):
    return amap_place._evaluate_candidates(
        rows, city=city, canonical_name=query, safe_aliases=(),
        expected_category=category, expected_district=None, atomic=query,
    )


def _alias_payload(alias: str, field: str) -> dict:
    return {"alias": alias} if field == "alias" else {"business": {"alias": alias}}


@pytest.mark.parametrize("city,foreign_location", [
    ("北京", "121.48,31.23"),
    ("北京", "120.17,30.25"),
    ("上海", "116.40,39.99"),
    ("上海", "120.17,30.25"),
    ("杭州", "116.40,39.99"),
    ("杭州", "121.48,31.23"),
])
def test_city_metadata_cannot_rescue_coordinates_in_another_city(city, foreign_location):
    assert _decision([_poi(city=city, location=foreign_location)], city=city).selected is None


@pytest.mark.parametrize("city", _CITIES)
@pytest.mark.parametrize("location", [
    "nan,31.23", "121.48,nan", "inf,31.23", "121.48,-inf",
    "181,31.23", "121.48,91", "", None, "121.48",
])
def test_nonfinite_malformed_or_global_outside_coordinates_remain_unconfirmed(city, location):
    assert _decision([_poi(city=city, location=location)], city=city).selected is None


# These are the existing coarse city envelopes used by manual POI selection.
# The check must reject an outside point without rejecting its inside neighbor.
@pytest.mark.parametrize("city,inside,outside", [
    ("北京", "115.401,39.9", "115.399,39.9"),
    ("北京", "117.599,39.9", "117.601,39.9"),
    ("北京", "116.4,39.401", "116.4,39.399"),
    ("北京", "116.4,41.099", "116.4,41.101"),
    ("上海", "120.801,31.2", "120.799,31.2"),
    ("上海", "122.299,31.2", "122.301,31.2"),
    ("上海", "121.4,30.601", "121.4,30.599"),
    ("上海", "121.4,31.899", "121.4,31.901"),
    ("杭州", "118.301,30.2", "118.299,30.2"),
    ("杭州", "120.799,30.2", "120.801,30.2"),
    ("杭州", "120.1,29.101", "120.1,29.099"),
    ("杭州", "120.1,30.799", "120.1,30.801"),
])
def test_coarse_city_boundary_keeps_inside_and_rejects_outside(city, inside, outside):
    assert _decision([_poi(city=city, location=inside)], city=city).selected is not None
    assert _decision([_poi(city=city, location=outside)], city=city).selected is None


@pytest.mark.parametrize("alias_field", ["alias", "business.alias"])
@pytest.mark.parametrize("query,name", [
    ("星河博物馆", "星河博物馆（南馆）"),
    ("星河博物馆", "星河博物馆北区"),
    ("星河博物馆", "星河博物馆珍宝馆"),
    ("星河博物馆", "星河博物馆第二展厅"),
    ("星河博物馆", "星河博物馆公交站"),
    ("星河博物馆（南馆）", "星河博物馆（北馆）"),
    ("星河博物馆（新馆）", "星河博物馆（旧馆）"),
    ("星河博物馆（南馆）", "星河博物馆"),
])
def test_provider_alias_cannot_add_remove_or_change_venue_scope(alias_field, query, name):
    row = _poi(name, **_alias_payload(query, alias_field))
    assert _decision([row], query=query).selected is None


@pytest.mark.parametrize("alias_field", ["alias", "business.alias"])
@pytest.mark.parametrize("query,name", [
    ("星河餐厅", "星河餐厅（黄浦店）"),
    ("星河餐厅（黄浦店）", "星河餐厅（徐汇店）"),
    ("星河餐厅（黄浦店）", "星河餐厅"),
])
def test_restaurant_alias_cannot_choose_a_different_or_unspecified_branch(alias_field, query, name):
    row = _poi(name, typecode="050100", type="餐饮服务;中餐厅;中餐厅",
               **_alias_payload(query, alias_field))
    assert _decision([row], query=query, category=PlaceCategory.FOOD).selected is None


@pytest.mark.parametrize("alias_field", ["alias", "business.alias"])
@pytest.mark.parametrize("name", [
    "https://example.test/星河博物馆",
    "星河博物馆需要提前预约",
    "星河博物馆开放时间说明",
    "星河博物馆参观约2小时",
    "",
    "   ",
    None,
])
def test_correct_alias_cannot_rescue_a_non_place_primary_name(alias_field, name):
    row = _poi(name, **_alias_payload("星河博物馆", alias_field))
    assert _decision([row]).selected is None


@pytest.mark.parametrize("query,name", [
    ("星河博物馆（南馆）", "星河博物馆（南馆）"),
    ("星河博物馆（南馆）", "星河博物馆(南馆)"),
    ("星河博物馆（南馆）", "星河博物馆南馆"),
    ("星河博物馆南馆", "星河博物馆（南馆）"),
    ("星河博物馆（南馆）", "星河博物馆·南馆"),
    ("星河博物馆-南馆", "星河博物馆（南馆）"),
    ("星河博物馆（旧馆）", "星河博物馆—旧馆"),
])
def test_formatting_variants_keep_the_same_explicit_venue_scope(query, name):
    decision = _decision([_poi(name)], query=query)
    assert decision.selected is not None
    assert decision.selected.raw["name"] == name


@pytest.mark.parametrize("city", _CITIES)
@pytest.mark.parametrize("query,name", [
    ("星河博物馆", "星河博物馆"),
    ("星河博物馆（南馆）", "星河博物馆（南馆）"),
])
def test_identical_names_with_distinct_ids_remain_ambiguous(city, query, name):
    first = _poi(name, city=city)
    second = {**first, "id": "synthetic-other-museum"}
    decision = _decision([first, second], city=city, query=query)
    assert decision.selected is None
    assert decision.metrics["category_compatible_candidate_count"] == 2


@pytest.mark.parametrize("city", _CITIES)
def test_repeated_identical_provider_row_does_not_create_false_ambiguity(city):
    row = _poi(city=city)
    assert _decision([row, dict(row)], city=city).selected is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"location": "116.397,39.917"},
    {"name": "星河博物馆（南馆）", "alias": "星河博物馆"},
    {"name": "https://example.test/星河博物馆", "alias": "星河博物馆"},
    {"name": "星河博物馆需要提前预约", "alias": "星河博物馆"},
])
async def test_rejected_identity_never_becomes_an_auto_matched_resolver_result(monkeypatch, overrides):
    monkeypatch.setattr(amap_place, "get_three_city_place_lexicon", lambda: ThreeCityPlaceLexicon(entries=()))

    async def respond(_request):
        return httpx.Response(200, json={"status": "1", "infocode": "10000", "pois": [_poi(**overrides)]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        outcome = await amap_place.AmapPlaceResolver(api_key="synthetic-only", client=client).resolve(
            city="上海", atomic_place_name="星河博物馆", category_hint="景点",
        )
    assert outcome.place is None
    assert outcome.receipt.get("status") != "AUTO_MATCHED"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "https://example.test/星河博物馆",
    "星河博物馆需要提前预约",
])
async def test_manual_candidates_exclude_non_place_names_and_keep_valid_choice(monkeypatch, name):
    monkeypatch.setattr(candidates, "get_settings", lambda: SimpleNamespace(
        amap_api_key="synthetic-only", trip_understanding_provider_mode="live",
    ))

    async def query(_self, **_kwargs):
        return [_poi(name, id="synthetic-invalid-name"), _poi()], {}

    monkeypatch.setattr(amap_place.AmapPlaceResolver, "_query_provider", query)
    rows = await candidates.search_candidates(city="上海", query="星河博物馆", category_hint="景点")
    assert rows is not None
    assert [row.name for row in rows] == ["星河博物馆"]
