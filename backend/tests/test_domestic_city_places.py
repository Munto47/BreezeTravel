"""Synthetic administrative responses; owner guides are not regression data."""
import httpx
import pytest
from types import SimpleNamespace

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.candidates import CandidateSearchRequest
from app.trip_understanding.errors import PlaceProviderUnavailableError
from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from tests.test_automatic_place_candidates import resolve as resolve_shanghai, shanghai
from app.trip_understanding.pipeline import _model_activity_cities
from app.trip_understanding import candidates as candidate_search


def poi(**changes):
    return {"id": "synthetic-a", "name": "星湖公园", "pname": "江西省", "cityname": "新余市",
        "adname": "渝水区", "adcode": "360502", "location": "114.9,27.8", "address": "星湖路",
        "typecode": "110101", "type": "风景名胜;公园广场;公园", **changes}


def administrative(keyword):
    if keyword == "360000":
        return {"name": "江西省", "adcode": "360000", "level": "province"}
    return {"name": "新余市", "adcode": "360500", "level": "city", "polyline": "114.5,27.5;115.5,27.5;115.5,28.5;114.5,27.5",
        "districts": [{"name": "渝水区", "adcode": "360502", "level": "district"}]}


async def resolve(row, *, city="新余", district_change=None):
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/district"):
            district = administrative(request.url.params["keywords"])
            if district_change and request.url.params["keywords"] != "360000":
                district.update(district_change)
            return httpx.Response(200, json={"status": "1", "districts": [district]})
        return httpx.Response(200, json={"status": "1", "pois": [row]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        resolver = AmapPlaceResolver(api_key="test-only", client=client)
        outcome = await resolver.resolve(city=city, atomic_place_name="星湖公园", category_hint="景点")
        again = await resolver.resolve(city=city, atomic_place_name="星湖公园", category_hint="景点")
    assert bool(outcome.place) == bool(again.place)
    return outcome, calls


@pytest.mark.asyncio
async def test_other_city_uses_verified_admin_and_reuses_scope():
    outcome, calls = await resolve(poi(), city="新余市")
    assert outcome.place.name == "星湖公园"
    assert len([r for r in calls if r.url.path.endswith("/district")]) == 2
    assert all(r.url.params.get("city_limit") == "true" for r in calls if r.url.path.endswith("/text"))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"cityname": "南昌市"}, {"pname": "湖南省"}, {"adcode": "360102"}, {"adname": "分宜县"},
    {"location": "116.4,39.9"}, {"typecode": "100100", "type": "住宿服务;宾馆酒店;宾馆酒店"},
    {"name": "星湖公园停车场"}, {"cityname": []}, {"location": "nan,27.8"},
])
async def test_other_city_still_rejects_wrong_identity_admin_category_and_coordinates(change):
    outcome, _ = await resolve(poi(**change))
    assert outcome.place is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"name": "南昌市"}, {"level": "province"}, {"districts": None}, {"polyline": "bad"}])
async def test_unverified_city_never_searches_nationwide(change):
    outcome, calls = await resolve(poi(), district_change=change)
    assert outcome.place is None
    assert not any(r.url.path.endswith("/text") for r in calls)


@pytest.mark.asyncio
async def test_administrative_failure_is_typed_and_does_not_leak_key():
    def handle(request):
        return httpx.Response(200, json={"status": "0"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(PlaceProviderUnavailableError) as caught:
            await AmapPlaceResolver(api_key="private-test-key", client=client).resolve(city="新余", atomic_place_name="星湖公园")
    assert caught.value.category == "CITY_SCOPE_UNAVAILABLE"
    assert "private-test-key" not in str(caught.value.provider_binding)


def test_manual_search_accepts_domestic_city_instead_of_three_city_enum():
    assert CandidateSearchRequest(activity_token="x"*24, query="星湖公园", city="新余").city == "新余"


@pytest.mark.parametrize("note", ["必买：**门票 + 船票**", "必点：**鱼丸、米粉**", "小吃：**豆花、烧饼**"])
def test_bold_products_and_dishes_are_not_required_places(note):
    source = f"新余 Day1\n星湖公园。{note}。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "新余", "activities": [
        {"source_quote": "星湖公园", "place_name": "星湖公园", "role": "PLANNED", "day_index": 1, "category": "景点"}]}))
    assert [m.atomic_place_name for m in proposal.mentions] == ["星湖公园"]
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("新余",)


@pytest.mark.asyncio
@pytest.mark.parametrize("query,name,address,accepted", [
    ("青松国家森林公园", "青松森林公园", "青松路", True),
    ("青松国家森林公园", "青松森林公园管理处", "青松路", False),
    ("星河纪念馆", "星湖公园-星河纪念馆", "星湖公园", True),
    ("星河纪念馆", "星湖公园-星河纪念馆", "其他公园", False),
    ("星河纪念馆", "星河纪念馆(新馆)", "星湖公园", False),
])
async def test_name_variants_do_not_accept_management_offices_or_branches(query, name, address, accepted):
    outcome = await resolve_shanghai([{**shanghai(name), "address": address}], query)
    assert bool(outcome.place) is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("second_location,second_district,accepted", [
    ("121.4705,31.2305", "黄浦区", True),
    ("121.49,31.25", "黄浦区", False),
])
async def test_road_visitor_duplicates_require_same_name_and_short_distance(second_location, second_district, accepted):
    rows = [{**shanghai("星河胡同", "190301", "地名地址信息;交通地名;道路名"), "location": "121.47,31.23", "id": "road"},
        {**shanghai("星河胡同"), "location": second_location, "id": "visitor", "adname": second_district}]
    outcome = await resolve_shanghai(rows, "星河胡同")
    assert bool(outcome.place) is accepted
    if accepted:
        assert outcome.place.canonical_place_id == "visitor"


@pytest.mark.parametrize("name,description,role", [
    ("星河米粉", "，本地特色嗦粉", "REFERENCE"),
    ("星河米粉", "，当地特色小吃", "REFERENCE"),
    ("星河粉店", "，本地特色嗦粉", "PLANNED"),
    ("星河米粉", "，这家店就在公园门口", "PLANNED"),
])
def test_same_named_food_shop_requires_a_visit_not_a_dish_description(name, description, role):
    source = f"新余 Day1\n中午吃早餐 / 午饭：**{name}**{description}。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "新余", "activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1, "category": "餐饮"}]}))
    assert proposal.mentions[0].role.value == role


@pytest.mark.asyncio
async def test_town_query_does_not_relabel_child_leisure_business_as_attraction(monkeypatch):
    rows = [shanghai(name, "080500", "体育休闲服务;休闲场所;休闲场所") for name in ("星河小镇", "星河小镇按摩中心")]
    def handle(request):
        return httpx.Response(200, json={"status": "1", "pois": rows})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(AmapPlaceResolver, "_http_client", lambda self: client)
        monkeypatch.setattr(candidate_search, "get_settings", lambda: SimpleNamespace(amap_api_key="test", trip_understanding_provider_mode="live"))
        found = await candidate_search.search_candidates(city="上海", query="星河小镇", category_hint="景点")
    assert [p.name for p in found] == ["星河小镇"]
