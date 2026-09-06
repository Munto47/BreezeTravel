"""Reviewed identities still require exact, same-district live category evidence."""
import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver


def row(name, code="110202", label="风景名胜;风景名胜;国家级景点", **overrides):
    return {"id": "synthetic-poi", "name": name, "typecode": code, "type": label,
            "cityname": "北京市", "pname": "北京市", "adname": "东城区", "adcode": "110101",
            "location": "116.397,39.924", **overrides}


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["故宫北门", "故宫博物院北门", "神武门"])
async def test_explicit_north_gate_uses_reviewed_name_and_not_parent(query):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "1", "pois": [
            row("故宫博物院"), row("故宫博物院-神武门"),
            row("神武门(公交站)", "150700", "交通设施服务;公交车站;公交车站")
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await AmapPlaceResolver(api_key="test", client=client).resolve(
            city="北京", atomic_place_name=query, category_hint="景点")
    assert result.place and result.place.name == "故宫博物院-神武门"
    assert len(requests) == 1
    assert requests[0].url.params["keywords"] == "故宫博物院-神武门"


@pytest.mark.asyncio
@pytest.mark.parametrize("query,returned", [
    ("故宫", "故宫博物院-神武门"), ("故宫北门", "故宫博物院"),
    ("故宫北门", "故宫博物院-神武门广场"),
    ("故宫北门", "神武门(公交站)"),
])
async def test_gate_hint_never_promotes_parent_child_or_station(query, returned):
    def handle(request):
        return httpx.Response(200, json={"status": "1", "pois": [row(returned, business={"alias": query})]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await AmapPlaceResolver(api_key="test", client=client).resolve(
            city="北京", atomic_place_name=query, category_hint="景点")
    assert result.place is None


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides,accepted", [
    ({}, True), ({"adname": "西城区", "adcode": "110102"}, False),
    ({"name": "中国美术馆分馆", "business": {"alias": "中国美术馆"}}, False),
    ({"typecode": "140200", "type": "科教文化服务;展览馆;展览馆"}, False),
    ({"typecode": "050000", "type": "餐饮服务;餐饮相关场所;餐饮相关"}, False),
    ({"type": "科教文化服务;博物馆;博物馆|住宿服务;宾馆酒店;宾馆酒店"}, False),
])
async def test_art_museum_exception_is_exact_and_narrow(overrides, accepted):
    item = {**row("中国美术馆", "140100", "科教文化服务;博物馆;博物馆"), **overrides}
    def handle(request):
        return httpx.Response(200, json={"status": "1", "pois": [item]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await AmapPlaceResolver(api_key="test", client=client).resolve(
            city="北京", atomic_place_name="中国美术馆", category_hint="景点")
    assert (result.place is not None) is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("name,accepted", [("圆明园遗址公园", True), ("圆明园遗址公园东区", False)])
async def test_whole_scenic_area_suffix_is_not_a_branch(name, accepted):
    def handle(request):
        return httpx.Response(200, json={"status": "1", "pois": [row(name,
            adname="海淀区", adcode="110108", business={"alias": "圆明园"})]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await AmapPlaceResolver(api_key="test", client=client).resolve(
            city="北京", atomic_place_name="圆明园", category_hint="景点")
    assert (result.place is not None) is accepted


@pytest.mark.asyncio
async def test_museum_affiliation_alias_does_not_override_its_actual_museum_category():
    item = row("上海自然博物馆", "140100", "科教文化服务;博物馆;博物馆", cityname="上海市", pname="上海市",
        adname="静安区", adcode="310106", location="121.463,31.237", business={"alias": "上海科技馆分馆"})
    def handle(request):
        return httpx.Response(200, json={"status": "1", "pois": [item]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await AmapPlaceResolver(api_key="test", client=client).resolve(
            city="上海", atomic_place_name="上海自然博物馆", category_hint="景点")
    assert result.place and result.place.name == "上海自然博物馆"
