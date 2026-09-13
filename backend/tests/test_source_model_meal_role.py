"""Validate model meal meaning against the same source item; all HTTP is fixed."""
import json

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_anonymous_meal_context import build_shanghai_meal_context_result, fixture


@pytest.mark.asyncio
async def test_saved_shanghai_dinner_survives_adapter_without_changing_visits_or_poi_identities():
    saved = fixture()
    wire = json.loads(saved['response'])
    row = next(row for row in wire['activities'] if row['source_quote'] == '人和馆、老正兴')
    assert row['role'] == 'PLANNED' and row['meal_role'] == 'DINNER' and row['place_name'] is None
    typed = SemanticDraft.model_validate(wire)
    assert next(item for item in typed.activities if item.source_quote == row['source_quote']).meal_role == 'DINNER'
    output = await build_shanghai_meal_context_result()
    mention = next(item for item in output.proposal.mentions if item.raw_text == row['source_quote'])
    assert (mention.role.value, mention.day_index, mention.meal_role, mention.atomic_place_name) == ('PLANNED', 2, 'DINNER', None)
    public = output.public_result
    assert [[slot.meal_role for slot in day.meal_slots] for day in public.days] == [['LUNCH'], ['LUNCH', 'DINNER'], []]
    dinner = public.days[1].meal_slots[1]
    assert dinner.preference_text == '晚上：进贤路吃本帮菜，推荐人和馆、老正兴，尝红烧肉、响油鳝糊、油爆虾携程'
    assert dinner.selection_status == 'UNSELECTED' and dinner.selected_activity_token is None
    assert dinner.after_activity_token == public.days[1].activities[-1].activity_token
    assert sum(len(day.activities) for day in public.days) == 17
    assert sum(card.status == 'READY' for day in public.days for card in day.activities) == 14
    # Current city assets separately verify the formal congress-site alias.
    identities = {activity.compiled.mention.atomic_place_name: activity.place.canonical_place_id if activity.place else None
                  for activity in output.activities if activity.compiled.mention.role == 'PLANNED' and activity.compiled.mention.atomic_place_name}
    assert [identities[row['source_name']] for row in saved['expected_main_identities']] == ['B0MGOCVPM6' if row['source_name'] == '中共一大会址' else row['poi_id'] for row in saved['expected_main_identities']]
    for day, original in zip(public.days, saved['expected_public']['days'], strict=True):
        assert [(card.name, card.status, card.city) for card in day.activities] == [
            ('中国共产党第一次全国代表大会会址', 'READY', '上海') if card['name'] == '中共一大会址'
            else (card['name'], card['status'], card['city']) for card in original['activities'] if card['category'] != '餐饮']
        assert [(item.name, item.branch_label, item.choice_group_selectable) for item in day.alternatives] == [
            (item['name'], item['branch_label'], item.get('choice_group_selectable', False)) for item in original['alternatives']]
    assert public.days[2].meal_slots == [] and public.coverage.complete is False
    assert public.coverage.unprocessed_count == 6


@pytest.mark.asyncio
@pytest.mark.parametrize('text,quote,model_role,expected', [
    ('晚上吃饭，门店待选。', '晚上吃饭', 'DINNER', 'DINNER'),
    ('晚上：松柏路吃本帮菜，推荐春风馆、月湖楼，尝红烧肉。', '春风馆、月湖楼', 'DINNER', 'DINNER'),
    ('晚上吃饭，门店待选。', '晚上吃饭', None, 'UNSPECIFIED'),
    ('晚上在月湖楼用餐。', '月湖楼', None, 'UNSPECIFIED'),
    ('下午茶：在月湖楼吃点心。', '月湖楼', 'DINNER', 'SNACK'),
    ('中午在月湖楼用餐。', '月湖楼', 'DINNER', 'LUNCH'),
    ('晚上：春风馆、月湖楼。', '春风馆、月湖楼', 'DINNER', 'UNSPECIFIED'),
    ('19:00在月湖楼用餐。', '月湖楼', 'DINNER', 'UNSPECIFIED'),
    ('晚上：在春风馆吃饭。随后路过月湖楼。', '月湖楼', 'DINNER', 'UNSPECIFIED'),
    ('晚餐：在春风馆吃饭；晚上：路过月湖楼。', '月湖楼', 'DINNER', 'UNSPECIFIED'),
    ('晚上：在春风馆吃饭，随后路过月湖楼。', '月湖楼', 'DINNER', 'UNSPECIFIED'),
    ('晚上：在春风馆吃饭！随后路过月湖楼。', '月湖楼', 'DINNER', 'UNSPECIFIED'),
    ('晚上：不吃本帮菜，推荐春风馆、月湖楼仅供参考。', '春风馆、月湖楼', 'DINNER', 'UNSPECIFIED'),
    ('晚上：不想吃饭。', '不想吃饭', 'DINNER', 'UNSPECIFIED'),
    ('晚上：不准备在月湖楼吃饭。', '月湖楼', 'DINNER', 'UNSPECIFIED'),
    ('晚上：不想排队但仍要在月湖楼吃饭。', '月湖楼', 'DINNER', 'DINNER'),
])
async def test_fixed_new_input_validates_model_dinner_with_local_use_and_keeps_other_meal_meanings(text, quote, model_role, expected):
    source = '广州\nDay1：先到沙面。\n' + text + '\n之后去越秀公园。'
    wire = dict(destination='广州', day_labels=[None], unprocessed_quotes=[], activities=[
        dict(source_quote='沙面', place_name='沙面', role='PLANNED', day_index=1),
        dict(source_quote=quote, place_name=None, role='PLANNED', day_index=1, category='餐饮', meal_role=model_role),
        dict(source_quote='越秀公园', place_name='越秀公园', role='PLANNED', day_index=1),
    ])
    plan = proposal_from_draft(source, SemanticDraft.model_validate(wire), allow_partial=True)
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=plan)
    day = output.public_result.days[0]
    assert [card.name for card in day.activities] == ['沙面', '越秀公园']
    slot, = day.meal_slots
    assert slot.meal_role == expected
    assert slot.selection_status == 'UNSELECTED' and slot.selected_activity_token is None
    assert slot.after_activity_token == day.activities[0].activity_token
    assert slot.before_activity_token == day.activities[1].activity_token
    assert all(card.start_time is None and card.end_time is None for card in day.activities)


