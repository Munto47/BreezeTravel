"""Saved six supplier rows plus independent controls; no provider calls."""
import json
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trip_understandings_v3 as api
from app.trip_understanding.candidates import CandidatePlace, issue_candidate, search_candidates
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.daily_dining import build_daily_meals, meal_context, project_daily_meals
from app.trip_understanding.demo import FixedBeijingPlaceResolver
from app.trip_understanding.dining import (
    DiningCandidateView, bind_dining_access, dining_binding, dining_metadata, select_dining_rows,
    source_meal_binding, source_meal_context, verify_command_candidate,
)
from app.trip_understanding.errors import CommandTargetChangedError, ResourceAccessDeniedError, RevisionConflictError
from app.trip_understanding.map_render import MapStop
from app.trip_understanding.models import (
    ActivityDeleteCommand, ActivityMoveCommand, ActivityTextEditCommand, CreateFullRequest,
    DiningInsertCommand, PlaceConfirmCommand, PlaceReplaceCommand, RedoCommand,
    SourceMealRef, UndoCommand, UserFacingTripResult,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from tests.test_dining_recommendations import anchor, row, restaurant
from tests.test_experience_v3_journey import repository_for
from tests.test_semantic_supplement_budget import Client, provider
from tests.test_source_meal_selection import SOURCE

SAVED = json.loads(Path(__file__).with_name('fixtures').joinpath('dining_access_saved_six.json').read_text(encoding='utf8'))
RESOURCE = 'fixed-dining-access-resource'
ETAG = 'fixed-dining-access-version'


class SavedParentResolver(FixedBeijingPlaceResolver):
    _PLACES = dict(FixedBeijingPlaceResolver._PLACES)
    for item in SAVED['anchors']:
        original = _PLACES[item['name']]
        _PLACES[item['name']] = (item['canonical_place_id'], *original[1:3],
            item['coordinates']['longitude'], item['coordinates']['latitude'])


async def build_dining_access_result():
    specs = [(1, '故宫博物院'), (1, '午餐：清淡面条'), (1, '景山公园'), (1, '晚餐：本帮菜，想吃红烧肉'),
             (2, '天坛公园'), (2, '晚餐：清淡面条'), (2, '前门大街')]
    raw = {'destination': '北京', 'day_labels': ['Day1', 'Day2'], 'unprocessed_quotes': [], 'activities': []}
    for day, name in specs:
        meal = '餐：' in name
        raw['activities'].append(dict(source_quote=name, place_name=None if meal else name, role='PLANNED',
            day_index=day, category='餐饮' if meal else '景点',
            meal_role=('LUNCH' if name.startswith('午餐') else 'DINNER') if meal else None))
    return await TripUnderstandingPipeline(provider(Client(raw)), SavedParentResolver()).run(SOURCE)


def plan_for(result):
    stops = []
    raw_places = {item['name']: item for item in SAVED['rows']}
    for day_index, day in enumerate(result.days, 1):
        for sequence, card in enumerate(day.activities):
            if card.name in SavedParentResolver._PLACES:
                identity, _, _, longitude, latitude = SavedParentResolver._PLACES[card.name]
            else:
                item = raw_places[card.name]
                identity = 'amap:' + item['id']
                longitude, latitude = map(float, item['location'].split(','))
            stops.append(MapStop(activity_token=card.activity_token, day_index=day_index, day_label=day.label,
                sequence_index=sequence, name=card.name, category=card.category, canonical_place_id=identity,
                resolution_status='AUTO_MATCHED' if card.status == 'READY' else 'NEEDS_CONFIRMATION',
                city=card.city, longitude=longitude, latitude=latitude))
    return SimpleNamespace(stops=stops)


def saved_candidates(plan, day_index=1, *, token=None, before=False):
    stop = next(item for item in plan.stops if item.activity_token == token) if token else next(item for item in plan.stops if item.day_index == day_index)
    rows = SAVED['rows'][:3] if day_index == 1 else SAVED['rows'][3:]
    return [bind_dining_access(place, stops=plan.stops, activity_token=stop.activity_token, before=before)
        for place in select_dining_rows(rows, anchor=stop, excluded_ids=set(), meal_only=True)]


def source_selection(result, plan, *, resource=RESOURCE, etag=ETAG, now=None, name='冰窖餐厅'):
    now = now or datetime.now(timezone.utc)
    ref = SourceMealRef(day_index=1, slot_index=0)
    context = source_meal_context(result, plan, ref)
    places = saved_candidates(plan, token=context.after_activity_token, before=context.insert_before)
    issued = [issue_candidate(place, public_resource_id=resource, expected_etag=etag, now=now,
        activity_token=source_meal_binding(context.after_activity_token, before=context.insert_before,
            meal_slot=ref, meal_role=context.meal_role)) for place in places]
    context.candidates = [DiningCandidateView(**item.model_dump(), reason='固定保存的供应商资料，非新搜索。') for item in issued]
    chosen = next(item for item in issued if item.name == name)
    command = DiningInsertCommand(command_type='DINING_INSERT', after_activity_token=context.after_activity_token,
        insert_before=context.insert_before, meal_role=context.meal_role, meal_slot=ref, candidate_token=chosen.candidate_token)
    return context, command


async def build_dining_access_states():
    """Browser fixture: real Provider/Pipeline and candidate/command logic, fixed external facts."""
    before = (await build_dining_access_result()).public_result
    now = datetime.now(timezone.utc)
    plan = plan_for(before)
    candidates, command = source_selection(before, plan, now=now)
    place = verify_command_candidate(command, public_resource_id=RESOURCE, expected_etag=ETAG, now=now)
    adopted = apply_public_command(before, command, confirmed_place=place, dining_plan=plan).result
    card = next(card for card in adopted.days[0].activities if card.name == place.name)
    move = ActivityMoveCommand(command_type='ACTIVITY_MOVE', activity_token=card.activity_token,
        target_day_index=1, target_position=2)
    moved = apply_public_command(adopted, move).result
    restored = apply_public_command(moved, UndoCommand(command_type='UNDO'), undo_result=adopted).result
    redone = apply_public_command(restored, RedoCommand(command_type='REDO'), redo_result=moved).result
    return {'before': before, 'candidate_response': candidates, 'select_command': command,
        'adopted': adopted, 'move_command': move, 'moved': moved, 'undo': restored, 'redo': redone}


@pytest.mark.asyncio
async def test_six_original_rows_keep_five_parent_conditions_and_one_light_only_menu():
    before = (await build_dining_access_result()).public_result
    plan = plan_for(before)
    day1, day2 = saved_candidates(plan), saved_candidates(plan, 2)
    assert len(day1) == len(day2) == 3
    parents = [p for p in day1 + day2 if p.provider_parent_place_id]
    assert len(parents) == 5
    assert all(p.dining_access.status == 'DURING_VISIT' for p in parents)
    assert all(p.dining_access.parent_name == '故宫博物院' for p in day1)
    by_name = {p.name: p for p in day1 + day2}
    assert by_name['坤宁宫东院餐厅'].meal_evidence_status == 'LIGHT_FOOD_ITEMS_ONLY'
    assert by_name['天坛福宴'].dining_info.cuisine == '茶点'
    assert by_name['天坛福宴'].meal_evidence_status == 'UNSPECIFIED'
    assert by_name['锦馨豆汁(天坛路店)'].dining_access is None
    public = issue_candidate(day1[0], public_resource_id=RESOURCE, activity_token='x' * 24,
        expected_etag=ETAG, now=datetime.now(timezone.utc)).model_dump_json()
    assert 'provider_parent_place_id' not in public and 'dining_parent_activity_token' not in public
    assert day1[0].provider_parent_place_id not in public


@pytest.mark.parametrize('tags,status', [
    ('下午茶,点心', 'LIGHT_FOOD_ITEMS_ONLY'), ('咖啡,蛋糕', 'LIGHT_FOOD_ITEMS_ONLY'),
    ('下午茶,干炸丸子', 'UNSPECIFIED'), ('', 'UNSPECIFIED'), ('茶香排骨', 'UNSPECIFIED'),
])
def test_independent_supplier_tags_do_not_classify_from_names_or_broad_cuisine(tags, status):
    item = {**row(), 'name': '合成茶点餐厅', 'business': {'keytag': '茶点', 'tag': tags}}
    selected = select_dining_rows([item], anchor=anchor(), excluded_ids=set(), meal_only=True)[0]
    assert selected.meal_evidence_status == status
    assert selected.dining_access is None


@pytest.mark.parametrize('category,name', [('住宿', '合成酒店'), ('购物', '合成商场')])
def test_independent_public_venue_parent_is_not_rejected_as_a_paid_scenic_area(category, name):
    stop = anchor().model_copy(update={'name': name, 'category': category, 'canonical_place_id': 'BTESTPARENT'})
    selected = select_dining_rows([{**row(), 'parent': 'BTESTPARENT'}], anchor=stop, excluded_ids=set())[0]
    assert selected.dining_access.model_dump() == {'status': 'DURING_VISIT', 'parent_name': name}


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['before-parent', 'after-next-stop', 'wrong-day', 'other-id', 'pending-parent', 'revisit-token'])
async def test_parent_location_and_same_occurrence_must_be_proved(bad):
    result = (await build_dining_access_result()).public_result
    plan = plan_for(result)
    place = saved_candidates(plan)[0]
    original_parent = plan.stops[0]
    token, before = original_parent.activity_token, False
    if bad == 'before-parent':
        before = True
    elif bad == 'after-next-stop':
        token = plan.stops[1].activity_token
    elif bad == 'wrong-day':
        token = plan.stops[2].activity_token
    elif bad == 'other-id':
        original_parent.canonical_place_id = 'BOTHERPARENT'
    elif bad == 'pending-parent':
        original_parent.resolution_status = 'NEEDS_CONFIRMATION'
        result.days[0].activities[0].status = 'NEEDS_CONFIRMATION'
    else:
        revisit = result.days[0].activities[0].model_copy(update={'activity_token': 'revisit-parent-' + 'x' * 20})
        result.days[0].activities.append(revisit)
        plan.stops.append(original_parent.model_copy(update={'activity_token': revisit.activity_token, 'sequence_index': 2}))
        token = revisit.activity_token
    command = DiningInsertCommand(command_type='DINING_INSERT', after_activity_token=token,
        insert_before=before, meal_role='LUNCH', candidate_token='x' * 40)
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(result, command, confirmed_place=place, dining_plan=plan)


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['BREAKFAST', 'LUNCH', 'DINNER'])
async def test_old_signed_light_food_candidate_cannot_consume_a_full_meal(role):
    before = (await build_dining_access_result()).public_result
    plan = plan_for(before)
    light = next(p for p in saved_candidates(plan) if p.meal_evidence_status == 'LIGHT_FOOD_ITEMS_ONLY')
    # Simulate an older saved object with menu facts but no new purpose field.
    light = CandidatePlace.model_validate(light.model_dump(exclude={'meal_evidence_status'}))
    now = datetime.now(timezone.utc)
    issued = issue_candidate(light, public_resource_id=RESOURCE, activity_token=dining_binding(plan.stops[0].activity_token),
        expected_etag=ETAG, now=now)
    command = DiningInsertCommand(command_type='DINING_INSERT', after_activity_token=plan.stops[0].activity_token,
        meal_role=role, candidate_token=issued.candidate_token)
    with pytest.raises(CommandTargetChangedError, match='full meal'):
        verify_command_candidate(command, public_resource_id=RESOURCE, expected_etag=ETAG, now=now)
    with pytest.raises(CommandTargetChangedError, match='full meal'):
        apply_public_command(before, command, confirmed_place=light, dining_plan=plan)


