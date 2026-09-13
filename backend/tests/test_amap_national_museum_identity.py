"""Official shorthand plus recorded AMap identities; all HTTP is fixed replay."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.city_knowledge import load_city_knowledge


SNAPSHOT = json.loads((Path(__file__).parent / "fixtures/amap_national_museum.json").read_text(encoding="utf-8"))
PARENT_ID = "B000A83U0P"
CANONICAL_NAME = "中国国家博物馆"


def _client(rows, observed):
    async def respond(request):
        observed.append(request)
        return httpx.Response(200, json={"status": "1", "infocode": "10000", "pois": rows})
    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


def test_official_shorthand_has_one_district_qualified_identity():
    knowledge = load_city_knowledge()
    assert knowledge.issues == ()
    entry = knowledge.query_lookup(city="北京", name="国家博物馆").unique
    assert entry and entry.canonical_name == CANONICAL_NAME
    assert entry.district == "东城区"
    assert any(source["url"] == "https://www.chnmuseum.cn/cg/" for source in entry.sources)
    assert knowledge.query_lookup(city="上海", name="国家博物馆").unique is None
    for name in ("国家自然博物馆", "国家典籍博物馆", "国家动物博物馆",
                 "中国国家博物馆文物科技保护中心", "国家博物馆列宾展(打卡点)"):
        assert all(match.canonical_name != CANONICAL_NAME
                   for match in knowledge.query_lookup(city="北京", name=name).matches)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["国家博物馆", "中国国家博物馆", "国博"])
async def test_recorded_museum_shorthand_confirms_only_the_main_museum(query):
    observed = []
    async with _client(deepcopy(SNAPSHOT["pois"]), observed) as client:
        result = await AmapPlaceResolver(api_key="fixed-response-only", client=client).resolve(
            city="北京", atomic_place_name=query, category_hint="景点",
        )
    assert result.place and result.place.canonical_place_id == PARENT_ID
    assert result.place.name == CANONICAL_NAME
    assert result.place.area_or_address == "东长安街16号天安门广场东侧"
    assert len(observed) == 1
    assert observed[0].url.params["keywords"] == CANONICAL_NAME
    assert observed[0].url.params["region"] == "北京"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "missing", "other_museum_alias", "internal_alias", "conservation_alias", "wrong_city", "wrong_district",
    "ambiguous", "category_conflict", "parent_for_internal",
])
async def test_museum_shorthand_does_not_relax_identity_boundaries(mutation):
    rows = deepcopy(SNAPSHOT["pois"])
    parent = next(row for row in rows if row["id"] == PARENT_ID)
    query = "国家博物馆"
    if mutation in {"missing", "other_museum_alias", "internal_alias", "conservation_alias"}:
        rows = [row for row in rows if row["id"] != PARENT_ID]
        if mutation != "missing":
            target_id = {"other_museum_alias": "B000A7CRPA", "internal_alias": "B0MRPZIITR",
                         "conservation_alias": "B000A9S3QY"}[mutation]
            target = next(row for row in rows if row["id"] == target_id)
            target["business"] = {"alias": query}
    elif mutation == "wrong_city":
        parent.update(pname="上海市", cityname="上海市", adname="黄浦区", adcode="310101", location="121.48,31.23")
    elif mutation == "wrong_district":
        parent.update(adname="丰台区", adcode="110106")
    elif mutation == "ambiguous":
        rows.append({**parent, "id": "controlled-distinct-museum"})
    elif mutation == "category_conflict":
        parent.update(typecode="140500", type="科教文化服务;图书馆;图书馆")
    elif mutation == "parent_for_internal":
        rows = [parent]
        query = "中国国家博物馆文物科技保护中心"
        parent["business"] = {"alias": query}
    async with _client(rows, []) as client:
        result = await AmapPlaceResolver(api_key="fixed-response-only", client=client).resolve(
            city="北京", atomic_place_name=query, category_hint="景点",
        )
    assert result.place is None
