"""Constructed candidates preserve the distinction between square and building."""
import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.city_scope import CityScope


@pytest.mark.asyncio
@pytest.mark.parametrize('name,district,accepted', [
    ('市民广场', '福田区', True), ('市民广场', '南山区', False),
    ('市民广场南广场', '福田区', False), ('深圳市民中心', '福田区', False),
])
async def test_civic_square_alias_requires_its_own_name_and_district(monkeypatch, name, district, accepted):
    async def scope(self, city, receipt=None):
        return CityScope('深圳市', '广东省', '440300', (('福田区', '440304'), ('南山区', '440305')), (113.7,114.7,22.3,22.9))
    monkeypatch.setattr(AmapPlaceResolver, 'city_scope', scope)
    poi = dict(id='synthetic-square',name=name,adname=district,cityname='深圳市',pname='广东省',
        adcode='440304' if district == '福田区' else '440305',typecode='110105',
        type='风景名胜;公园广场;城市广场',location='114.06,22.54',address='合成地址')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=dict(status='1',pois=[poi])))) as client:
        result = await AmapPlaceResolver(api_key='synthetic',client=client).resolve(city='深圳',atomic_place_name='深圳市民中心广场',category_hint='景点')
    assert bool(result.place) == accepted
    if accepted:
        assert result.place.canonical_place_id == 'synthetic-square'
