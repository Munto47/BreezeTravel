"""Actual Shanghai first answer retains meals without inventing restaurant cards."""
import json
from pathlib import Path

import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.experience_inference import ExperienceQwenProvider, _source_meal_role
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_semantic_supplement_budget import Client


def fixture():
    return json.loads((Path(__file__).parent / 'fixtures/live_owner_shanghai_meal_context.json').read_text(encoding='utf-8'))


async def build_shanghai_meal_context_result():
    saved = fixture()
    calls = []

    def reply(request):
        params = {k: v for k, v in request.url.params.multi_items() if k != 'key'}
        recorded = next(c for c in saved['place_calls'] if c['path'] == request.url.path and c['query'] == params)
        calls.append(params)
        return httpx.Response(200, json=recorded['response'])

    client = Client(saved['response'])
    # The captured run had only one answer. Replay this first-answer meal
    # contract independently of subsequent optional-parent development.
    provider = ExperienceQwenProvider(api_key='fixed', base_url='https://offline.invalid', model='fixed',
        client=client, deadline_seconds=10, enable_source_visits=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
        output = await TripUnderstandingPipeline(provider, AmapPlaceResolver(api_key='fixed', client=transport)).run(saved['source'])
    assert len(client.calls) == 1 and len(calls) == 20
    return output


@pytest.mark.asyncio
async def test_saved_three_anonymous_meals_become_positioned_slots_not_unresolved_places():
    output = await build_shanghai_meal_context_result()
    public = output.public_result
    assert [[s.meal_role for s in d.meal_slots] for d in public.days] == [['LUNCH'], ['LUNCH', 'UNSPECIFIED'], []]
    assert sum(len(d.activities) for d in public.days) == 17
    assert sum(c.status == 'READY' for d in public.days for c in d.activities) == 13
    assert not any(c.category == '餐饮' for d in public.days for c in d.activities)
    actual_identities = {a.compiled.mention.atomic_place_name: a.place.canonical_place_id if a.place else None
        for a in output.activities if a.compiled.mention.role == 'PLANNED' and a.compiled.mention.atomic_place_name}
    assert [actual_identities[x['source_name']] for x in fixture()['expected_main_identities']] == [
        x['poi_id'] for x in fixture()['expected_main_identities']]
    expected = fixture()['expected_public']
    for day, original in zip(public.days, expected['days'], strict=True):
        assert [(c.name, c.status, c.city) for c in day.activities] == [
            (c['name'], c['status'], c['city']) for c in original['activities'] if c['category'] != '餐饮']
        assert [(c.name, c.branch_label, c.choice_group_selectable) for c in day.alternatives] == [
            (c['name'], c['branch_label'], c['choice_group_selectable']) for c in original['alternatives']]
    lunch1 = public.days[0].meal_slots[0]
    assert '大壶春生煎、沈大成条头糕、鲜得来排骨年糕' in lunch1.preference_text
    lunch2, dinner_unknown = public.days[1].meal_slots
    assert '乌鲁木齐中路 / 安福路吃本帮面、咖啡简餐' in lunch2.preference_text
    assert '人和馆、老正兴' in dinner_unknown.preference_text
    assert '红烧肉、响油鳝糊、油爆虾' in dinner_unknown.preference_text
    by_name = {c.name: c.activity_token for c in public.days[1].activities}
    assert lunch2.after_activity_token == by_name['五原路']
    assert lunch2.before_activity_token == by_name['上海博物馆(人民广场馆)']
    assert dinner_unknown.after_activity_token == by_name['进贤路']
    assert dinner_unknown.before_activity_token is None
    # Same-named morning sightseeing and the separate lunch-area occurrence
    # survive independently; no choice is silently made for the meal.
    anfus = [m for m in output.proposal.mentions if m.atomic_place_name == '安福路']
    assert [(m.role.value, m.day_index) for m in anfus] == [('PLANNED', 2), ('OPTIONAL', 2)]
    assert anfus[0].span_start < anfus[1].span_start
    assert [a.name for a in public.days[1].alternatives] == ['乌鲁木齐中路', '安福路']
    assert all(s.selection_status == 'UNSELECTED' and s.selected_activity_token is None for d in public.days for s in d.meal_slots)
    assert public.days[2].activities == [] and public.coverage.complete is False
    # Do not pretend this meal slice recovered the missing optional child visits.
    assert all(not a.source_details for d in public.days for a in d.activities + d.alternatives)


@pytest.mark.parametrize('source,quote,expected', [
    ('Day1\n- 中午：步行到南京路，吃老字号：春风生煎、月湖年糕。', '春风生煎、月湖年糕', 'LUNCH'),
    ('Day2\n- 中午：湖滨路 / 林荫路吃本帮面、咖啡简餐。', '湖滨路 / 林荫路', 'LUNCH'),
    ('Day2\n- **中午**：走湖滨路，吃传统面食。', '传统面食', 'LUNCH'),
    ('Day2\n- 晚餐：到湖滨路，吃传统面食。', '传统面食', 'DINNER'),
    ('Day2\n- 晚上：松柏路吃本帮菜，推荐春风馆、月湖楼，尝红烧肉。', '春风馆、月湖楼', None),
    ('Day1\n- 中午：参观青溪公园，随后到月湖楼吃饭。', '青溪公园', None),
    ('Day1\n- 中午：在月湖楼吃饭，然后游览青溪公园。', '青溪公园', None),
    ('Day1\n- 中午：在青溪公园不吃饭，随后去月湖楼。', '青溪公园', None),
    ('Day1\n- 中午：走湖滨路。\n- 下午：在月湖楼吃饭。', '月湖楼', None),
    ('Day1\n- 午餐：在春风馆吃饭。\nDay2\n- 晚上：月湖楼吃饭。', '月湖楼', None),
])
def test_bounded_meal_list_context_does_not_borrow_another_visit_or_invent_dinner(source, quote, expected):
    start = source.index(quote)
    assert _source_meal_role(source, start, start + len(quote)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['PLANNED', 'OPTIONAL', 'REFERENCE', 'EXCLUDED'])
async def test_unspecified_anonymous_meal_keeps_role_without_inventing_meal_type_or_place(role):
    from tests.semantic_page_replays import FixedReplayPlaces
    source = '广州\nDay1：先到沙面。晚上吃饭，门店待选。随后去越秀公园。'
    raw = dict(destination='广州', activities=[
        dict(source_quote='沙面', place_name='沙面', role='PLANNED', day_index=1),
        dict(source_quote='晚上吃饭', place_name=None, role=role, day_index=1, category='餐饮', meal_role='DINNER'),
        dict(source_quote='越秀公园', place_name='越秀公园', role='PLANNED', day_index=1),
    ])
    from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
    plan = proposal_from_draft(source, SemanticDraft.model_validate(raw), allow_partial=True)
    public = (await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=plan)).public_result
    assert [c.name for c in public.days[0].activities] == ['沙面', '越秀公园']
    assert [s.meal_role for s in public.days[0].meal_slots] == (['UNSPECIFIED'] if role == 'PLANNED' else [])
    if role == 'PLANNED':
        slot = public.days[0].meal_slots[0]
        assert slot.preference_text == '晚上吃饭'
        assert slot.after_activity_token == public.days[0].activities[0].activity_token
        assert slot.before_activity_token == public.days[0].activities[1].activity_token


