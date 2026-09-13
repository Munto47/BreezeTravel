"""Regression of independently reviewed public map candidates, not live tests."""
import copy
import json
from pathlib import Path

import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.city_knowledge import _parse_record, get_city_knowledge
from app.trip_understanding.city_scope import CityScope

CASES = json.loads((Path(__file__).parent / "fixtures/city_focus_poi_20260907.json").read_text(encoding="utf-8"))["cases"]
SCOPES = {
    "杭州": CityScope("杭州市", "浙江省", "330100", (("西湖区", "330106"), ("上城区", "330102")), (119.1, 120.8, 29.5, 30.8)),
    "广州": CityScope("广州市", "广东省", "440100", (("荔湾区", "440103"), ("白云区", "440111"), ("增城区", "440118")), (112.8, 114, 22.5, 24)),
    "深圳": CityScope("深圳市", "广东省", "440300", (("福田区", "440304"), ("南山区", "440305"), ("盐田区", "440308")), (113.7, 114.7, 22.3, 22.9)),
}


async def resolve(case, pois, monkeypatch):
    async def scope(self, city, receipt=None):
        return SCOPES[city]
    monkeypatch.setattr(AmapPlaceResolver, "city_scope", scope)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "1", "pois": pois})
    )) as client:
        return await AmapPlaceResolver(api_key="controlled-test", client=client).resolve(
            city=case["city"], atomic_place_name=case["name"], category_hint="景点")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
async def test_reviewed_aliases_and_types_resolve_exact_subject(case, monkeypatch):
    result = await resolve(case, case["pois"], monkeypatch)
    assert (result.place.canonical_place_id if result.place else None) == case["expected_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["陈家祠", "白云山", "断桥残雪", "曲院风荷", "湖滨银泰in77", "深圳市民中心", "欢乐海岸", "蛇口海上世界"])
@pytest.mark.parametrize("change", ["wrong_city", "child", "hotel", "missing_label", "conflicting_pair"])
async def test_reviewed_identity_never_overrides_subject_admin_or_type(name, change, monkeypatch):
    case = next(c for c in CASES if c["name"] == name)
    row = copy.deepcopy(next(p for p in case["pois"] if p["id"] == case["expected_id"]))
    if change == "wrong_city":
        row.update(cityname="南京市", pname="江苏省", adname="玄武区", adcode="320102")
    elif change == "child":
        row["name"] += "-售票处"
        row["alias"] = name
    elif change == "hotel":
        row.update(typecode="100100", type="住宿服务;宾馆酒店;宾馆酒店")
    elif change == "missing_label":
        row["type"] = ""
    else:
        row["typecode"] += "|100100"
        row["type"] += "|住宿服务;宾馆酒店;宾馆酒店"
    assert (await resolve(case, [row], monkeypatch)).place is None


@pytest.mark.asyncio
async def test_two_actual_visitor_identities_still_require_choice(monkeypatch):
    case = next(c for c in CASES if c["name"] == "前门大街")
    rows = copy.deepcopy(case["pois"])
    first = next(p for p in rows if p["id"] == case["expected_id"])
    rows.append({**first, "id": "a-second-visitor-poi", "location": "116.40,39.90"})
    assert (await resolve(case, rows, monkeypatch)).place is None


@pytest.mark.asyncio
async def test_reviewed_street_still_rejects_distant_same_district_namesake(monkeypatch):
    case = next(c for c in CASES if c["name"] == "前门大街")
    rows = copy.deepcopy(case["pois"])
    next(p for p in rows if p["id"] == case["expected_id"])["location"] = "116.42,39.91"
    assert (await resolve(case, rows, monkeypatch)).place is None


def test_civic_technical_pair_needs_exact_name_district_and_review():
    knowledge = get_city_knowledge()
    row = next(p for c in CASES if c["name"] == "深圳市民中心" for p in c["pois"] if p["id"] == c["expected_id"])
    assert knowledge.technical_type_matches(row, city="深圳", name="市民中心")
    for changed in [{**row, "name": "深圳市民中心B区"}, {**row, "adname": "南山区"}, {**row, "typecode": "190700"}]:
        assert not knowledge.technical_type_matches(changed, city="深圳", name="市民中心")
    entity = knowledge.technical_landmark(city="深圳", name="深圳市民中心")
    assert entity
    raw = {"id": "controlled", "city": "深圳", "canonical_name": entity.canonical_name,
           "kind": "poi", "category": "attraction", "district": entity.district,
           "review_status": "lead", "sources": list(entity.sources),
           "provider_type_pairs": list(entity.provider_type_pairs)}
    with pytest.raises(ValueError, match="UNREVIEWED_PROVIDER_TYPE_PAIR"):
        _parse_record(raw, "深圳")


def test_official_short_food_street_name_keeps_a_searchable_primary_keyword():
    result = get_city_knowledge().query_lookup(city="深圳", name="盐田海鲜街")
    assert result.unique and result.unique.canonical_name == "盐田海鲜街"
    assert "盐田海鲜食街" in result.unique.aliases


@pytest.mark.asyncio
@pytest.mark.parametrize("name,replacement", [
    ("沙面", "沙面公园"),
    ("沙面", "广州市沙面·西堤旅游区"),
    ("浙江省博物馆孤山馆", "浙江省博物馆(之江馆区)"),
    ("浙江省博物馆孤山馆", "浙江省博物馆(孤山馆区)-停车场"),
    ("岳王庙", "杭州西湖风景名胜区"),
    ("岳王庙", "杭州西湖风景名胜区-岳王庙-售票处"),
    ("圆明园", "圆明园博物馆"),
    ("圆明园", "圆明园-绮春园景区"),
])
async def test_added_alias_never_substitutes_branch_parent_or_child(name, replacement, monkeypatch):
    case = next(c for c in CASES if c["name"] == name)
    row = copy.deepcopy(next(p for p in case["pois"] if p["id"] == case["expected_id"]))
    row["name"] = replacement
    row["alias"] = name
    assert (await resolve(case, [row], monkeypatch)).place is None