@pytest.mark.asyncio
@pytest.mark.parametrize('role,source_slot', [('SNACK', False), (None, False), (None, True)])
async def test_light_food_can_remain_explicit_snack_or_unspecified_without_becoming_lunch(role, source_slot):
    before = (await build_dining_access_result()).public_result
    plan = plan_for(before)
    light = next(p for p in saved_candidates(plan) if p.meal_evidence_status == 'LIGHT_FOOD_ITEMS_ONLY')
    if source_slot:
        before.days[0].meal_slots[0].meal_role = 'UNSPECIFIED'
    command = DiningInsertCommand(command_type='DINING_INSERT', after_activity_token=plan.stops[0].activity_token,
        meal_role=role, candidate_token='x' * 40,
        meal_slot=SourceMealRef(day_index=1, slot_index=0) if source_slot else None)
    selected = apply_public_command(before, command, confirmed_place=light, dining_plan=plan).result
    card = selected.days[0].activities[1]
    assert card.meal_evidence_status == 'LIGHT_FOOD_ITEMS_ONLY' and card.meal_role == role
    assert meal_context(selected.days[0], plan_for(selected).stops)[0]['status'] != 'EXISTING'
    if source_slot:
        assert selected.days[0].meal_slots[0].selection_status == 'SELECTED'


