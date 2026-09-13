"""Grounded ride/experience outputs fit the existing VISIT contract, not extra POIs.

These are explicit controlled outputs. They validate adapter boundaries and do
not establish that a live model will recall the experiences after the prompt edit.
"""
import pytest

from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_semantic_supplement_budget import Client


async def fixed_visit(source, parent, names, *, role='PLANNED'):
    first = {'destination': '上海', 'day_labels': [None], 'unprocessed_quotes': [], 'activities': [{
        'source_quote': parent, 'place_name': parent, 'day_index': 1, 'role': role, 'category': '景点',
    }]}
    evidence = source.split('Day1：', 1)[1]
    second = {'city_fields': [], 'source_visits': [{
        'parent_index': 0, 'kind': 'VISIT', 'source_quote': name, 'optional': False, 'evidence': evidence,
    } for name in names]}
    client = Client(first, second)
    provider = ExperienceQwenProvider(api_key='fixed', base_url='https://fixed.invalid', model='controlled',
        client=client, enable_source_visits=True, deadline_seconds=2)
    result = await TripUnderstandingPipeline(provider, FixedReplayPlaces()).run(source)
    assert len(client.calls) == 2
    assert client.calls[1]['response_format']['json_schema']['name'] == 'BreezeTravelSourceInstructions'
    return result


@pytest.mark.asyncio
async def test_optional_park_rides_and_area_stay_on_one_unselected_parent():
    names = ['飞跃地平线', '加勒比海盗', '创极速光轮', '疯狂动物城园区']
    source = '上海\nDay1：备选上海迪士尼，园内必玩：' + '、'.join(names) + '。'
    result = await fixed_visit(source, '上海迪士尼', names, role='OPTIONAL')
    day = result.public_result.days[0]
    assert day.activities == []
    parent, = day.alternatives
    assert parent.name == '上海迪士尼'
    assert [d.name for d in parent.source_details] == names
    assert result.resolution_receipt['attempted_count'] == 0
    children = [m for m in result.proposal.mentions if m.parent_mention_id]
    assert len(children) == 4 and all(m.detail_kind == 'VISIT' and m.day_index == 1 for m in children)


@pytest.mark.asyncio
@pytest.mark.parametrize('experience', ['活字拓印体验', '红烧肉制作体验', '吃豆人大冒险'])
async def test_nonpark_named_museum_experience_is_one_parent_detail_not_a_route_stop(experience):
    source = '上海\nDay1：到星河博物馆，馆内参加' + experience + '。'
    result = await fixed_visit(source, '星河博物馆', [experience])
    day = result.public_result.days[0]
    parent, = day.activities
    assert parent.name == '星河博物馆'
    assert [d.name for d in parent.source_details] == [experience]
    assert result.resolution_receipt['attempted_count'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('text,wrong_detail', [
    ('园内远眺对岸星河乐园，不去那里。', '星河乐园'),
    ('园内介绍推荐活字拓印体验，但本次不去活字拓印体验。', '活字拓印体验'),
    ('园内不去活字拓印体验。', '活字拓印体验'),
    ('园内午餐吃红烧肉。', '红烧肉'),
    ('园内午餐喝一杯酸梅汤。', '酸梅汤'),
    ('园内午餐品尝「红烧肉」。', '红烧肉'),
])
async def test_viewing_recommendation_not_visited_and_food_cannot_become_experiences(text, wrong_detail):
    source = '上海\nDay1：到星河博物馆，' + text
    result = await fixed_visit(source, '星河博物馆', [wrong_detail])
    parent, = result.public_result.days[0].activities
    assert parent.source_details == []
    assert not any(m.parent_mention_id for m in result.proposal.mentions)
    assert result.resolution_receipt['attempted_count'] == 1
    assert result.public_result.coverage.unprocessed_count > 0 and not result.public_result.coverage.complete
