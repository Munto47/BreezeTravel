"""Saved owner model/AMap replies preserve meal content without choosing a shop."""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.models import (
    ActivityMoveCommand, CreateFullRequest, MealSlotView, UndoCommand, UserFacingTripResult,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_semantic_supplement_budget import Client, provider


MEALS = ['晚餐：铜锅涮肉（南门涮肉）、炸酱面', '晚餐：烤鸭（四季民福、紫光园）']


@pytest.mark.parametrize("status", ["UNSPECIFIED", "LIGHT_FOOD_ITEMS_ONLY"])
def test_public_meal_evidence_status_is_a_safe_field_not_private_text(status):
    from evals.g07_text_convergence_v1.runner import _public_payload_is_redacted

    assert _public_payload_is_redacted({"days": [{"meal_slots": [{"meal_evidence_status": status}]}]})


@pytest.mark.parametrize("field", ["meal_evidence_status", "preference_text"])
@pytest.mark.parametrize("text", ["private evidence", '{"provider_binding":"private"}', "Traceback: failure"])
def test_public_meal_field_values_still_reject_private_diagnostic_text(field, text):
    from evals.g07_text_convergence_v1.runner import _public_payload_is_redacted

    assert not _public_payload_is_redacted({"days": [{"meal_slots": [{field: text}]}]})


def fixture():
    return json.loads((Path(__file__).parent / 'fixtures/live_owner_beijing_meal_preferences.json').read_text(encoding='utf-8'))


async def build_owner_meal_result():
    saved = fixture()
    calls = []

    def reply(request):
        params = {k: v for k, v in request.url.params.multi_items() if k != 'key'}
        recorded = next(c for c in saved['place_calls'] if c['path'] == request.url.path and c['query'] == params)
        calls.append(params)
        return httpx.Response(200, json=recorded['response'])

    client = Client(*saved['responses'])
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
        result = await TripUnderstandingPipeline(provider(client, deadline=10),
            AmapPlaceResolver(api_key='fixed-only', client=transport)).run(saved['source'])
    assert len(client.calls) == 2
    assert len(calls) == 13
    return result


def preferences(result):
    return [[slot.preference_text for slot in day.meal_slots] for day in result.days]


@pytest.mark.asyncio
async def test_actual_two_answers_and_thirteen_poi_replies_preserve_dinner_content_and_all_existing_visits():
    from evals.g07_text_convergence_v1.runner import _public_payload_is_redacted
    output = await build_owner_meal_result()
    public = output.public_result
    assert preferences(public) == [[MEALS[0]], [MEALS[1]], []]
    resolved = [a for a in output.activities if a.place is not None]
    assert [a.place.canonical_place_id for a in resolved] == [x['poi_id'] for x in fixture()['expected_main']]
    assert [[c.name for c in day.activities] for day in public.days] == [
        ['天安门广场', '故宫博物院', '景山公园', '什刹海', '后海'],
        ['国家体育场', '国家游泳中心'],
        ['天坛公园', '中国国家博物馆', '前门大街', '北京大栅栏', '杨梅竹斜街'],
    ]
    assert sum(len(c.source_details) for d in public.days for c in d.activities) == 15
    assert public.coverage.complete is False
    # ff370da independently removed two explicit reference warnings. This meal
    # projection must preserve the current ten; it does not recover those items.
    assert public.coverage.unprocessed_count == 10
    assert all(s.selection_status == 'UNSELECTED' and s.selected_activity_token is None for d in public.days for s in d.meal_slots)
    huguosi = next(m for m in output.proposal.mentions if m.atomic_place_name == '护国寺小吃')
    # Source meal classification is independent of the model's wrong role:
    # identifying SNACK must not silently promote this reference into a slot.
    assert huguosi.role == 'REFERENCE' and huguosi.meal_role == 'SNACK'
    assert public.days[2].meal_slots == []
    assert _public_payload_is_redacted(public.model_dump(mode='json'))
    assert not _public_payload_is_redacted({'meal_slots': [{'source_quote': 'whole private source', 'span_start': 0}]})


@pytest.mark.asyncio
async def test_multiline_meal_quote_keeps_every_preference_and_does_not_publish_other_source_lines():
    from tests.semantic_page_replays import FixedReplayPlaces
    source = '北京\nDay1：故宫博物院。\n晚餐：铜锅涮肉（南门涮肉）、\n炸酱面。\n这是未作为餐饮引用的另一段文字。'
    quote = '晚餐：铜锅涮肉（南门涮肉）、\n炸酱面'
    raw = dict(destination='北京', day_labels=['Day1'], unprocessed_quotes=[], activities=[
        dict(source_quote='故宫博物院', place_name='故宫博物院', role='PLANNED', day_index=1),
        dict(source_quote=quote, place_name=None, role='PLANNED', day_index=1, category='餐饮', meal_role='DINNER'),
    ])
    output = await TripUnderstandingPipeline(provider(Client(raw)), FixedReplayPlaces()).run(source)
    assert preferences(output.public_result) == [[quote.replace('\n', ' ')]]
    assert '另一段文字' not in output.public_result.model_dump_json()


@pytest.mark.asyncio
async def test_edits_and_old_public_defaults_preserve_meal_preferences_without_guessing_selection():
    current = (await build_owner_meal_result()).public_result
    moved = apply_public_command(current, ActivityMoveCommand(command_type='ACTIVITY_MOVE',
        activity_token=current.days[0].activities[-1].activity_token, target_day_index=1, target_position=3)).result
    assert preferences(moved) == preferences(current)
    assert preferences(UserFacingTripResult.model_validate_json(moved.model_dump_json())) == preferences(current)
    assert MealSlotView(meal_role='DINNER').preference_text is None
    assert MealSlotView(meal_role='DINNER').selection_status == 'UNKNOWN'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['memory', 'postgres'])
