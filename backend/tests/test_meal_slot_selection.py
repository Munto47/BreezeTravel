"""One saved meal selection survives token renewal without guessing by position."""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError, model_serializer

from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.models import (
    ActivityDeleteCommand, ActivityMoveCommand, ActivityTextEditCommand, CreateFullRequest,
    DiningInsertCommand, PlaceReplaceCommand, UndoCommand, UserFacingTripResult,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_dining_recommendations import anchor, restaurant
from tests.test_semantic_supplement_budget import Client, provider


def fixture():
    return json.loads((Path(__file__).parent / 'fixtures/live_shenzhen_gate_actions.json').read_text(encoding='utf-8'))


class FixedPlaces(FixedReplayPlaces):
    async def resolve(self, **query):
        value = await super().resolve(**query)
        value.provider_binding.update(coordinates={'longitude': 114.05, 'latitude': 22.55})
        return value


async def build_meal_result():
    saved = fixture()
    return await TripUnderstandingPipeline(provider(Client(*saved['responses'])), FixedPlaces()).run(saved['source'])


def fixed_plan(result):
    return SimpleNamespace(stops=[anchor().model_copy(update={
        'activity_token': card.activity_token, 'name': card.name, 'city': '深圳',
        'day_index': i + 1, 'day_label': day.label, 'sequence_index': j, 'category': card.category,
    }) for i, day in enumerate(result.days) for j, card in enumerate(day.activities) if card.status == 'READY'])


def selected_restaurant():
    return restaurant().model_copy(update={'city': '深圳', 'name': '合成采纳餐厅'})


def adopt(result):
    return apply_public_command(result, DiningInsertCommand(command_type='DINING_INSERT',
        after_activity_token=result.days[0].activities[0].activity_token,
        candidate_token='fixed-candidate-not-sent-to-service-00000000', meal_role='LUNCH'),
        confirmed_place=selected_restaurant(), dining_plan=fixed_plan(result)).result


def assert_selected(result):
    slot = result.days[0].meal_slots[0]
    assert slot.selection_status == 'SELECTED'
    card = next(c for c in result.days[0].activities if c.activity_token == slot.selected_activity_token)
    assert card.name == '合成采纳餐厅'
    assert card.meal_role == slot.meal_role
    assert result.days[1].meal_slots[0].selection_status == 'UNSELECTED'
    # The exact serialized public view must validate on a fresh read.
    UserFacingTripResult.model_validate_json(result.model_dump_json())
    return card


@pytest.mark.asyncio
async def test_projection_and_adoption_create_explicit_selection_without_consuming_other_day():
    from evals.g07_text_convergence_v1.runner import _public_payload_is_redacted
    current = (await build_meal_result()).public_result
    assert [slot.selection_status for day in current.days for slot in day.meal_slots] == ['UNSELECTED'] * 2
    result = adopt(current)
    assert_selected(result)
    assert current.days[0].meal_slots[0].selection_status == 'UNSELECTED'
    assert _public_payload_is_redacted(result.model_dump(mode='json'))
    assert not _public_payload_is_redacted({'meal_slots': [{'source_quote': 'private source'}]})


@pytest.mark.asyncio
async def test_old_unknown_selection_is_not_inferred_but_can_be_explicitly_adopted():
    current = (await build_meal_result()).public_result
    old = current.model_dump(mode='json')
    for day in old['days']:
        for slot in day['meal_slots']:
            slot.pop('selection_status', None)
            slot.pop('selected_activity_token', None)
    restored = UserFacingTripResult.model_validate(old)
    assert all(s.selection_status == 'UNKNOWN' and s.selected_activity_token is None for d in restored.days for s in d.meal_slots)
    updated = adopt(restored)
    assert updated.days[0].meal_slots[0].selection_status == 'SELECTED'
    assert updated.days[1].meal_slots[0].selection_status == 'UNKNOWN'


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['move_same_day', 'rename', 'time_edit'])
async def test_selection_follows_card_not_source_interval_and_pending_identity_is_preserved(action):
    selected = adopt((await build_meal_result()).public_result)
    card = assert_selected(selected)
    if action == 'move_same_day':
        command = ActivityMoveCommand(command_type='ACTIVITY_MOVE', activity_token=card.activity_token,
            target_day_index=1, target_position=0)
    elif action == 'rename':
        command = ActivityTextEditCommand(command_type='ACTIVITY_TEXT_EDIT', activity_token=card.activity_token, name=card.name)
    else:
        command = ActivityTextEditCommand(command_type='ACTIVITY_TEXT_EDIT', activity_token=card.activity_token, time_hint='用户稍后确定')
    updated = apply_public_command(selected, command).result
    updated_card = assert_selected(updated)
    assert updated_card.activity_token != card.activity_token
    if action == 'move_same_day':
        assert updated.days[0].activities[0].activity_token == updated_card.activity_token
    if action == 'rename':
        assert updated_card.status == 'NEEDS_CONFIRMATION'


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['delete', 'move_other_day'])
async def test_removing_selected_card_reopens_only_its_slot_and_undo_restores_link(action):
    selected = adopt((await build_meal_result()).public_result)
    card = assert_selected(selected)
    command = (ActivityDeleteCommand(command_type='ACTIVITY_DELETE', activity_token=card.activity_token) if action == 'delete'
        else ActivityMoveCommand(command_type='ACTIVITY_MOVE', activity_token=card.activity_token, target_day_index=2, target_position=0))
    changed = apply_public_command(selected, command).result
    assert all(s.selection_status == 'UNSELECTED' and s.selected_activity_token is None for d in changed.days for s in d.meal_slots)
    restored = apply_public_command(changed, UndoCommand(command_type='UNDO'), undo_result=selected).result
    assert_selected(restored)