@pytest.mark.asyncio
async def test_legacy_missing_fields_and_real_meal_none_keep_previous_behavior():
    before = (await build_dining_access_result()).public_result
    before.days[0].meal_slots = []
    plan = plan_for(before)
    legacy = CandidatePlace.model_validate(restaurant().model_dump(exclude={'dining_access', 'meal_evidence_status',
        'provider_parent_place_id', 'dining_parent_activity_token'}))
    command = DiningInsertCommand(command_type='DINING_INSERT', after_activity_token=plan.stops[0].activity_token,
        candidate_token='x' * 40)
    selected = apply_public_command(before, command, confirmed_place=legacy, dining_plan=plan).result
    card = selected.days[0].activities[1]
    assert card.meal_role is None and card.dining_access is None and card.meal_evidence_status == 'UNSPECIFIED'
    assert meal_context(selected.days[0], [])[0]['status'] == 'EXISTING'
    old_json = selected.model_dump(mode='json')
    for day in old_json['days']:
        for item in day['activities']:
            item.pop('dining_access')
            item.pop('meal_evidence_status')
    assert UserFacingTripResult.model_validate(old_json).days[0].activities[1].dining_access is None


@pytest.mark.asyncio
async def test_daily_projection_preserves_conditions_and_does_not_recommend_light_only():
    result = (await build_dining_access_result()).public_result
    plan = plan_for(result)
    async def search(**kwargs):
        return saved_candidates(plan, kwargs['anchor'].day_index)
    rows = await build_daily_meals(result, plan, search=search, routes=None, area_search=None)
    projected = project_daily_meals(rows, public_resource_id=RESOURCE, etag=ETAG)
    assert len(projected[0].candidates) == 3
    assert all(c.dining_access.status == 'DURING_VISIT' for c in projected[0].candidates)
    light = next(c for c in projected[0].candidates if c.name == '坤宁宫东院餐厅')
    assert light.meal_evidence_status == 'LIGHT_FOOD_ITEMS_ONLY' and not light.recommended


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['move-meal', 'move-parent', 'cross-day', 'delete-parent', 'replace-parent', 'rename-meal', 'same-name-other-id'])
async def test_edits_keep_restaurant_but_never_rebind_conditions_to_another_visit(change):
    states = await build_dining_access_states()
    selected = states['adopted']
    parent, meal = selected.days[0].activities[:2]
    extra = {}
    if change == 'move-meal':
        command = ActivityMoveCommand(command_type='ACTIVITY_MOVE', activity_token=meal.activity_token, target_day_index=1, target_position=2)
    elif change == 'move-parent':
        command = ActivityMoveCommand(command_type='ACTIVITY_MOVE', activity_token=parent.activity_token, target_day_index=1, target_position=2)
    elif change == 'cross-day':
        command = ActivityMoveCommand(command_type='ACTIVITY_MOVE', activity_token=meal.activity_token, target_day_index=2, target_position=0)
    elif change == 'delete-parent':
        command = ActivityDeleteCommand(command_type='ACTIVITY_DELETE', activity_token=parent.activity_token)
    elif change == 'replace-parent':
        command = PlaceReplaceCommand(command_type='PLACE_REPLACE', activity_token=parent.activity_token,
            replacement={'name': parent.name, 'category': '景点', 'area_or_address': '另一处同名地点'})
    elif change == 'rename-meal':
        command = ActivityTextEditCommand(command_type='ACTIVITY_TEXT_EDIT', activity_token=meal.activity_token, name='合成另一餐厅')
    else:
        command = PlaceConfirmCommand(command_type='PLACE_CONFIRM', activity_token=parent.activity_token, candidate_token='x' * 40)
        extra = {'current_place_id': 'B000A8UIN8', 'confirmed_place': restaurant().model_copy(update={
            'name': parent.name, 'category': '景点', 'canonical_place_id': 'amap:BOTHERPARENT'})}
    updated = apply_public_command(selected, command, **extra).result
    kept = next(c for day in updated.days for c in day.activities if c.dining_access)
    assert kept.dining_access.status == 'NEEDS_REVIEW'
    assert kept.dining_access.parent_name == parent.name


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['memory', 'postgres'])
async def test_access_saved_refreshed_undo_redo_old_key_and_authorization(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest(mode='FULL', source={'type': 'TEXT', 'text': SOURCE}),
            owner_user_id='experience-owner', idempotency_key='access-create', now=now)
        job = await repo.claim_next(worker_id='fixed-access', now=now, lease_seconds=60)
        await repo.complete_job(job, await build_dining_access_result(), now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None, user_id='experience-owner', now=now)
        async def read():
            refreshed = await repo.authorize(created.accepted.public_resource_id, capability_hash=None, user_id='experience-owner', now=now)
            return refreshed, await repo.get_result(refreshed)
        with pytest.raises(ResourceAccessDeniedError):
            await repo.authorize(resource.public_resource_id, capability_hash=None, user_id='stranger', now=now)
        before = await repo.get_result(resource)
        plan, etag = await repo.get_current_place_plan(resource)
        _, command = source_selection(before.result, plan, resource=resource.public_resource_id, etag=etag, now=now)
        outcome = await service.apply_command(resource, command, expected_etag=etag, idempotency_key='access-adopt', now=now)
        assert (await service.apply_command(resource, command, expected_etag=etag, idempotency_key='access-adopt', now=now + timedelta(minutes=11))).replayed
        with pytest.raises(RevisionConflictError):
            await service.apply_command(resource, command, expected_etag=etag, idempotency_key='access-stale', now=now)
        resource, current = await read()
        assert current.opaque_etag == outcome.opaque_etag
        assert current.result.days[0].activities[1].dining_access.status == 'DURING_VISIT'
        assert current.result.days[0].meal_slots[0].selected_activity_token == current.result.days[0].activities[1].activity_token
        assert current.result.days[0].meal_slots[1].selection_status == 'UNSELECTED'
        assert [c.name for c in current.result.days[1].activities] == [c.name for c in before.result.days[1].activities]
        move = ActivityMoveCommand(command_type='ACTIVITY_MOVE', activity_token=current.result.days[0].activities[1].activity_token,
            target_day_index=1, target_position=2)
        await service.apply_command(resource, move, expected_etag=current.opaque_etag, idempotency_key='access-move', now=now)
        resource, current = await read()
        assert current.result.days[0].activities[-1].dining_access.status == 'NEEDS_REVIEW'
        for index, action in enumerate(['UNDO', 'REDO', 'UNDO', 'UNDO', 'REDO']):
            command_cls = UndoCommand if action == 'UNDO' else RedoCommand
            await service.apply_command(resource, command_cls(command_type=action), expected_etag=current.opaque_etag,
                idempotency_key=f'access-history-{index}', now=now)
            resource, current = await read()
            cards = [c for day in current.result.days for c in day.activities if c.dining_access]
            if index == 3:
                assert cards == [] and current.result.days[0].meal_slots[0].selection_status == 'UNSELECTED'
            else:
                assert len(cards) == 1
                assert cards[0].dining_access.status == ('NEEDS_REVIEW' if index == 1 else 'DURING_VISIT')
        public = current.result.model_dump_json()
        assert 'provider_parent_place_id' not in public and 'dining_parent_activity_token' not in public
        assert UserFacingTripResult.model_validate_json(public).days[0].activities[1].dining_access.parent_name == '故宫博物院'


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['BREAKFAST', 'LUNCH', 'DINNER', None, 'SNACK'])
async def test_place_confirm_cannot_bypass_the_known_meal_purpose(role):
    selected = (await build_dining_access_states())['adopted']
    target = selected.days[0].activities[1]
    target.meal_role = role
    light = dining_metadata(restaurant().model_copy(update={'name': '独立咖啡店'}),
        {**row(), 'business': {'tag': '咖啡,点心'}})
    command = PlaceConfirmCommand(command_type='PLACE_CONFIRM', activity_token=target.activity_token, candidate_token='x' * 40)
    if role in {'BREAKFAST', 'LUNCH', 'DINNER'}:
        with pytest.raises(CommandTargetChangedError, match='full meal'):
            apply_public_command(selected, command, confirmed_place=light, current_place_id='old-place')
    else:
        updated = apply_public_command(selected, command, confirmed_place=light, current_place_id='old-place').result
        card = updated.days[0].activities[1]
        assert card.meal_evidence_status == 'LIGHT_FOOD_ITEMS_ONLY' and card.meal_role == role
        assert meal_context(updated.days[0], [])[0]['status'] != 'EXISTING'