@pytest.mark.asyncio
async def test_fixed_provider_keeps_supported_named_dinner_and_relative_position():
    from tests.test_semantic_supplement_budget import Client, provider

    source = '广州\nDay1：先到沙面。晚上在月湖楼用餐。随后去越秀公园。'
    raw = dict(destination='广州', day_labels=[None], unprocessed_quotes=[], activities=[
        dict(source_quote='沙面', place_name='沙面', role='PLANNED', day_index=1),
        dict(source_quote='月湖楼', place_name='月湖楼', role='PLANNED', day_index=1, category='餐饮', meal_role='DINNER'),
        dict(source_quote='越秀公园', place_name='越秀公园', role='PLANNED', day_index=1),
    ])
    model = Client(raw)
    output = await TripUnderstandingPipeline(provider(model), FixedReplayPlaces()).run(source)
    assert len(model.calls) == 1
    assert [(mention.atomic_place_name, mention.meal_role) for mention in output.proposal.mentions] == [
        ('沙面', None), ('月湖楼', 'DINNER'), ('越秀公园', None)]
    day, = output.public_result.days
    assert [(card.name, card.meal_role) for card in day.activities] == [('沙面', None), ('月湖楼', 'DINNER'), ('越秀公园', None)]
    assert day.meal_slots == []


@pytest.mark.asyncio
async def test_previous_day_dinner_cannot_supply_another_days_brand_mention():
    source = '广州\nDay1：晚上在春风馆吃饭。\nDay2：月湖楼。'
    raw = dict(destination='广州', day_labels=[None, None], unprocessed_quotes=[], activities=[
        dict(source_quote='春风馆', place_name=None, role='PLANNED', day_index=1, category='餐饮', meal_role='DINNER'),
        dict(source_quote='月湖楼', place_name=None, role='REFERENCE', day_index=2, category='餐饮', meal_role='DINNER'),
    ])
    plan = proposal_from_draft(source, SemanticDraft.model_validate(raw), allow_partial=True)
    assert [(item.day_index, item.role.value, item.meal_role) for item in plan.mentions] == [(1, 'PLANNED', 'DINNER'), (2, 'REFERENCE', None)]
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=plan)
    assert [[slot.meal_role for slot in day.meal_slots] for day in output.public_result.days] == [['DINNER'], []]
    assert not any(day.activities for day in output.public_result.days)


@pytest.mark.asyncio
@pytest.mark.parametrize('role,text', [
    ('OPTIONAL', '如果有时间，晚上在月湖楼吃饭。'),
    ('REFERENCE', '背景介绍：晚上在月湖楼吃饭。'),
    ('EXCLUDED', '晚上不要在月湖楼吃饭。'),
])
async def test_model_dinner_never_promotes_nonplanned_role(role, text):
    source = '广州\nDay1：' + text
    plan = proposal_from_draft(source, SemanticDraft.model_validate(dict(destination='广州', day_labels=[None], unprocessed_quotes=[], activities=[
        dict(source_quote='月湖楼', place_name=None, role=role, day_index=1, category='餐饮', meal_role='DINNER'),
    ])), allow_partial=True)
    assert [mention.role.value for mention in plan.mentions] == [role]
    result = (await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=plan)).public_result
    assert result.days[0].activities == [] and result.days[0].meal_slots == []