@pytest.mark.asyncio
@pytest.mark.parametrize('category', ['餐饮', '景点'])
async def test_replacement_keeps_pending_restaurant_but_cannot_select_a_non_restaurant(category):
    current = adopt((await build_meal_result()).public_result)
    card = assert_selected(current)
    changed = apply_public_command(current, PlaceReplaceCommand(command_type='PLACE_REPLACE',
        activity_token=card.activity_token, replacement={'name': '合成更换地点',
            'category': category, 'area_or_address': '固定地址'})).result
    slot = changed.days[0].meal_slots[0]
    assert slot.selection_status == ('SELECTED' if category == '餐饮' else 'UNSELECTED')
    if category == '餐饮':
        selected = next(c for c in changed.days[0].activities if c.activity_token == slot.selected_activity_token)
        assert selected.status == 'NEEDS_CONFIRMATION'
    else:
        assert slot.selected_activity_token is None
    UserFacingTripResult.model_validate_json(changed.model_dump_json())


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['ambiguous', 'missing_plan', 'wrong_position'])
async def test_adoption_cannot_guess_an_ambiguous_or_unverified_source_slot(boundary):
    current = (await build_meal_result()).public_result
    if boundary == 'ambiguous':
        current.days[0].meal_slots.append(current.days[0].meal_slots[0].model_copy(deep=True))
    plan = fixed_plan(current) if boundary != 'missing_plan' else None
    after = current.days[0].activities[-1 if boundary == 'wrong_position' else 0].activity_token
    result = apply_public_command(current, DiningInsertCommand(command_type='DINING_INSERT',
        after_activity_token=after, candidate_token='fixed-candidate-not-sent-to-service-00000000', meal_role='LUNCH'),
        confirmed_place=selected_restaurant(), dining_plan=plan).result
    assert all(s.selection_status == 'UNSELECTED' and s.selected_activity_token is None for d in result.days for s in d.meal_slots)


@pytest.mark.asyncio
async def test_insert_before_uses_the_existing_meal_context_selection():
    current = (await build_meal_result()).public_result
    slot = current.days[0].meal_slots[0]
    slot.after_activity_token = None
    slot.before_activity_token = current.days[0].activities[0].activity_token
    result = apply_public_command(current, DiningInsertCommand(command_type='DINING_INSERT',
        after_activity_token=slot.before_activity_token, insert_before=True,
        candidate_token='fixed-candidate-not-sent-to-service-00000000', meal_role='LUNCH'),
        confirmed_place=selected_restaurant(), dining_plan=fixed_plan(current)).result
    assert assert_selected(result) is result.days[0].activities[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['missing_token', 'other_day', 'wrong_role', 'not_restaurant', 'unknown_with_token', 'duplicate_slot'])
async def test_public_selected_relationship_rejects_invalid_links(bad):
    current = adopt((await build_meal_result()).public_result)
    payload = current.model_dump(mode='json')
    slot = payload['days'][0]['meal_slots'][0]
    if bad == 'missing_token': slot['selected_activity_token'] = None
    elif bad == 'other_day': slot['selected_activity_token'] = payload['days'][1]['activities'][0]['activity_token']
    elif bad == 'wrong_role': slot['meal_role'] = 'DINNER'
    elif bad == 'not_restaurant': payload['days'][0]['activities'][1]['category'] = '景点'
    elif bad == 'duplicate_slot': payload['days'][0]['meal_slots'].append(dict(slot))
    else: slot['selection_status'] = 'UNKNOWN'
    with pytest.raises(ValidationError):
        UserFacingTripResult.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['memory', 'postgres'])
