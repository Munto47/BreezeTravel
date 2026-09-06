"""Regression cases from the first public text journey (no live credentials)."""
import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding import candidates


def poi(name, code="110200", label="风景名胜;风景名胜;风景名胜", *, city="北京", district="朝阳区", adcode="110105", **extra):
    return dict(id=name, name=name, typecode=code, type=label, cityname=city+"市", pname=city+"市",
                adname=district, adcode=adcode, location="116.396,39.99", address=district, **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize("query,name,code,label,city,district,adcode", [
    ("鸟巢", "国家体育场", "080101", "体育休闲服务;运动场馆;综合体育馆", "北京", "朝阳区", "110105"),
    ("水立方", "国家游泳中心", "080101", "体育休闲服务;运动场馆;综合体育馆", "北京", "朝阳区", "110105"),
    ("后海", "后海", "190205", "地名地址信息;自然地名;湖泊", "北京", "西城区", "110102"),
    ("武康路", "武康路", "190301", "地名地址信息;交通地名;道路名", "上海", "徐汇区", "310104"),
    ("东方明珠", "东方明珠广播电视塔", "110202", "风景名胜;风景名胜;国家级景点", "上海", "浦东新区", "310115"),
])
async def test_common_landmark_exact_parent_in_one_request(query,name,code,label,city,district,adcode):
    calls=[]
    async def handle(req):
        calls.append(req)
        return httpx.Response(200,json={"status":"1","infocode":"10000","pois":[poi(name,code,label,city=city,district=district,adcode=adcode)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await AmapPlaceResolver(api_key="test-only",client=client).resolve(city=city,atomic_place_name=query,category_hint="景点")
    assert result.place and result.place.name==name
    assert len(calls)==1
    assert code in calls[0].url.params["types"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name",["鸟巢空中走廊","鸟巢-空中走廊","鸟巢北广场","鸟巢检票口"])
async def test_parent_alias_on_child_poi_cannot_confirm_parent(name):
    async def handle(req):
        return httpx.Response(200,json={"status":"1","pois":[poi(name,business={"alias":"鸟巢"})]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await AmapPlaceResolver(api_key="test-only",client=client).resolve(city="北京",atomic_place_name="鸟巢",category_hint="景点")
    assert result.place is None


@pytest.mark.asyncio
@pytest.mark.parametrize("override",[
    {"city":"上海","district":"黄浦区","adcode":"310101"},
    {"district":"海淀区","adcode":"110108"},
    {"code":"100100","label":"住宿服务;宾馆酒店;宾馆酒店"},
    {"code":"080101","label":"餐饮服务;中餐厅;中餐厅"},
    {"code":"080110","label":"体育休闲服务;运动场馆;游泳馆"},
])
async def test_landmark_hint_never_overrides_provider_identity(override):
    async def handle(req):
        return httpx.Response(200,json={"status":"1","pois":[poi("国家体育场",**override)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await AmapPlaceResolver(api_key="test-only",client=client).resolve(city="北京",atomic_place_name="鸟巢",category_hint="景点")
    assert result.place is None


@pytest.mark.asyncio
async def test_manual_search_keeps_main_stadium_before_related_attraction(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(candidates,"get_settings",lambda:SimpleNamespace(amap_api_key="test-only",trip_understanding_provider_mode="live"))
    async def query(self,**kw):
        return [poi("鸟巢空中走廊"),poi("国家体育场","080101","体育休闲服务;运动场馆;综合体育馆")],{}
    monkeypatch.setattr(AmapPlaceResolver,"_query_provider",query)
    rows=await candidates.search_candidates(city="北京",query="鸟巢",category_hint="景点")
    assert rows and rows[0].name=="国家体育场"
    assert all(row.category=="景点" for row in rows)

@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["住宿服务;宾馆酒店;宾馆酒店", "体育休闲服务;运动场馆;酒店", "体育休闲服务;运动场馆;综合体育馆|住宿服务;宾馆酒店;宾馆酒店"])
async def test_technical_type_cannot_mask_hotel_label(label):
    async def handle(req):
        return httpx.Response(200,json={"status":"1","pois":[poi("江湾体育场","080101",label,city="上海",district="杨浦区",adcode="310110")]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await AmapPlaceResolver(api_key="test-only",client=client).resolve(city="上海",atomic_place_name="江湾体育场",category_hint="景点")
    assert result.place is None


@pytest.mark.asyncio
async def test_canonical_stadium_cannot_be_swimming_subcategory():
    async def handle(req):
        return httpx.Response(200,json={"status":"1","pois":[poi("国家体育场","080110","体育休闲服务;运动场馆;游泳馆")]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await AmapPlaceResolver(api_key="test-only",client=client).resolve(city="北京",atomic_place_name="国家体育场",category_hint="景点")
    assert result.place is None


@pytest.mark.asyncio
async def test_two_parent_ids_stay_pending():
    first=poi("国家体育场","080101","体育休闲服务;运动场馆;综合体育馆")
    async def handle(req):
        return httpx.Response(200,json={"status":"1","pois":[first,{**first,"id":"another-id"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await AmapPlaceResolver(api_key="test-only",client=client).resolve(city="北京",atomic_place_name="鸟巢",category_hint="景点")
    assert result.place is None


@pytest.mark.asyncio
async def test_explicit_child_can_still_be_resolved():
    async def handle(req):
        return httpx.Response(200,json={"status":"1","pois":[poi("鸟巢空中走廊",business={"alias":"鸟巢"})]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await AmapPlaceResolver(api_key="test-only",client=client).resolve(city="北京",atomic_place_name="鸟巢空中走廊",category_hint="景点")
    assert result.place and result.place.name=="鸟巢空中走廊"


@pytest.mark.asyncio
async def test_manual_parent_ranked_before_six_candidate_limit(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(candidates,"get_settings",lambda:SimpleNamespace(amap_api_key="test-only",trip_understanding_provider_mode="live"))
    async def query(self,**kw):
        return [poi(f"鸟巢广场{n}") for n in range(7)]+[poi("国家体育场","080101","体育休闲服务;运动场馆;综合体育馆")],{}
    monkeypatch.setattr(AmapPlaceResolver,"_query_provider",query)
    rows=await candidates.search_candidates(city="北京",query="鸟巢",category_hint="景点")
    assert rows[0].name=="国家体育场" and len(rows)==6


@pytest.mark.asyncio
async def test_generic_manual_keyword_still_returns_choices(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(candidates,"get_settings",lambda:SimpleNamespace(amap_api_key="test-only",trip_understanding_provider_mode="live"))
    async def query(self,**kw):
        return [poi("奥运博物馆","140100","科教文化服务;博物馆;博物馆")],{}
    monkeypatch.setattr(AmapPlaceResolver,"_query_provider",query)
    rows=await candidates.search_candidates(city="北京",query="博物馆",category_hint="景点")
    assert rows and rows[0].name=="奥运博物馆"
