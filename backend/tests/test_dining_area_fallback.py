"""Public map identities plus synthetic negative cases; not live quality scores."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest

from app.trip_understanding import dining_areas as areas
from app.trip_understanding.candidates import CandidatePlace, GCJ02Position
from app.trip_understanding.daily_dining import build_daily_meals, project_daily_meals
from tests.test_daily_dining import card, day_context
from tests.test_dining_recommendations import restaurant

FIXTURE = json.loads((Path(__file__).parent / "fixtures/dining_area_poi_20260907.json").read_text(encoding="utf-8"))


def area_restaurant(fact, **changes):
    return CandidatePlace(canonical_place_id="amap:synthetic-meal", name="合成餐厅", category="餐饮",
                          city=fact.city, area_or_address="合成测试地址",
                          position=GCJ02Position(longitude=fact.longitude + .001, latitude=fact.latitude)).model_copy(update=changes)


@pytest.fixture(autouse=True)
def isolated_cache():
    areas._AREA_CACHE.clear()
    areas.load_dining_area_facts.cache_clear()
    yield
    areas._AREA_CACHE.clear()
    areas.load_dining_area_facts.cache_clear()


def mock_provider(monkeypatch, handler):
    native_client = httpx.AsyncClient
    monkeypatch.setattr(areas.httpx, "AsyncClient", lambda **kwargs: native_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(areas, "get_settings", lambda: NS(amap_api_key="synthetic-key", trip_understanding_provider_mode="live"))


def test_area_facts_have_independent_sources_and_bad_rows_are_isolated(tmp_path):
    original = json.loads(areas.AREA_FACTS_PATH.read_text(encoding="utf-8"))
    assert len(areas.load_dining_area_facts()) == len(original["areas"]) == 4
    good = original["areas"][0]
    original["areas"] += [{**good, "fact_id": "unreviewed", "review_status": "lead"},
                           {**good, "fact_id": "nan", "longitude": "NaN"},
                           {**good, "fact_id": "unsourced", "sources": good["sources"][:1]},
                           {**good, "fact_id": "station", "typecode": "150500"}]
    file = tmp_path / "facts.json"
    file.write_text(json.dumps(original), encoding="utf-8")
    assert len(areas.load_dining_area_facts(file)) == 4


def test_area_dataset_expands_through_city_configuration_without_record_quota(tmp_path, monkeypatch):
    original = json.loads(areas.AREA_FACTS_PATH.read_text(encoding="utf-8"))
    good = original["areas"][0]
    new_city = {**good, "city": "成都", "cityname": "成都市", "pname": "四川省", "adname": "武侯区",
                "adcode": "510107", "longitude": 104.06, "latitude": 30.65}
    original["areas"] = [{**new_city, "fact_id": f"synthetic-{index}", "poi_id": f"B0SYN{index:06}"}
                         for index in range(205)]
    original["areas"].insert(4, {"malformed": True})
    file = tmp_path / "expanded.json"
    file.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(areas, "get_city_knowledge", lambda: NS(versions={"成都": "synthetic-test-version"}))
    assert len(areas.load_dining_area_facts(file)) == 205


@pytest.mark.parametrize("change", [{"id": "another-id"}, {"name": "同名停车场"}, {"cityname": "广州市"},
                                    {"adcode": "440304"}, {"typecode": "150904"}, {"type": "交通设施服务;停车场;公共停车场"},
                                    {"location": "NaN,22.524077"}, {"location": "114.1,22.524077"}])
def test_area_identity_does_not_accept_wrong_city_branch_type_or_moved_position(change):
    fact = next(f for f in areas.load_dining_area_facts() if f.city == "深圳")
    row = next(r for r in FIXTURE["pois"] if r["id"] == fact.poi_id)
    assert areas._verified_position(row, fact)
    assert areas._verified_position({**row, **change}, fact) is None


@pytest.mark.asyncio
async def test_nearby_area_is_not_membership_and_success_cache_does_not_spend_again(monkeypatch):
    fact = next(f for f in areas.load_dining_area_facts() if f.city == "深圳")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "1", "pois": [r for r in FIXTURE["pois"] if r["id"] == fact.poi_id]})
    mock_provider(monkeypatch, handler)
    receipt = {}
    result = await areas.nearby_dining_area(area_restaurant(fact), receipt=receipt)
    assert result["area_relation"] == "NEARBY" and 0 < result["area_distance_m"] < 1200
    assert "business_area" not in result and receipt["area_http_attempts"] == 1
    second = {}
    assert await areas.nearby_dining_area(area_restaurant(fact), receipt=second) == result
    assert second["area_http_attempts"] == 0 and len(calls) == 1


@pytest.mark.asyncio
async def test_failed_area_is_not_retried_or_cached_and_city_radius_are_checked_first(monkeypatch):
    fact = next(f for f in areas.load_dining_area_facts() if f.city == "上海")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503)
    mock_provider(monkeypatch, handler)
    for _ in range(2):
        receipt = {}
        assert await areas.nearby_dining_area(area_restaurant(fact), receipt=receipt) is None
        assert receipt["area_http_attempts"] == 1
    for change in ({"city": "北京"}, {"business_area": "原地图区域"},
                   {"position": GCJ02Position(longitude=121.6, latitude=31.2)}):
        receipt = {}
        assert await areas.nearby_dining_area(area_restaurant(fact, **change), receipt=receipt) is None
        assert receipt["area_http_attempts"] == 0
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_expired_area_success_is_revalidated_with_one_request(monkeypatch):
    fact = next(f for f in areas.load_dining_area_facts() if f.city == "上海")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "1", "pois": [r for r in FIXTURE["pois"] if r["id"] == fact.poi_id]})
    mock_provider(monkeypatch, handler)
    clock = [100.0]
    monkeypatch.setattr(areas, "time", NS(monotonic=lambda: clock[0]))
    await areas.nearby_dining_area(area_restaurant(fact), receipt={})
    clock[0] += areas.AREA_CACHE_TTL_SECONDS + 1
    await areas.nearby_dining_area(area_restaurant(fact), receipt={})
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_area_success_cache_has_a_bounded_capacity(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "1", "pois": [r for r in FIXTURE["pois"]
            if r["id"] == request.url.params["id"]]})
    mock_provider(monkeypatch, handler)
    monkeypatch.setattr(areas, "AREA_CACHE_CAPACITY", 1)
    facts = [next(f for f in areas.load_dining_area_facts() if f.city == city) for city in ("上海", "深圳")]
    for fact in [*facts, facts[0]]:
        assert await areas.nearby_dining_area(area_restaurant(fact), receipt={})
        assert len(areas._AREA_CACHE) == 1
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_area_lookup_uses_final_top_choice_and_preserves_original_provider_area():
    day, stops = day_context([card("合成景点", 0)])
    top = restaurant().model_copy(update={"name": "A合成餐厅"})
    backup = restaurant().model_copy(update={"name": "B合成餐厅", "canonical_place_id": "amap:backup", "business_area": "备选所属区域"})
    calls = []
    async def search(**_):
        return [top, backup]
    async def fallback(place, *, receipt, **_):
        calls.append(place.name)
        receipt["area_http_attempts"] = 1
        return {"area": "合成附近商业广场", "area_relation": "NEARBY", "area_distance_m": 120}
    stats = {}
    rows = await build_daily_meals(NS(days=[day]), NS(stops=stops), search=search, area_search=fallback, stats=stats)
    assert calls == ["A合成餐厅"] and stats["area_http_attempts"] == 1
    public = project_daily_meals(rows, public_resource_id="synthetic-resource", etag="synthetic-etag")[0]
    assert public.area_relation == "NEARBY" and public.area_distance_m == 120
    assert public.candidates[0].business_area is None
    top.business_area = "地图返回区域"
    rows = await build_daily_meals(NS(days=[day]), NS(stops=stops), search=search, area_search=fallback)
    assert rows[0]["area_relation"] == "PROVIDER_AREA" and calls == ["A合成餐厅"]


@pytest.mark.asyncio
async def test_area_deadline_keeps_verified_restaurants_and_counts_started_request():
    day, stops = day_context([card("合成景点", 0)])
    async def search(**_):
        return [restaurant()]
    async def slow(_place, *, receipt, **_):
        receipt["area_http_attempts"] = 1
        await asyncio.sleep(1)
    stats = {}
    rows = await build_daily_meals(NS(days=[day]), NS(stops=stops), search=search, area_search=slow,
                                   deadline_seconds=.03, stats=stats)
    assert rows[0]["status"] == "AVAILABLE" and len(rows[0]["candidates"]) == 1
    assert rows[0]["area"] is None and stats["area_http_attempts"] == 1