async def test_saved_candidate_selection_move_delete_undo_and_old_readback(kind):
    from app.trip_understanding.candidates import issue_candidate
    from app.trip_understanding.dining import dining_binding
    from app.trip_understanding.errors import ResourceAccessDeniedError
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from tests.test_experience_v3_journey import repository_for

    async with repository_for(kind) as repository:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repository)
        saved = fixture()
        created = await service.create_full(CreateFullRequest.model_validate({'mode': 'FULL',
            'source': {'type': 'TEXT', 'text': saved['source']}}), owner_user_id='experience-owner',
            idempotency_key='meal-selection-create', now=now)
        job = await repository.claim_next(worker_id='fixed-meal-selection', now=now, lease_seconds=60)
        await repository.complete_job(job, await build_meal_result(), now=now)

        async def read():
            resource = await repository.authorize(created.accepted.public_resource_id,
                capability_hash=None, user_id='experience-owner', now=now)
            return resource, await repository.get_result(resource)

        resource, stored = await read()
        with pytest.raises(ResourceAccessDeniedError):
            await repository.authorize(resource.public_resource_id, capability_hash=None, user_id='another-owner', now=now)
        token = stored.result.days[0].activities[0].activity_token
        candidate = issue_candidate(selected_restaurant(), public_resource_id=resource.public_resource_id,
            activity_token=dining_binding(token), expected_etag=stored.opaque_etag, now=now)
        command = DiningInsertCommand(command_type='DINING_INSERT', after_activity_token=token,
            candidate_token=candidate.candidate_token, meal_role='LUNCH')
        outcome = await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
            idempotency_key='meal-adopt', now=now)
        replayed = await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
            idempotency_key='meal-adopt', now=now)
        assert replayed.replayed and outcome.opaque_etag == replayed.opaque_etag
        resource, stored = await read()
        assert_selected(stored.result)
        for index, action in enumerate(['move_same_day', 'delete', 'move_other_day']):
            selected = assert_selected(stored.result)
            command = (ActivityDeleteCommand(command_type='ACTIVITY_DELETE', activity_token=selected.activity_token) if action == 'delete'
                else ActivityMoveCommand(command_type='ACTIVITY_MOVE', activity_token=selected.activity_token,
                    target_day_index=1 if action == 'move_same_day' else 2, target_position=0))
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
                idempotency_key=f'meal-change-{index}', now=now)
            resource, changed = await read()
            if action == 'move_same_day': assert_selected(changed.result)
            else: assert changed.result.days[0].meal_slots[0].selection_status == 'UNSELECTED'
            await service.apply_command(resource, UndoCommand(command_type='UNDO'), expected_etag=changed.opaque_etag,
                idempotency_key=f'meal-undo-{index}', now=now)
            resource, stored = await read()
            assert_selected(stored.result)

        # Selecting a hotel has a separate whole-result token renewal path.
        from app.trip_understanding.stay import ControlledStayRouteProvider, StayRecommendationEngine
        from tests.test_g02_map_stay import _test_registry
        from tests.test_stay_overnight_segments import CityHotels
        await repository.refresh_stay_suggestions(resource, expected_etag=stored.opaque_etag,
            idempotency_key='meal-stay-refresh', now=now)
        stay_job = await repository.claim_next_stay(worker_id='fixed-meal-stay', now=now, lease_seconds=60)
        assert stay_job is not None
        plan = await repository.load_stay_plan(stay_job)
        stay = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider(),
            brand_registry=_test_registry()).recommend(plan, observed_at=now)
        await repository.complete_stay_job(stay_job, stay, now=now)
        view = await repository.get_stay_view(resource)
        candidate = view.candidates[0]
        await service.select_stay(resource, candidate_token=candidate.candidate_token,
            expected_etag=stored.opaque_etag, idempotency_key='meal-stay-select', now=now)
        resource, stored = await read()
        assert_selected(stored.result)

        # Insert a genuinely old-shape immutable version, never UPDATE old rows.
        class LegacyResult(UserFacingTripResult):
            @model_serializer(mode='wrap')
            def old_shape(self, handler):
                payload = handler(self)
                for day in payload['days']:
                    for slot in day['meal_slots']:
                        slot.pop('selection_status', None)
                        slot.pop('selected_activity_token', None)
                return payload
        old_created = await service.create_full(CreateFullRequest.model_validate({'mode': 'FULL',
            'source': {'type': 'TEXT', 'text': saved['source']}}), owner_user_id='experience-owner',
            idempotency_key='old-meal-create', now=now)
        old_job = await repository.claim_next(worker_id='fixed-old-meal', now=now, lease_seconds=60)
        output = await build_meal_result()
        output.public_result = LegacyResult.model_validate(output.public_result.model_dump())
        await repository.complete_job(old_job, output, now=now)
        old_resource = await repository.authorize(old_created.accepted.public_resource_id,
            capability_hash=None, user_id='experience-owner', now=now)
        old = await repository.get_result(old_resource)
        if kind == 'postgres':
            assert all(s.selection_status == 'UNKNOWN' and s.selected_activity_token is None for d in old.result.days for s in d.meal_slots)
            payload = json.loads(await repository._pool.fetchval('SELECT public_json FROM trip_understanding_results WHERE result_id=$1', old_resource.current_result_id))
            assert all('selection_status' not in s for d in payload['days'] for s in d['meal_slots'])