@pytest.mark.asyncio
async def test_place_confirm_parent_guard_and_same_identity_reconfirmation():
    selected = (await build_dining_access_states())['adopted']
    plan = plan_for(selected)
    target = selected.days[0].activities[1]
    place = dining_metadata(restaurant(), {**row(), 'parent': 'BOTHERPARENT'})
    command = PlaceConfirmCommand(command_type='PLACE_CONFIRM', activity_token=target.activity_token, candidate_token='x' * 40)
    with pytest.raises(CommandTargetChangedError, match='same parent'):
        apply_public_command(selected, command, confirmed_place=place, dining_plan=plan)
    parent = selected.days[0].activities[0]
    same_parent = restaurant().model_copy(update={'name': parent.name, 'category': parent.category,
        'canonical_place_id': 'amap:' + SAVED['anchors'][0]['canonical_place_id']})
    updated = apply_public_command(selected, PlaceConfirmCommand(command_type='PLACE_CONFIRM',
        activity_token=parent.activity_token, candidate_token='x' * 40), confirmed_place=same_parent,
        current_place_id=SAVED['anchors'][0]['canonical_place_id']).result
    assert updated.days[0].activities[1].dining_access.status == 'DURING_VISIT'


@pytest.mark.asyncio
async def test_actual_place_search_keeps_supplier_parent_and_menu_metadata(monkeypatch):
    import app.trip_understanding.candidates as module
    monkeypatch.setattr(module, 'get_settings', lambda: SimpleNamespace(amap_api_key='fixed-not-a-secret', trip_understanding_provider_mode='live'))
    raw = next(item for item in SAVED['rows'] if item['name'] == '坤宁宫东院餐厅')
    calls = []
    async def query(self, **kwargs):
        calls.append(kwargs)
        return [raw], {}
    monkeypatch.setattr(module.AmapPlaceResolver, '_query_provider', query)
    selected = await search_candidates(city='北京', query=raw['name'], category_hint='餐饮')
    assert len(calls) == len(selected) == 1
    assert selected[0].meal_evidence_status == 'LIGHT_FOOD_ITEMS_ONLY'
    assert selected[0].provider_parent_place_id == 'amap:' + raw['parent']
    assert selected[0].dining_access.status == 'NEEDS_REVIEW'