def test_meal_context_stops_at_next_list_or_explicit_time_section_and_keeps_existing_quote():
    from app.trip_understanding.source_meal_context import source_meal_block
    source = 'Day1\n- 中午：在湖滨路吃面；下午：去月光展厅。\n- 晚餐：吃饭。'
    start = source.index('湖滨路')
    assert source_meal_block(source, start, start + 3) == '中午：在湖滨路吃面'
    start = source.index('月光展厅')
    assert source_meal_block(source, start, start + 4) is None
    source = 'Day1\n晚餐：面食、\n汤品。\n另一段私人内容。'
    quote = '晚餐：面食、\n汤品'
    start = source.index(quote)
    assert source_meal_block(source, start, start + len(quote)) == quote


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['memory', 'postgres'])
async def test_unspecified_meal_round_trip_delete_source_and_undo_preserve_generated_content(kind):
    from datetime import datetime, timezone
    from app.trip_understanding.commands import apply_public_command
    from app.trip_understanding.models import ActivityMoveCommand, CreateFullRequest, UndoCommand
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from tests.test_experience_v3_journey import repository_for
    async with repository_for(kind) as repo:
        output = await build_shanghai_meal_context_result()
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest.model_validate({'mode': 'FULL',
            'source': {'type': 'TEXT', 'text': fixture()['source']}}), owner_user_id='experience-owner',
            idempotency_key='meal-context-create', now=now)
        job = await repo.claim_next(worker_id='meal-context-test', now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None,
            user_id='experience-owner', now=now)
        stored = await repo.get_result(resource)
        expected = [[(s.meal_role, s.preference_text) for s in d.meal_slots] for d in stored.result.days]
        assert expected[1][1][0] == 'UNSPECIFIED'
        await repo.delete_source(resource, user_id='experience-owner', idempotency_key='meal-context-source-delete',
            request_hash='b' * 64, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None,
            user_id='experience-owner', now=now)
        readback = await repo.get_result(resource)
        assert [[(s.meal_role, s.preference_text) for s in d.meal_slots] for d in readback.result.days] == expected
        moved = apply_public_command(readback.result, ActivityMoveCommand(command_type='ACTIVITY_MOVE',
            activity_token=readback.result.days[0].activities[-1].activity_token, target_day_index=1, target_position=1)).result
        undone = apply_public_command(moved, UndoCommand(command_type='UNDO'), undo_result=readback.result).result
        assert [[(s.meal_role, s.preference_text) for s in d.meal_slots] for d in undone.days] == expected
