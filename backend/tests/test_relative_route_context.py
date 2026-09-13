"""Real provider/pipeline and command mutations, fixed identity/route transports.

These test the shared context/carry entrance. They do not claim PostgreSQL hotel
selection, a fresh model answer, or live AMap accuracy.
"""
import json

import httpx
import pytest

from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.commands import apply_public_command, refresh_meal_slot_tokens
from app.trip_understanding.dining import source_meal_context
from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.map_repository import _plan_for_result
from app.trip_understanding.models import (
    ActivityInsertCommand, ActivityMoveCommand, DiningInsertCommand, PlaceConfirmCommand,
    RedoCommand, SourceMealRef, UndoCommand, UserFacingTripResult,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.relative_route_context import carry_source_order, route_context, seed_order_for_output
from app.trip_understanding.relative_route_options import build_relative_route_options
from app.trip_understanding.source_order import PrivateSourceOrderState
from tests.test_dining_recommendations import restaurant
from tests.test_relative_route_flow import FixedPlaces, NAMES
from tests.test_relative_route_options import FixedAmap, current_edges
from tests.test_semantic_supplement_budget import Client


SCOPES = (f"Day1：{NAMES[0]}。中午找餐厅吃午餐。{NAMES[1]}。",
          f"Day2：{'、'.join(NAMES[2:])}。以上只是初步顺序，可以根据交通调整。")
SOURCE = "北京两日游。\n" + "\n".join(SCOPES)


async def prepared():
    rows = [dict(source_quote=name, place_name=name, role="PLANNED", category="景点",
        day_index=1 if i < 2 else 2) for i, name in enumerate(NAMES)]
    rows.insert(1, dict(source_quote="午餐", place_name=None, role="PLANNED", category="餐饮", day_index=1, meal_role="LUNCH"))
    raw = dict(destination="北京", day_labels=[None, None], unprocessed_quotes=[], activities=rows,
        order_groups=[dict(kind="INITIAL_ORDER", activity_indices=indices, scope_quote=scope)
                      for indices, scope in zip(([0, 2], [3, 4, 5, 6]), SCOPES)])
    client = Client(raw)
    provider = ExperienceQwenProvider(api_key="fixed-test-only", base_url="https://fixed.invalid",
        model="fixed-order-context", client=client, enable_source_visits=False)
    output = await TripUnderstandingPipeline(provider, FixedPlaces()).run(SOURCE)
    assert len(client.calls) == 1
    assert [[card.name for card in day.activities] for day in output.public_result.days] == [list(NAMES[:2]), list(NAMES[2:])]
    return output.public_result, {"source_order": seed_order_for_output(output)}


def map_plan(result):
    bindings = {}
    for day in result.days:
        for card in day.activities:
            if card.name in NAMES and card.status == "READY":
                index = NAMES.index(card.name)
                bindings[card.activity_token] = (f"poi-{index}", "AUTO_MATCHED", {
                    "city": "北京", "coordinates": {"longitude": 116.30 + index / 100, "latitude": 39.9}})
    return _plan_for_result("fixed-order-context", 1, result, bindings)


def carry(proposal, before, mutation, command=None, **kwargs):
    return {"source_order": carry_source_order(proposal, before, mutation.result, mutation.token_map,
        command=command, **kwargs)}


def origins(result, proposal):
    state = PrivateSourceOrderState.model_validate(proposal["source_order"])
    return {card.name: state.bindings[card.activity_token] for day in result.days for card in day.activities}


async def compare_day(result, proposal, day=2):
    visits, constraints = route_context(result, map_plan(result), proposal)
    fixture = FixedAmap()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.respond)) as client:
        output = await build_relative_route_options(visits, day_index=day, constraints=constraints,
            current_edges=current_edges(visits), provider=AmapRouteProvider(api_key="fixed-test-only", client=client))
    return output, visits, constraints, fixture.calls


