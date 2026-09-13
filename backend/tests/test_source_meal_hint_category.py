"""An explicit, source-validated meal hint does not require restaurant category."""
import json
from pathlib import Path

import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.experience_inference import ExperienceQwenProvider, SemanticDraft, proposal_from_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_named_meal_area_intent import fixture, build_named_meal_area_result
from tests.test_semantic_supplement_budget import Client


async def build_deepseek_meal_hint_result():
    saved = fixture()
    pair = json.loads((Path(__file__).parent / 'fixtures/live_owner_shanghai_deepseek_json_object.json').read_text(encoding='utf-8'))
    calls = []

    def reply(request):
        params = {k: v for k, v in request.url.params.multi_items() if k != 'key'}
        row = next(row for row in saved['place_calls'] if row['path'] == request.url.path and row['query'] == params)
        calls.append(params)
        return httpx.Response(200, json=row['response'])

    client = Client(*pair['responses'])
    provider = ExperienceQwenProvider(api_key='fixed', base_url='https://fixed.invalid', model='fixed-real-pair',
        client=client, enable_source_visits=True, deadline_seconds=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
        output = await TripUnderstandingPipeline(provider, AmapPlaceResolver(api_key='fixed', client=transport)).run(saved['source'])
    assert len(client.calls) == 2 and len(calls) == 19
    return output


@pytest.mark.asyncio
async def test_saved_deepseek_pair_restores_only_grounded_dinner_and_keeps_all_failures():
    output = await build_deepseek_meal_hint_result()
    saved = fixture()
    actual = [(a.compiled.mention.day_index, a.compiled.mention.atomic_place_name,
               a.place.canonical_place_id if a.place else None)
              for a in output.activities if a.compiled.mention.role == 'PLANNED' and a.compiled.mention.atomic_place_name]
    assert actual == [(x['day'], x['name'], x['id']) for x in saved['expected_main_identities']]
    days = output.public_result.days
    assert [len(day.activities) for day in days] == [7, 10, 0]
    assert [len(day.alternatives) for day in days] == [1, 2, 9]
    assert [[slot.meal_role for slot in day.meal_slots] for day in days] == [[], ['DINNER'], []]
    dinner = days[1].meal_slots[0]
    area = days[1].activities[-1]
    assert area.name == '进贤路' and area.category == '地点' and area.status == 'NEEDS_CONFIRMATION'
    assert area.meal_role == 'DINNER' and dinner.after_activity_token == area.activity_token
    assert dinner.selection_status == 'UNSELECTED' and dinner.selected_activity_token is None
    assert all(text in dinner.preference_text for text in ('人和馆', '老正兴', '红烧肉', '响油鳝糊', '油爆虾'))
    assert [a.meal_role for a in days[1].alternatives] == ['LUNCH', 'LUNCH']
    assert all(not a.source_details for day in days for a in day.activities + day.alternatives)
    assert not output.public_result.coverage.complete and output.public_result.coverage.unprocessed_count == 7
    assert any(d.category == 'SOURCE_VISITS_UNPROCESSED' for d in output.proposal.diagnostics)
    assert all(m.role == 'REFERENCE' for m in output.proposal.mentions if m.atomic_place_name in {'大壶春', '沈大成', '鲜得来'})


@pytest.mark.asyncio
async def test_saved_qwen_pair_keeps_all_three_existing_meals_and_six_missing_alternatives():
    output = await build_named_meal_area_result()
    assert [[s.meal_role for s in d.meal_slots] for d in output.public_result.days] == [['LUNCH'], ['LUNCH', 'DINNER'], []]
    assert [len(d.activities) for d in output.public_result.days] == [7, 10, 0]
    assert sum(len(d.alternatives) for d in output.public_result.days) == 6
    assert output.public_result.coverage.unprocessed_count == 7 and not output.public_result.coverage.complete


def named_area(source, *, meal='DINNER', occurrence=1, day=1, role='PLANNED'):
    draft = SemanticDraft.model_validate({'destination': '广州', 'activities': [{
        'source_quote': '星河街', 'place_name': '星河街', 'occurrence': occurrence,
        'day_index': day, 'role': role, 'category': '地点', 'meal_role': meal,
    }]})
    return proposal_from_draft(source, draft, allow_partial=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('text,meal', [
    ('中午在星河街吃面。', 'LUNCH'), ('晚上在星河街吃饭。', 'DINNER'),
    ('下午茶在星河街品尝点心。', 'SNACK'),
])
async def test_grounded_meal_hint_preserves_area_category_role_and_one_unselected_slot(text, meal):
    source = '广州\nDay1：' + text
    plan = named_area(source, meal=meal)
    mention, = plan.mentions
    assert (mention.category_hint, mention.role, mention.meal_role) == ('地点', 'PLANNED', meal)
    result = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=plan)
    assert len(result.public_result.days[0].activities) == 1
    slot, = result.public_result.days[0].meal_slots
    assert slot.meal_role == meal and slot.selection_status == 'UNSELECTED'


@pytest.mark.parametrize('source,occurrence,day', [
    ('广州\nDay1：晚上不在星河街吃饭。', 1, 1),
    ('广州\nDay1：晚上取消在星河街吃饭。', 1, 1),
    ('广州\nDay1：晚上先在小馆吃饭，随后到星河街散步。', 1, 1),
    ('广州\nDay1：晚餐在小馆吃饭。之后到星河街拍照。', 1, 1),
    ('广州\nDay1：中午在星河街吃面。\n晚上又到星河街散步。', 2, 1),
    ('广州\nDay1：晚上在星河街吃饭。\nDay2：早上到星河街拍照。', 2, 2),
    ('广州\nDay1：晚上到星河街拍照。', 1, 1),
])
def test_model_hint_cannot_borrow_denied_or_another_visit_meal(source, occurrence, day):
    plan = named_area(source, occurrence=occurrence, day=day)
    assert all(m.meal_role is None for m in plan.mentions)


@pytest.mark.parametrize('source,role', [
    ('广州\nDay1：若有时间，晚上在星河街吃饭。', 'OPTIONAL'),
    ('广州\nDay1：资料说明：星河街曾经有人吃晚餐，不安排到访。', 'REFERENCE'),
])
def test_meal_field_does_not_promote_an_optional_or_reference_visit(source, role):
    plan = named_area(source, role=role)
    assert all(m.role != 'PLANNED' and m.category_hint == '地点' for m in plan.mentions)