def test_actual_api_place_search_binds_current_restaurant_position_before_confirmation():
    repo = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    app.dependency_overrides[api.get_trip_understanding_repository] = lambda: repo
    seen = []
    async def search(**kwargs):
        seen.append(kwargs)
        raw = next(item for item in SAVED['rows'] if item['name'] == '景运门故宫餐厅')
        return [dining_metadata(restaurant().model_copy(update={'name': raw['name'], 'canonical_place_id': 'amap:' + raw['id']}), raw)]
    app.dependency_overrides[api.get_place_candidate_search] = lambda: search
    with TestClient(app) as client:
        created = client.post('/api/v3/trip-understandings', json={'mode': 'FULL', 'source': {'type': 'TEXT', 'text': SOURCE}},
            headers={'Idempotency-Key': 'access-api'})
        now = datetime.now(timezone.utc)
        job = asyncio.run(repo.claim_next(worker_id='fixed-access-api', now=now, lease_seconds=60))
        output = asyncio.run(build_dining_access_result())
        # Complete a fixed Provider/Pipeline result, then use real commands.
        asyncio.run(repo.complete_job(job, output, now=now))
        route = '/api/v3/trip-understandings/' + created.json()['public_resource_id']
        response = client.get(route + '/result')
        before = UserFacingTripResult.model_validate(response.json())
        _, command = source_selection(before, plan_for(before), resource=created.json()['public_resource_id'], etag=response.headers['etag'].strip('"'), now=now)
        adopted = client.post(route + '/commands', json=command.model_dump(mode='json'),
            headers={'If-Match': response.headers['etag'], 'Idempotency-Key': 'access-api-adopt'})
        assert adopted.status_code == 200, adopted.text
        response = client.get(route + '/result')
        target = response.json()['days'][0]['activities'][1]
        candidates = client.post(route + '/place-candidates', json={'activity_token': target['activity_token'], 'query': '景运门故宫餐厅'})
        assert candidates.status_code == 200, candidates.text
        candidate = candidates.json()['candidates'][0]
        assert candidate['dining_access'] == {'status': 'DURING_VISIT', 'parent_name': '故宫博物院'}
        assert 'provider_parent_place_id' not in candidate and 'dining_parent_activity_token' not in candidate
        confirmed = client.post(route + '/commands', json={'command_type': 'PLACE_CONFIRM', 'activity_token': target['activity_token'],
            'candidate_token': candidate['candidate_token']}, headers={'If-Match': response.headers['etag'], 'Idempotency-Key': 'access-api-confirm'})
        assert confirmed.status_code == 200, confirmed.text
        final = client.get(route + '/result').json()['days'][0]['activities'][1]
        assert final['name'] == candidate['name'] and final['dining_access']['status'] == 'DURING_VISIT'
        assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['memory', 'postgres'])
