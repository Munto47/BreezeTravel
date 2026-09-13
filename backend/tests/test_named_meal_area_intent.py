"""Dining-area purpose is separate from the identity/category of the area.

The Shanghai fixture is an unchanged real two-answer/19-POI capture; all tests
use fixed transports. New short sources are constructed, not live observations.
"""
import json
from pathlib import Path

import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.experience_inference import ExperienceQwenProvider, SemanticDraft, proposal_from_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_semantic_supplement_budget import Client


def fixture():
    return json.loads((Path(__file__).parent / 'fixtures/live_owner_shanghai_named_meal_area.json').read_text(encoding='utf-8'))


async def build_named_meal_area_result():
    saved = fixture()
    calls = []

    def reply(request):
        params = {k: v for k, v in request.url.params.multi_items() if k != 'key'}
        found = next(row for row in saved['place_calls'] if row['path'] == request.url.path and row['query'] == params)
        calls.append((request.url.path, params))
        return httpx.Response(200, json=found['response'])

    client = Client(*saved['responses'])
    provider = ExperienceQwenProvider(api_key='fixed', base_url='https://fixed.invalid', model='fixed',
        client=client, enable_source_visits=True, deadline_seconds=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
        output = await TripUnderstandingPipeline(provider, AmapPlaceResolver(api_key='fixed', client=transport)).run(saved['source'])
    assert len(client.calls) == 2 and len(calls) == 19
    return output


@pytest.mark.asyncio
async def test_actual_named_dinner_area_preserves_meal_without_becoming_a_restaurant_or_new_visit():
    output = await build_named_meal_area_result()
    saved = fixture()
    identities = [(a.compiled.mention.day_index, a.compiled.mention.atomic_place_name,
                   a.place.canonical_place_id if a.place else None)
                  for a in output.activities if a.compiled.mention.role == 'PLANNED' and a.compiled.mention.atomic_place_name]
    assert identities == [(row['day'], row['name'], row['id']) for row in saved['expected_main_identities']]
    public = output.public_result
    assert sum(len(day.activities) for day in public.days) == 17
    assert sum(card.status == 'READY' for day in public.days for card in day.activities) == 14
    assert [[slot.meal_role for slot in day.meal_slots] for day in public.days] == [['LUNCH'], ['LUNCH', 'DINNER'], []]
    dinner = public.days[1].meal_slots[-1]
    area = public.days[1].activities[-1]
    assert area.name == '进贤路' and area.category == '地点' and area.status == 'NEEDS_CONFIRMATION'
    assert area.meal_role == 'DINNER'
    assert dinner.selection_status == 'UNSELECTED' and dinner.selected_activity_token is None
    assert dinner.after_activity_token == area.activity_token and dinner.before_activity_token is None
    assert dinner.preference_text == '晚上：进贤路吃本帮菜，推荐人和馆、老正兴，尝红烧肉、响油鳝糊、油爆虾携程'
    for current, previous in zip(public.days, saved['expected_public_before']['days'], strict=True):
        assert [(x.name, x.branch_label, x.meal_role, x.choice_group_selectable) for x in current.alternatives] == [
            (x['name'], x['branch_label'], x['meal_role'], x['choice_group_selectable']) for x in previous['alternatives']]
    assert public.days[2].activities == []
    assert public.coverage.unprocessed_count == 7 and not public.coverage.complete
    assert all(not x.source_details for day in public.days for x in day.activities + day.alternatives)


async def area_plan(text, *, role='PLANNED', meal='DINNER', anonymous=False, extra_anon=False):
    source = '广州\nDay1：先到沙面。\n' + text + '\n之后去越秀公园。'
    named = dict(source_quote='湖滨路', place_name=None if anonymous else '湖滨路', role=role,
                 day_index=1, category='餐饮', meal_role=meal)
    rows = [dict(source_quote='沙面', place_name='沙面', role='PLANNED', day_index=1), named]
    if extra_anon:
        rows.append(dict(source_quote='春风馆、月湖楼', place_name=None, role=role, day_index=1, category='餐饮', meal_role=meal))
    rows.append(dict(source_quote='越秀公园', place_name='越秀公园', role='PLANNED', day_index=1))
    plan = proposal_from_draft(source, SemanticDraft.model_validate(dict(destination='广州', activities=rows)), allow_partial=True)
    return await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=plan)


@pytest.mark.asyncio
@pytest.mark.parametrize('text,meal', [
    ('中午：湖滨路吃本地菜，店铺待选。', 'LUNCH'),
    ('晚上：湖滨路吃本地菜，店铺待选。', 'DINNER'),
    ('下午茶：在湖滨路品尝点心。', 'SNACK'),
])
async def test_constructed_area_retains_one_unselected_meal_at_the_existing_area(text, meal):
    output = await area_plan(text, meal=meal)
    day = output.public_result.days[0]
    assert [card.name for card in day.activities] == ['沙面', '湖滨路', '越秀公园']
    assert day.activities[1].category == '地点'
    slot, = day.meal_slots
    assert slot.meal_role == meal and slot.selection_status == 'UNSELECTED'
    assert slot.after_activity_token == day.activities[1].activity_token
    assert slot.before_activity_token == day.activities[2].activity_token


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    '晚上：不想在湖滨路吃饭。', '晚上：取消在湖滨路吃饭。',
    '晚上：在春风馆吃饭。之后经过湖滨路。',
    '晚上：在春风馆吃饭，随后路过湖滨路。',
    '晚上：湖滨路，品牌仅供参考。',
])
async def test_named_area_does_not_gain_meal_from_denial_background_or_another_visit(text):
    output = await area_plan(text)
    assert output.public_result.days[0].meal_slots == []


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['OPTIONAL', 'REFERENCE', 'EXCLUDED'])
async def test_unselected_or_nonplanned_area_does_not_create_planned_meal(role):
    text = {'OPTIONAL': '备选：如果晚上有空，在湖滨路吃本地菜。',
            'REFERENCE': '资料说明：湖滨路有本地菜餐馆，本文仅供参考，不安排到访。',
            'EXCLUDED': '晚上：取消去湖滨路吃本地菜。'}[role]
    output = await area_plan(text, role=role)
    assert output.public_result.days[0].meal_slots == []
    assert all(card.name != '湖滨路' for card in output.public_result.days[0].activities)


