"""Duplicate source-intent projection, not model-quality scores or live services."""
from datetime import datetime, timezone

import pytest

from app.trip_understanding.dining import SourceMealRef, source_meal_context
from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.models import UndoCommand, UserFacingTripResult
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.pipeline import PublicResultProjector, TripUnderstandingPipeline, canonical_sha256
from tests.test_experience_text_fidelity import DraftProvider, activity
from tests.test_named_meal_area_intent import area_plan
from tests.test_source_meal_selection import fixed_plan, selection
from tests.test_experience_v3_journey import repository_for

SOURCE = '北京。\nDay1：故宫博物院。\n- 中午：吃面和小吃。\n之后去景山公园。'


def meal(quote, **values):
    return dict(source_quote=quote, place_name=None, role='PLANNED', day_index=1,
                category='餐饮', meal_role='LUNCH', **values)


async def duplicate_output():
    return await TripUnderstandingPipeline(DraftProvider([
        activity('故宫博物院'), meal('吃面和小吃。'), meal('吃面和小吃'), activity('景山公园'),
    ]), ControlledSnapshotPlaceResolver()).run(SOURCE)


def projected(output, source=SOURCE):
    return PublicResultProjector().project(output.proposal.destination_name, output.proposal.destination_basis,
        output.activities, source_text=source, day_count=1, include_alternatives=True)


@pytest.mark.asyncio
async def test_nested_valid_same_occurrence_intents_create_one_slot_without_removing_mentions():
    output = await duplicate_output()
    anonymous = [m for m in output.proposal.mentions if m.meal_role == 'LUNCH']
    assert len(anonymous) == 2
    assert anonymous[0].span_start == anonymous[1].span_start
    assert anonymous[0].span_end != anonymous[1].span_end
    slot, = output.public_result.days[0].meal_slots
    assert slot.preference_text == '中午：吃面和小吃'
    assert slot.after_activity_token == output.public_result.days[0].activities[0].activity_token
    assert slot.before_activity_token == output.public_result.days[0].activities[1].activity_token
    # A repeat projection is deterministic and cannot remove source diagnostics.
    old_count = output.proposal.unprocessed_count
    assert len(projected(output).days[0].meal_slots) == 1
    assert len(output.proposal.mentions) == 4 and output.proposal.unprocessed_count == old_count


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['missing_source', 'invalid_source', 'other_start', 'meal_role', 'branch', 'group', 'anchor'])
async def test_dedup_requires_valid_same_source_role_branch_and_position(kind):
    output = await duplicate_output()
    rows = [row.compiled.mention for row in output.activities if row.compiled.mention.meal_role == 'LUNCH']
    source = SOURCE
    if kind == 'missing_source':
        source = None
    elif kind == 'invalid_source':
        rows[1].raw_text = '未在原文出现的证据'
    elif kind == 'other_start':
        rows[1].span_start += 1
        rows[1].raw_text = source[rows[1].span_start:rows[1].span_end]
    elif kind == 'meal_role':
        rows[1].meal_role = 'DINNER'
    elif kind == 'branch':
        rows[1].branch_id = 'another-declared-branch'
    elif kind == 'group':
        rows[1].choice_group_id = 'another-declared-group'
    elif kind == 'anchor':
        rows[1].sequence_index = 4  # After the second independent visit.
    assert len(projected(output, source).days[0].meal_slots) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('same_anchors', [True, False])
async def test_same_day_repeated_identical_line_remains_two_actual_meals(same_anchors):
    source = '北京。\nDay1：故宫博物院。\n- 中午：吃面和小吃。\n'
    source += '- 中午：吃面和小吃。\n之后去景山公园。' if same_anchors else '之后去景山公园。\n- 中午：吃面和小吃。'
    middle = [meal('吃面和小吃。', occurrence=2), activity('景山公园')] if same_anchors else [activity('景山公园'), meal('吃面和小吃。', occurrence=2)]
    output = await TripUnderstandingPipeline(DraftProvider([
        activity('故宫博物院'), meal('吃面和小吃。'), *middle,
    ]), ControlledSnapshotPlaceResolver()).run(source)
    day = output.public_result.days[0]
    assert len(day.meal_slots) == 2
    assert day.meal_slots[0].preference_text == day.meal_slots[1].preference_text
    assert (day.meal_slots[0].after_activity_token == day.meal_slots[1].after_activity_token) == same_anchors


@pytest.mark.asyncio
async def test_distinct_days_remain_distinct_even_when_recovered_meal_text_matches():
    output = await duplicate_output()
    second = [row.compiled.mention for row in output.activities if row.compiled.mention.meal_role == 'LUNCH'][1]
    second.day_index = 2
    result = PublicResultProjector().project(output.proposal.destination_name, output.proposal.destination_basis,
        output.activities, source_text=SOURCE, day_count=2)
    assert [len(day.meal_slots) for day in result.days] == [1, 1]


@pytest.mark.asyncio
async def test_existing_named_area_plus_anonymous_preference_still_keeps_one_slot():
    output = await area_plan('晚上：湖滨路吃本地菜，推荐春风馆、月湖楼。', extra_anon=True)
    assert len(output.public_result.days[0].activities) == 3
    assert len(output.public_result.days[0].meal_slots) == 1


@pytest.mark.asyncio
async def test_memory_save_readback_and_consumption_have_only_one_source_target():
    output = await duplicate_output()
    async with repository_for('memory') as repo:
        now = datetime.now(timezone.utc)
        created = await repo.create_demo(capability_hash='a' * 64, source_text=SOURCE,
            idempotency_key='duplicate-source-meal-create', request_hash=canonical_sha256({'source': SOURCE}), now=now, ttl_hours=24)
        job = await repo.claim_next(worker_id='fixed-meal-source', now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash='a' * 64, now=now)
        saved = await repo.get_result(resource)
        public = UserFacingTripResult.model_validate(saved.result.model_dump(mode='json'))
        assert len(public.days[0].meal_slots) == 1
        ref = SourceMealRef(day_index=1, slot_index=0)
        command = selection(public, fixed_plan(public), ref=ref, now=now,
            resource=resource.public_resource_id, etag=saved.opaque_etag)
        service = TripUnderstandingApplicationService(repo)
        await service.apply_command(resource, command, expected_etag=saved.opaque_etag,
            idempotency_key='duplicate-source-meal-select', now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash='a' * 64, now=now)
        selected_saved = await repo.get_result(resource)
        selected = selected_saved.result
        assert len(selected.days[0].meal_slots) == 1
        assert selected.days[0].meal_slots[0].selection_status == 'SELECTED'
        assert source_meal_context(selected, fixed_plan(selected), ref).status == 'EXISTING'
        assert sum(card.category == '餐饮' for card in selected.days[0].activities) == 1
        with pytest.raises(CommandTargetChangedError, match='slot changed'):
            source_meal_context(selected, fixed_plan(selected), SourceMealRef(day_index=1, slot_index=1))
        await service.apply_command(resource, UndoCommand(command_type='UNDO'),
            expected_etag=selected_saved.opaque_etag, idempotency_key='duplicate-source-meal-undo', now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash='a' * 64, now=now)
        restored = (await repo.get_result(resource)).result
        assert len(restored.days[0].meal_slots) == 1
        assert restored.days[0].meal_slots[0].selection_status == 'UNSELECTED'
        assert len(restored.days[0].activities) == 2