@pytest.mark.asyncio
async def test_selected_day1_source_lunch_does_not_block_a_real_day2_comparison():
    before, proposal = await prepared()
    plan = map_plan(before)
    target = SourceMealRef(day_index=1, slot_index=0)
    context = source_meal_context(before, plan, target)
    command = DiningInsertCommand(command_type="DINING_INSERT", after_activity_token=context.after_activity_token,
        insert_before=context.insert_before, meal_role=context.meal_role, meal_slot=target,
        candidate_token="fixed-candidate-not-sent-to-service-00000000")
    mutation = apply_public_command(before, command, confirmed_place=restaurant(), dining_plan=plan)
    after = UserFacingTripResult.model_validate(mutation.result.model_dump(mode="json"))
    assert after.days[0].meal_slots[0].selection_status == "SELECTED"
    changed = carry(proposal, before, mutation, command)
    output, visits, constraints, calls = await compare_day(after, changed)
    assert len(output.options) == 1 and output.no_options_reason is None and len(calls) == 6
    assert constraints.protected_adjacencies == ()
    assert [v.stop.name for v in visits if v.meal_role] == [restaurant().name]
    assert output.options[0].minutes_saved == 45
    assert origins(after, changed)[restaurant().name].source_kind == "USER_ADDED"
    assert all(origins(after, changed)[name] == origins(before, proposal)[name] for name in NAMES)


@pytest.mark.asyncio
async def test_unselected_double_anchor_is_local_and_keeps_other_day_available():
    result, proposal = await prepared()
    output, visits, constraints, calls = await compare_day(result, proposal)
    assert len(output.options) == 1 and len(calls) == 6
    assert constraints.protected_adjacencies == ((visits[0].visit_id, visits[1].visit_id),)
    assert not any(v.meal_role for v in visits)
    assert all(v.stop.day_index == 2 for v in visits if v.visit_id in output.options[0].before_visit_order)


@pytest.mark.asyncio
async def test_manual_insert_inside_source_meal_gap_protects_current_interval_only():
    before, proposal = await prepared()
    command = ActivityInsertCommand(command_type="ACTIVITY_INSERT", day_index=1, position=1, name="新加书店")
    mutation = apply_public_command(before, command)
    changed = carry(proposal, before, mutation, command)
    output, visits, constraints, calls = await compare_day(mutation.result, changed)
    assert len(output.options) == 1 and len(calls) == 6 and constraints.protected_adjacencies == ()
    assert [v.stop.name for v in visits if v.meal_role] == [NAMES[0], "新加书店", NAMES[1]]
    inserted = next(v for v in visits if v.stop.name == "新加书店")
    assert inserted.source_kind == "USER_ADDED" and inserted.stop.resolution_status == "NEEDS_CONFIRMATION"
    assert all(not v.meal_role for v in visits if v.stop.day_index == 2)


@pytest.mark.asyncio
async def test_manual_move_across_days_does_not_leave_a_foreign_meal_anchor_or_lose_visit():
    before, proposal = await prepared()
    token = before.days[0].activities[1].activity_token
    command = ActivityMoveCommand(command_type="ACTIVITY_MOVE", activity_token=token, target_day_index=2, target_position=0)
    mutation = apply_public_command(before, command)
    changed = carry(proposal, before, mutation, command)
    slot = mutation.result.days[0].meal_slots[0]
    assert slot.before_activity_token is None and slot.after_activity_token == mutation.result.days[0].activities[0].activity_token
    output, visits, constraints, _calls = await compare_day(mutation.result, changed)
    assert output.options and constraints.protected_adjacencies == ()
    assert [v.stop.name for v in visits if v.meal_role] == [NAMES[0]]
    assert origins(mutation.result, changed) == origins(before, proposal)
    assert next(v for v in visits if v.stop.name == NAMES[1]).stop.day_index == 2