async def test_saved_meal_content_permissions_source_delete_edit_undo_and_trip_delete(kind):
    from app.trip_understanding.errors import ResourceAccessDeniedError, ResourceGoneError
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from tests.test_experience_v3_journey import repository_for

    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest.model_validate({'mode': 'FULL',
            'source': {'type': 'TEXT', 'text': fixture()['source']}}), owner_user_id='experience-owner',
            idempotency_key='meal-preference-create', now=now)
        job = await repo.claim_next(worker_id='fixed-meal-preference', now=now, lease_seconds=60)
        await repo.complete_job(job, await build_owner_meal_result(), now=now)

        async def read():
            resource = await repo.authorize(created.accepted.public_resource_id,
                capability_hash=None, user_id='experience-owner', now=now)
            return resource, await repo.get_result(resource)

        resource, stored = await read()
        expected = [[MEALS[0]], [MEALS[1]], []]
        assert preferences(stored.result) == expected
        with pytest.raises(ResourceAccessDeniedError):
            await repo.authorize(resource.public_resource_id, capability_hash=None, user_id='another-owner', now=now)
        await repo.delete_source(resource, user_id='experience-owner', idempotency_key='delete-meal-source',
            request_hash='a' * 64, now=now)
        resource, stored = await read()
        assert preferences(stored.result) == expected
        await service.apply_command(resource, ActivityMoveCommand(command_type='ACTIVITY_MOVE',
            activity_token=stored.result.days[0].activities[-1].activity_token, target_day_index=1, target_position=3),
            expected_etag=stored.opaque_etag, idempotency_key='move-meal-neighbor', now=now)
        resource, changed = await read()
        assert preferences(changed.result) == expected
        await service.apply_command(resource, UndoCommand(command_type='UNDO'), expected_etag=changed.opaque_etag,
            idempotency_key='undo-meal-neighbor', now=now)
        resource, restored = await read()
        assert preferences(restored.result) == expected
        if kind == 'postgres':
            assert await repo._pool.fetchval('SELECT count(*) FROM trip_understanding_sources WHERE understanding_id=$1 AND encrypted_content IS NOT NULL', resource.understanding_id) == 0
            assert await repo._pool.fetchval('SELECT count(*) FROM trip_understanding_source_claims WHERE understanding_id=$1', resource.understanding_id) == 0
        await repo.delete_trip(resource, capability_hash=None, user_id='experience-owner', idempotency_key='delete-meal-trip',
            request_hash='b' * 64, now=now)
        with pytest.raises(ResourceGoneError):
            await read()