async def test_signed_candidate_rejects_expiry_other_resource_parent_mismatch_and_light_meal(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest(mode='FULL', source={'type': 'TEXT', 'text': SOURCE}),
            owner_user_id='experience-owner', idempotency_key='access-negative-create', now=now)
        job = await repo.claim_next(worker_id='fixed-access-negative', now=now, lease_seconds=60)
        await repo.complete_job(job, await build_dining_access_result(), now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None, user_id='experience-owner', now=now)
        stored = await repo.get_result(resource)
        plan, etag = await repo.get_current_place_plan(resource)
        for case in ['expired', 'other-resource', 'wrong-parent-token', 'light']:
            candidate = next(p for p in saved_candidates(plan) if p.name == ('坤宁宫东院餐厅' if case == 'light' else '冰窖餐厅'))
            if case == 'wrong-parent-token':
                candidate.dining_parent_activity_token = plan.stops[1].activity_token
            issued = issue_candidate(candidate, public_resource_id='another-resource' if case == 'other-resource' else resource.public_resource_id,
                expected_etag=etag, activity_token=dining_binding(plan.stops[0].activity_token),
                now=now - timedelta(minutes=11) if case == 'expired' else now)
            command = DiningInsertCommand(command_type='DINING_INSERT', after_activity_token=plan.stops[0].activity_token,
                meal_role='LUNCH', candidate_token=issued.candidate_token)
            with pytest.raises(CommandTargetChangedError):
                await service.apply_command(resource, command, expected_etag=etag, idempotency_key='invalid-' + case, now=now)
        current = await repo.get_result(resource)
        assert current.opaque_etag == etag
        assert current.result.model_dump() == stored.result.model_dump()
