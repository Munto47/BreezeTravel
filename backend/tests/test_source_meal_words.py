"""Saved real meal fields keep their source meaning through public lunch slots."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.trip_understanding.daily_dining import build_daily_meals
from app.trip_understanding.experience_inference import _source_meal_role
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_experience_source_anchors import CapturedClient, provider
from tests.test_semantic_day_sections import RecordingPlaces


SAVED = json.loads((Path(__file__).parent / 'fixtures/live_source_meal_words.json').read_text(encoding='utf-8'))['cases']


@pytest.mark.parametrize('source,quote,expected', [
    ('中午在天坛东门附近的【护国寺小吃总店】品尝豌豆黄、驴打滚等传统点心。', '护国寺小吃总店', 'LUNCH'),
    ('中午建议在恩宁路附近品尝地道粤式点心。', '恩宁路附近', 'LUNCH'),
    ('中午在广州塔附近用餐。', '广州塔附近用餐', 'LUNCH'),
    ('中午品尝云吞面等传统小吃。', '中午品尝云吞面等传统小吃', 'LUNCH'),
    ('中午在周边商圈解决午餐。', '周边商圈', 'LUNCH'),
    ('中午在【湖边餐馆】吃饭。', '湖边餐馆', 'LUNCH'),
    ('中午到【天坛公园】参观。', '天坛公园', None),
    ('中午不在【湖边餐馆】吃饭。', '湖边餐馆', None),
    ('中午没有在【湖边餐馆】用餐。', '湖边餐馆', None),
    ('中午先参观【天坛公园】，之后去饭店吃饭。', '天坛公园', None),
    ('中午参观【天坛公园】后在湖边餐馆吃饭。', '天坛公园', None),
    ('中午参观天坛公园后在【湖边餐馆】吃饭。', '湖边餐馆', 'LUNCH'),
    ('中午到【天坛公园】后去湖边餐馆吃饭。', '天坛公园', None),
    ('中午在湖边餐馆吃饭后去【天坛公园】。', '天坛公园', None),
    ('中午逛完后去【湖边餐馆】吃饭。', '湖边餐馆', 'LUNCH'),
    ('午餐不在【湖边餐馆】用餐。', '湖边餐馆', None),
    ('上午在【湖边餐馆】吃早餐。', '湖边餐馆', 'BREAKFAST'),
    ('傍晚在【湖边餐馆】享用晚餐。', '湖边餐馆', 'DINNER'),
    ('晚上在【湖边餐馆】吃饭。', '湖边餐馆', None),
    ('在【湖边餐馆】喝下午茶。', '湖边餐馆', 'SNACK'),
    ('午餐在【湖边餐馆】，晚餐去别的地方。', '湖边餐馆', 'LUNCH'),
])
def test_meal_words_require_a_local_meal_not_just_noon(source, quote, expected):
    start = source.index(quote)
    assert _source_meal_role(source, start, start + len(quote)) == expected


class MealPlaces(RecordingPlaces):
    def __init__(self, *, pending_food=False):
        super().__init__()
        self.pending_food = pending_food

    async def resolve(self, **query):
        if self.pending_food and query['category_hint'] == '餐饮':
            return None
        place = await super().resolve(**query)
        return place.model_copy(update={'category': query['category_hint'] or '地点'})


async def replay(case, *, pending_food=False):
    client = CapturedClient(*case['responses'])
    inference = provider(client)
    # The fixture is the exact source sent to one existing day task, not a
    # fresh global-structure request. No input, role or prompt is rewritten.
    inference.enable_day_sections = False
    output = await TripUnderstandingPipeline(inference, MealPlaces(pending_food=pending_food)).run(case['source'])
    assert len(client.calls) == len(case['responses'])
    return output


@pytest.mark.asyncio
@pytest.mark.parametrize('pending_food', [False, True])
async def test_saved_named_huguosi_lunch_keeps_meal_role_and_consumes_the_day_lunch(pending_food):
    output = await replay(SAVED[0], pending_food=pending_food)
    day = output.public_result.days[1]
    lunch = next(card for card in day.activities if card.name == '护国寺小吃总店')
    assert lunch.meal_role == 'LUNCH'
    assert not any(slot.meal_role == 'LUNCH' for slot in day.meal_slots)

    async def forbidden(**_):
        pytest.fail('An already retained named lunch must not trigger another restaurant search')

    meals = await build_daily_meals(SimpleNamespace(days=[day]), SimpleNamespace(stops=[]), search=forbidden)
    assert meals[0]['existing_activity_token'] == lunch.activity_token
    assert meals[0]['status'] == ('NEEDS_CONFIRMATION' if pending_food else 'EXISTING')


@pytest.mark.asyncio
@pytest.mark.parametrize('case,day_index,before,after', [
    (SAVED[1], 1, '永庆坊', '沙面岛'),
    (SAVED[2], 2, '北京路步行街', '广州塔'),
    (SAVED[3], 4, '广东省博物馆', '白云山'),
    (SAVED[4], 5, '上下九步行街', None),
], ids=['nearby-dim-sum', 'nearby-dining', 'existing-explicit-lunch', 'anonymous-noon-food'])
async def test_saved_anonymous_lunch_is_a_positioned_slot_not_an_unnamed_place(case, day_index, before, after):
    output = await replay(case)
    day = output.public_result.days[day_index - 1]
    slots = [slot for slot in day.meal_slots if slot.meal_role == 'LUNCH']
    assert len(slots) == 1
    named = {card.name: card.activity_token for card in day.activities}
    assert slots[0].after_activity_token == named[before]
    assert slots[0].before_activity_token == (named[after] if after else None)
    assert not any(card.name == '地点待确认' and card.category == '餐饮' for card in day.activities)


@pytest.mark.asyncio
async def test_optional_noon_restaurants_do_not_consume_a_lunch_or_create_two_slots():
    source = '北京一日游。\nDay1：先去故宫博物院。中午可在湖边餐馆或山边餐馆用餐。下午去景山公园。'
    rows = [dict(source_quote=name, place_name=name, role=role, day_index=1, category=category,
                 meal_role='LUNCH' if category == '餐饮' else None)
            for name, role, category in [('故宫博物院', 'PLANNED', '景点'),
                                         ('湖边餐馆', 'OPTIONAL', '餐饮'),
                                         ('山边餐馆', 'OPTIONAL', '餐饮'),
                                         ('景山公园', 'PLANNED', '景点')]]
    client = CapturedClient({'destination': '北京', 'activities': rows})
    output = await TripUnderstandingPipeline(provider(client), MealPlaces()).run(source)
    day = output.public_result.days[0]
    assert [card.name for card in day.activities] == ['故宫博物院', '景山公园']
    assert [item.name for item in day.alternatives] == ['湖边餐馆', '山边餐馆']
    assert day.meal_slots == []
    meals = [m for m in output.proposal.mentions if m.category_hint == '餐饮']
    assert all(m.meal_role == 'LUNCH' and m.role.value == 'OPTIONAL' for m in meals)
