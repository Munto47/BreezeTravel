"""Meal-role projection for controlled correct plans; no claimed model improvement."""
import json

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, _source_meal_role, proposal_from_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_meal_preferences import fixture


@pytest.mark.parametrize('source,quote,expected', [
    ('护国寺小吃可尝。', '护国寺小吃', 'SNACK'),
    ('游览后建议品尝本地小吃。', '建议品尝本地小吃', 'SNACK'),
    ('下午在老街尝尝传统点心。', '传统点心', 'SNACK'),
    ('当地的小吃值得品尝。', '当地的小吃', 'SNACK'),
    ('中午品尝传统点心。', '传统点心', 'LUNCH'),
    ('晚餐吃些当地小吃。', '当地小吃', 'DINNER'),
    ('小吃店在街角。', '小吃店', None),
    ('点心店。', '点心店', None),
    ('买些点心带回家。', '点心', None),
    ('参观小吃制作展览。', '小吃制作展览', None),
    ('不品尝当地小吃。', '当地小吃', None),
    ('小吃不建议品尝。', '小吃', None),
    ('小吃不可尝。', '小吃', None),
    ('不要尝点心。', '点心', None),
    ('不吃当地小吃。', '当地小吃', None),
    ('不想排队但仍要品尝当地小吃。', '当地小吃', 'SNACK'),
    ('先参观星河展厅，之后品尝小吃。', '星河展厅', None),
    ('在点心店品尝点心后去星河展厅。', '星河展厅', None),
])
def test_snack_requires_a_local_consumption_action_not_a_shop_name(source, quote, expected):
    start = source.index(quote)
    assert _source_meal_role(source, start, start + len(quote)) == expected


@pytest.mark.asyncio
async def test_controlled_correct_owner_snack_plan_reaches_slot_without_a_chosen_store():
    saved = fixture()
    raw = json.loads(saved['responses'][0])
    # Controlled correct plan, not the actual failed first answer: only this
    # item's source quote/role/place/meal interpretation is intentionally fixed.
    snack = next(row for row in raw['activities'] if row['place_name'] == '护国寺小吃')
    snack.update(source_quote='护国寺小吃可尝', place_name=None, role='PLANNED', meal_role='SNACK')
    plan = proposal_from_draft(saved['source'], SemanticDraft.model_validate(raw), allow_partial=True)
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(saved['source'], prepared_plan=plan)
    slots = output.public_result.days[2].meal_slots
    assert len(slots) == 1
    assert slots[0].meal_role == 'SNACK'
    assert slots[0].preference_text == '护国寺小吃可尝'
    assert slots[0].selection_status == 'UNSELECTED' and slots[0].selected_activity_token is None
    assert not any(c.category == '餐饮' for d in output.public_result.days for c in d.activities)


@pytest.mark.asyncio
async def test_new_city_snack_preserves_source_position_with_no_model_time_or_store_invention():
    from tests.test_semantic_supplement_budget import Client, provider
    source = '广州\nDay1：先游沙面。建议品尝传统点心。之后游越秀公园。'
    raw = dict(destination='广州', day_labels=['Day1'], unprocessed_quotes=[], activities=[
        dict(source_quote='沙面', place_name='沙面', role='PLANNED', day_index=1),
        dict(source_quote='建议品尝传统点心', place_name=None, role='PLANNED', day_index=1,
             category='餐饮', meal_role='SNACK'),
        dict(source_quote='越秀公园', place_name='越秀公园', role='PLANNED', day_index=1),
    ])
    client = Client(raw)
    output = await TripUnderstandingPipeline(provider(client), FixedReplayPlaces()).run(source)
    assert len(client.calls) == 1
    day = output.public_result.days[0]
    assert [c.name for c in day.activities] == ['沙面', '越秀公园']
    slot, = day.meal_slots
    assert slot.meal_role == 'SNACK' and slot.preference_text == '建议品尝传统点心'
    assert slot.after_activity_token == day.activities[0].activity_token
    assert slot.before_activity_token == day.activities[1].activity_token
    assert slot.selection_status == 'UNSELECTED'
    assert all(c.start_time is None and c.end_time is None for c in day.activities)


@pytest.mark.asyncio
@pytest.mark.parametrize('role,source', [
    ('REFERENCE', '背景介绍：当地的小吃值得品尝。'),
    ('OPTIONAL', '如果有时间，可以品尝当地小吃。'),
    ('EXCLUDED', '不要品尝当地小吃。'),
])
async def test_nonplanned_snack_never_becomes_a_meal_slot(role, source):
    text = '广州\nDay1：' + source
    raw = dict(destination='广州', day_labels=['Day1'], activities=[dict(source_quote=source.rstrip('。'),
        place_name=None, role=role, day_index=1, category='餐饮', meal_role='SNACK')])
    plan = proposal_from_draft(text, SemanticDraft.model_validate(raw), allow_partial=True)
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(text, prepared_plan=plan)
    assert all(m.role == role for m in plan.mentions)
    assert output.public_result.days[0].meal_slots == []


@pytest.mark.asyncio
async def test_actual_old_owner_reference_role_remains_a_recorded_failure():
    saved = fixture()
    plan = proposal_from_draft(saved['source'], SemanticDraft.model_validate(json.loads(saved['responses'][0])), allow_partial=True)
    snack = next(m for m in plan.mentions if m.atomic_place_name == '护国寺小吃')
    assert snack.role == 'REFERENCE'
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(saved['source'], prepared_plan=plan)
    assert output.public_result.days[2].meal_slots == []
    assert output.public_result.coverage.complete is False