@pytest.mark.asyncio
async def test_unpositioned_source_meal_protects_only_its_day_not_the_whole_trip():
    result, proposal = await prepared()
    # This is a supported old-reader state, not an invented empty public result.
    payload = result.model_dump(mode="json")
    payload["days"][0]["meal_slots"][0].update(after_activity_token=None, before_activity_token=None)
    old = UserFacingTripResult.model_validate(payload)
    output, visits, _constraints, _calls = await compare_day(old, proposal)
    assert output.options and [v.stop.name for v in visits if v.meal_role] == list(NAMES[:2])


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", ["missing_city_same_name", "same_id_alias", "different_place", "different_id_same_name"])
async def test_confirmation_preserves_the_same_source_visit_and_invalidates_a_real_replacement(confirmation):
    result, proposal = await prepared()
    before = result.model_copy(deep=True)
    card = before.days[0].activities[0]
    card.status, card.city = "NEEDS_CONFIRMATION", None
    confirmed = restaurant().model_copy(update={"canonical_place_id": "poi-0", "name": card.name, "category": "景点"})
    current_id = None
    if confirmation == "same_id_alias":
        current_id = "poi-0"
        confirmed = confirmed.model_copy(update={"name": card.name + "规范名称"})
    elif confirmation == "different_place":
        confirmed = confirmed.model_copy(update={"canonical_place_id": "different-place", "name": "其他公园"})
    elif confirmation == "different_id_same_name":
        current_id = "old-distinct-identity"
    command = PlaceConfirmCommand(command_type="PLACE_CONFIRM", activity_token=card.activity_token,
        candidate_token="fixed-candidate-not-sent-to-service-00000000")
    mutation = apply_public_command(before, command, confirmed_place=confirmed, current_place_id=current_id)
    changed = carry(proposal, before, mutation, command, confirmed_place=confirmed, current_place_id=current_id)
    old_state = PrivateSourceOrderState.model_validate(proposal["source_order"])
    new_state = PrivateSourceOrderState.model_validate(changed["source_order"])
    old_origin = old_state.bindings[card.activity_token]
    new_origin = new_state.bindings[mutation.token_map[card.activity_token]]
    same = confirmation in {"missing_city_same_name", "same_id_alias"}
    assert (new_origin == old_origin) is same
    assert len(new_state.groups) == (2 if same else 1)
    assert (new_origin.source_kind == "UNKNOWN") is (not same)
    assert new_state.groups[-1] == old_state.groups[-1]  # Other day's meaning remains intact.
    if not same:
        undo = apply_public_command(mutation.result, UndoCommand(command_type="UNDO"), undo_result=before)
        restored = carry(proposal, before, undo, UndoCommand(command_type="UNDO"))
        assert restored["source_order"]["groups"] == proposal["source_order"]["groups"]
        assert origins(undo.result, restored) == origins(before, proposal)
        redo = apply_public_command(undo.result, RedoCommand(command_type="REDO"), redo_result=mutation.result)
        replayed = carry(changed, mutation.result, redo, RedoCommand(command_type="REDO"))
        assert replayed["source_order"]["groups"] == changed["source_order"]["groups"]
        assert origins(redo.result, replayed) == origins(mutation.result, changed)


@pytest.mark.asyncio
async def test_hotel_token_rollover_and_undo_redo_use_target_snapshot_without_source_payload():
    before, original = await prepared()
    # Exactly the existing hotel carry boundary: unchanged business cards,
    # renewed tokens and refreshed meal references. This is not a PG hotel run.
    hotel = before.model_copy(deep=True)
    token_map = {}
    for i, card in enumerate(c for d in hotel.days for c in d.activities):
        token_map[card.activity_token] = f"hotel-version-token-{i:020d}"
        card.activity_token = token_map[card.activity_token]
    refresh_meal_slot_tokens(hotel.days, token_map)
    hotel = UserFacingTripResult.model_validate(hotel.model_dump(mode="json"))
    hotel_proposal = {"source_order": carry_source_order(original, before, hotel, token_map)}
    assert origins(hotel, hotel_proposal) == origins(before, original)
    assert hotel_proposal["source_order"]["groups"] == original["source_order"]["groups"]
    command = ActivityMoveCommand(command_type="ACTIVITY_MOVE", activity_token=hotel.days[1].activities[1].activity_token,
        target_day_index=2, target_position=2)
    moved = apply_public_command(hotel, command)
    moved_proposal = carry(hotel_proposal, hotel, moved, command)
    undo = apply_public_command(moved.result, UndoCommand(command_type="UNDO"), undo_result=hotel)
    restored = carry(hotel_proposal, hotel, undo, UndoCommand(command_type="UNDO"))
    redo = apply_public_command(undo.result, RedoCommand(command_type="REDO"), redo_result=moved.result)
    replayed = carry(moved_proposal, moved.result, redo, RedoCommand(command_type="REDO"))
    assert origins(undo.result, restored) == origins(hotel, hotel_proposal)
    assert origins(redo.result, replayed) == origins(moved.result, moved_proposal)
    assert restored["source_order"]["groups"] == hotel_proposal["source_order"]["groups"]
    assert replayed["source_order"]["groups"] == moved_proposal["source_order"]["groups"]
    for payload in (original, hotel_proposal, moved_proposal, restored, replayed):
        text = json.dumps(payload, ensure_ascii=False)
        assert SOURCE not in text and not any(name in text for name in NAMES)
        assert all(key not in text for key in ("scope_quote", "scope_start", "span_start", "span_end", "evidence", "mention_id"))
