"""Synthetic regressions for safe automatic defaults, independent of owner text."""

import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver
from tests.test_landmark_matching import poi
from tests.test_soft_city_evidence import entry, proposal_for, use_entries
from app.trip_understanding.pipeline import _model_activity_cities
from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft


@pytest.mark.parametrize("food", ["老北京风味", "北京铜锅涮肉", "北京烤鸭"])
def test_cuisine_city_evidence_does_not_turn_soft_destination_into_explicit(food, monkeypatch):
    use_entries(monkeypatch, [entry("景山公园", city="北京"), entry("颐和园", city="北京")])
    source = f"Day1：景山公园；颐和园；吃{food}。"
    draft = SemanticDraft.model_validate(
        {
            "destination": "北京",
            "activities": [
                {"source_quote": name, "place_name": name, "role": "PLANNED", "category": "景点", "day_index": 1}
                for name in ["景山公园", "颐和园"]
            ]
            + [
                {
                    "source_quote": f"吃{food}",
                    "place_name": None,
                    "role": "PLANNED",
                    "category": "餐饮",
                    "day_index": 1,
                    "city": "北京",
                    "city_evidence": food,
                }
            ],
        }
    )
    proposal = proposal_from_draft(source, draft)
    assert proposal.destination_basis.value == "SOFT_ASSUMPTION"
    assert all(_model_activity_cities(source, proposal, item) == ("北京",) for item in proposal.mentions[:2])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,label,matched",
    [
        ("110210|110105", "风景名胜;风景名胜;城市广场|风景名胜;公园广场;城市广场", True),
        ("110201|140100", "风景名胜;风景名胜;世界遗产|科教文化服务;博物馆;博物馆", True),
        ("110201|100100", "风景名胜;风景名胜;世界遗产|住宿服务;宾馆酒店;宾馆酒店", False),
        ("110201|140100", "风景名胜;风景名胜;世界遗产|餐饮服务;中餐厅;中餐厅", False),
    ],
)
async def test_multiple_known_visitor_categories_require_each_pair_to_agree(code, label, matched):
    result = await resolve([shanghai("星河文化园", code, label)], "星河文化园")
    assert bool(result.place) is matched


@pytest.mark.parametrize("suffix", ["周边吃饭", "附近休息", "沿线散步", "逛街"])
def test_embedded_road_city_in_description_does_not_disable_all_searches(suffix):
    source, proposal = proposal_for(["上海文化馆", "武康路", "豫园"], prefix=f"南京西路{suffix}。")
    assert all(_model_activity_cities(source, proposal, item) == ("上海",) for item in proposal.mentions)
    assert proposal.destination_basis.value == "SOFT_ASSUMPTION"


@pytest.mark.parametrize("prefix", ["南京西路附近，第二天去杭州。", "南京西路过后去南京。", "南京与上海两地。"])
def test_road_boundary_does_not_hide_real_cross_city(prefix):
    source, proposal = proposal_for(["上海文化馆", "武康路", "豫园"], prefix=prefix)
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("目的地待确认",)


async def resolve(rows, query, category="景点"):
    async def handle(request):
        return httpx.Response(200, json={"status": "1", "pois": rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        return await AmapPlaceResolver(api_key="synthetic", client=client).resolve(
            city="上海", atomic_place_name=query, category_hint=category
        )


def shanghai(name, code="110200", label="风景名胜;风景名胜;风景名胜", **extra):
    return poi(name, code, label, city="上海", district="黄浦区", adcode="310101", **extra)


@pytest.mark.asyncio
async def test_first_search_hit_is_not_automatically_the_selected_place():
    rows = [
        shanghai("远郊公园"),
        shanghai("南京路步行街-打卡点"),
        shanghai("南京路步行街", "061001", "购物服务;特色商业街;步行街"),
    ]
    result = await resolve(rows, "南京路步行街")
    assert result.place and result.place.name == "南京路步行街"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["上海新天地", "上海田子坊"])
async def test_commercial_street_exact_name_and_type_agree(name):
    result = await resolve([shanghai(name, "061000", "购物服务;特色商业街;特色商业街")], name.removeprefix("上海"))
    assert result.place and result.place.name == name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,label,matched",
    [
        ("110203|190307", "风景名胜;风景名胜;省级景点|地名地址信息;交通地名;桥", True),
        ("110203|100100", "风景名胜;风景名胜;省级景点|住宿服务;宾馆酒店;宾馆酒店", False),
        ("110203|190307", "风景名胜;风景名胜;省级景点", False),
        ("110203|190307", "风景名胜;风景名胜;省级景点|餐饮服务;中餐厅;中餐厅", False),
    ],
)
async def test_bridge_multiple_types_must_all_agree(code, label, matched):
    result = await resolve([shanghai("外白渡桥", code, label)], "外白渡桥")
    assert bool(result.place) is matched


@pytest.mark.asyncio
async def test_unique_named_road_can_match_without_inventing_an_attraction_name():
    result = await resolve([shanghai("星河路", "190301", "地名地址信息;交通地名;道路名")], "星河路", "地点")
    assert result.place and result.place.name == "星河路"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["same", "far", "district", "hotel", "wrong_city", "child"])
async def test_only_nearby_segments_of_same_road_can_use_first_eligible(mutation):
    first = shanghai("星河路", "190301", "地名地址信息;交通地名;道路名")
    second = {**first, "id": "second-road-segment", "location": "121.4801,31.2301"}
    if mutation == "far":
        second["location"] = "121.58,31.33"
    if mutation == "district":
        second.update(adname="浦东新区", adcode="310115")
    if mutation == "hotel":
        for row in (first, second):
            row.update(name="星河酒店", typecode="100100", type="住宿服务;宾馆酒店;宾馆酒店")
    if mutation == "wrong_city":
        for row in (first, second):
            row.update(cityname="北京市", pname="北京市", adname="东城区", adcode="110101")
    if mutation == "child":
        for row in (first, second):
            row.update(name="星河路(公交站)", typecode="150700", type="交通设施服务;公交车站;公交车站相关")
    result = await resolve(
        [first, second], "星河酒店" if mutation == "hotel" else "星河路", "住宿" if mutation == "hotel" else "地点"
    )
    assert bool(result.place) is (mutation == "same")
    if result.place:
        assert result.place.canonical_place_id == first["id"]


@pytest.mark.asyncio
async def test_reviewed_alias_resolves_parent_not_related_child():
    rows = [shanghai("万国建筑博览群-拍摄点"), shanghai("万国建筑博览群")]
    result = await resolve(rows, "外滩万国建筑群")
    assert result.place and result.place.name == "万国建筑博览群"


@pytest.mark.asyncio
async def test_unavailable_search_keeps_pending():
    result = await resolve([], "星河路", "地点")
    assert result.place is None and result.receipt["status"] == "NO_UNIQUE_MATCH"
