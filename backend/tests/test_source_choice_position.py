"""Use the saved source boundary without projecting away pending cards/hotels."""
import pytest

from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.models import ChoiceSelectCommand, TripDayView
from tests.test_choice_group_selection import build_choice_result, select_command


async def current_with_hidden_stops():
    result = (await build_choice_result()).public_result
    first, last = result.days[0].activities
    pending = first.model_copy(update={"activity_token": "fixed-hidden-pending-token-0001",
        "name": "待核景点", "status": "NEEDS_CONFIRMATION"})
    hotel = first.model_copy(update={"activity_token": "fixed-whole-trip-hotel-token-0001",
        "name": "全程酒店", "category": "住宿", "lodging_event": "OVERNIGHT", "lodging_scope": "WHOLE_TRIP"})
    result.days[0].activities = [first, pending, hotel, last]
    result.days.append(TripDayView(label="Day 2", activities=[first.model_copy(
        update={"activity_token": "fixed-same-name-day2-token-0001"})]))
    assert all(item.insertion_position == 1 for item in result.days[0].alternatives)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("position,expected", [("omitted", 1), (None, 1), (1, 1), (3, 3)])
async def test_default_uses_source_boundary_before_pending_and_hotel_but_explicit_position_is_preserved(position, expected):
    current = await current_with_hidden_stops()
    payload = select_command(current).model_dump()
    if position == "omitted":
        payload.pop("position")
    else:
        payload["position"] = position
    before = current.model_dump()
    updated = apply_public_command(current, ChoiceSelectCommand.model_validate(payload)).result
    expected_names = [card.name for card in current.days[0].activities]
    expected_names.insert(expected, current.days[0].alternatives[0].name)
    assert [card.name for card in updated.days[0].activities] == expected_names
    assert updated.days[0].activities[expected].status == "NEEDS_CONFIRMATION"
    assert [card.name for card in updated.days[1].activities] == [current.days[1].activities[0].name]
    assert current.model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("positions", [(None, None), (1, None), (1, 2), (5, 5)])
async def test_missing_conflicting_or_out_of_range_source_position_refuses_default(positions):
    current = await current_with_hidden_stops()
    for item, position in zip(current.days[0].alternatives, positions):
        item.insertion_position = position
    command = select_command(current).model_copy(update={"position": None})
    before = current.model_dump()
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(current, command)
    assert current.model_dump() == before


@pytest.mark.asyncio
async def test_default_source_position_never_authorizes_an_unsupported_group():
    current = await current_with_hidden_stops()
    current.days[0].alternatives[1].choice_group_selectable = False
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(current, select_command(current).model_copy(update={"position": None}))


@pytest.mark.asyncio
async def test_explicit_position_is_available_when_source_boundary_is_unknown():
    current = await current_with_hidden_stops()
    for item in current.days[0].alternatives:
        item.insertion_position = None
    result = apply_public_command(current, select_command(current).model_copy(update={"position": 2})).result
    assert result.days[0].activities[2].name == current.days[0].alternatives[0].name


@pytest.mark.asyncio
@pytest.mark.parametrize("count,allowed", [(159, True), (160, False)])
async def test_default_source_position_obeys_current_card_capacity(count, allowed):
    current = await current_with_hidden_stops()
    template = current.days[0].activities[0]
    current.days = [current.days[0]]
    current.days[0].activities = [template.model_copy(update={"activity_token": f"source-position-capacity-{i:06d}"})
                                  for i in range(count)]
    command = select_command(current).model_copy(update={"position": None})
    if allowed:
        assert len(apply_public_command(current, command).result.days[0].activities) == 160
    else:
        with pytest.raises(CommandTargetChangedError):
            apply_public_command(current, command)
        assert len(current.days[0].activities) == 160
