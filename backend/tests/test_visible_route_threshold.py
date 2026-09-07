from types import SimpleNamespace

import pytest

from app.trip_understanding.map_render import ControlledFixtureRouteProvider, MapRenderer, choose_route_mode
from app.trip_understanding.map_repository import _plan_for_result


@pytest.mark.parametrize('walking,transit,expected', [
    (29, 10, 'walking'), (30, 10, 'walking'), (31, 40, 'transit'),
    (90, 25, 'transit'), (None, 45, 'transit'), (20, None, 'walking'),
    (31, None, None), (None, None, None),
])
def test_thirty_minute_boundary(walking, transit, expected):
    def fact(minutes):
        return SimpleNamespace(status='AVAILABLE' if minutes else 'UNAVAILABLE', duration_minutes=minutes)
    assert choose_route_mode(fact(walking), fact(transit)) == expected


@pytest.mark.asyncio
async def test_hidden_mentions_do_not_break_visible_routes_or_mutate_records():
    def card(token, name, status):
        return SimpleNamespace(activity_token=token, name=name, status=status, city='北京')
    a = card('a', '合成公园', 'READY')
    hidden = card('hidden', '未识别餐饮', 'NEEDS_CONFIRMATION')
    b = card('b', '合成展馆', 'READY')
    result = SimpleNamespace(days=[SimpleNamespace(label='Day 1', activities=[a, hidden, b])])
    bindings = {c.activity_token: (f'poi-{c.activity_token}', 'AUTO_MATCHED', {
        'city': '北京', 'coordinates': {'longitude': 116.4 + i * .01, 'latitude': 39.9},
    }) for i, c in enumerate([a, b])}
    plan = _plan_for_result('test-visible-route', 1, result, bindings)
    assert [s.activity_token for s in plan.stops] == ['a', 'hidden', 'b']
    assert [s.sequence_index for s in plan.stops] == [0, 1, 2]
    provider = ControlledFixtureRouteProvider()
    provider._routes[(a.name, b.name)] = {
        'walking': {'duration_minutes': 20, 'distance_meters': 1500, 'transfer_count': 0},
        'transit': {'duration_minutes': 12, 'distance_meters': 1700, 'transfer_count': 0},
    }
    output = await MapRenderer(provider).render(plan)
    assert len(output.edges) == 1
    assert (output.edges[0].origin_name, output.edges[0].destination_name) == ('合成公园', '合成展馆')
    assert output.edges[0].selected_mode is not None
    assert result.days[0].activities == [a, hidden, b]