@pytest.mark.asyncio
async def test_same_meal_already_has_anonymous_preference_is_not_duplicated():
    output = await area_plan('晚上：湖滨路吃本地菜，推荐春风馆、月湖楼。', extra_anon=True)
    day = output.public_result.days[0]
    assert len(day.activities) == 3
    slot, = day.meal_slots
    assert slot.meal_role == 'DINNER' and '春风馆、月湖楼' in slot.preference_text
    assert slot.after_activity_token == day.activities[1].activity_token


@pytest.mark.asyncio
async def test_repeated_area_visits_keep_two_meals_and_only_deduplicate_the_second_preference():
    source = '广州\nDay1：先到沙面。\n晚上：先在湖滨路吃面。\n晚上：后来回湖滨路吃烧烤，推荐春风馆、月湖楼。'
    rows = [dict(source_quote='沙面', place_name='沙面', role='PLANNED', day_index=1)]
    rows.extend(dict(source_quote='湖滨路', place_name='湖滨路', occurrence=occurrence, role='PLANNED',
                     day_index=1, category='餐饮', meal_role='DINNER') for occurrence in (1, 2))
    rows.append(dict(source_quote='春风馆、月湖楼', place_name=None, role='PLANNED', day_index=1,
                     category='餐饮', meal_role='DINNER'))
    plan = proposal_from_draft(source, SemanticDraft.model_validate(dict(destination='广州', activities=rows)), allow_partial=True)
    public = (await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=plan)).public_result
    day = public.days[0]
    assert [card.name for card in day.activities] == ['沙面', '湖滨路', '湖滨路']
    assert [slot.meal_role for slot in day.meal_slots] == ['DINNER', 'DINNER']
    assert [slot.after_activity_token for slot in day.meal_slots] == [day.activities[1].activity_token, day.activities[2].activity_token]


@pytest.mark.asyncio
async def test_area_lunch_does_not_masquerade_as_an_existing_restaurant_in_recommendations():
    from app.trip_understanding.daily_dining import meal_context, source_lunch_gaps
    from tests.test_meal_slot_selection import fixed_plan

    result = (await area_plan('中午：湖滨路吃本地菜，店铺待选。', meal='LUNCH')).public_result
    day = result.days[0]
    view, anchor, _ = meal_context(day, fixed_plan(result).stops)
    assert view.get('existing_activity_token') is None
    assert view['meal_role'] == 'LUNCH' and anchor.activity_token == day.activities[1].activity_token
    assert source_lunch_gaps(result, []) == {}  # The typed meal slot owns the gap.
    day.activities[1].status = 'NEEDS_CONFIRMATION'
    view, _, _ = meal_context(day, [s for s in fixed_plan(result).stops if s.activity_token != day.activities[1].activity_token])
    assert view.get('existing_activity_token') is None
    assert view.get('meal_role') == 'LUNCH'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['memory', 'postgres'])
async def test_named_area_meal_survives_storage_source_deletion_and_current_projection(kind):
    from datetime import datetime, timezone
    from app.trip_understanding.models import CreateFullRequest
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from tests.test_experience_v3_journey import repository_for

    async with repository_for(kind) as repo:
        output = await build_named_meal_area_result()
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest.model_validate({'mode': 'FULL',
            'source': {'type': 'TEXT', 'text': fixture()['source']}}), owner_user_id='experience-owner',
            idempotency_key='named-meal-area-create', now=now)
        job = await repo.claim_next(worker_id='named-meal-area-test', now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None,
            user_id='experience-owner', now=now)
        stored = await repo.get_result(resource)
        before = stored.result
        assert before.days[1].meal_slots[-1].meal_role == 'DINNER'
        await repo.delete_source(resource, user_id='experience-owner', idempotency_key='named-meal-area-source-delete',
            request_hash='b' * 64, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None,
            user_id='experience-owner', now=now)
        readback = (await repo.get_result(resource)).result
        projected = await repo.project_current_knowledge(resource, readback, now=now)
        current_plan, _ = await repo.get_current_place_plan(resource)
        dining_trip = await repo.load_recommendation_trip_view(job.understanding_id, current_plan.plan_ref.revision)
        assert [[s.model_dump() for s in day.meal_slots] for day in readback.days] == [
            [s.model_dump() for s in day.meal_slots] for day in before.days]
        assert projected.days[1].meal_slots == readback.days[1].meal_slots
        assert dining_trip.result.days[1].meal_slots == readback.days[1].meal_slots
        assert readback.days[1].activities[-1].status == 'NEEDS_CONFIRMATION'
        assert readback.days[1].activities[-1].meal_role == 'DINNER'
        assert readback.days[1].meal_slots[-1].after_activity_token == readback.days[1].activities[-1].activity_token
