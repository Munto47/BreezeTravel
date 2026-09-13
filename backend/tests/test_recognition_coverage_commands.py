"""Visible counts follow edits; unprocessed source work cannot become complete."""
from types import SimpleNamespace

import pytest

from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.models import ActivityDeleteCommand, MealSlotView, PlaceConfirmCommand, TripRecognitionCoverage, UndoCommand


@pytest.mark.asyncio
async def test_confirmation_delete_and_undo_recount_without_erasing_pending_source_work():
    original = (await build_demo_pipeline().run(DEMO_SOURCE_TEXT)).public_result
    cards = [card for day in original.days for card in day.activities]
    for card in cards:
        card.status = "READY"
    cards[0].status = "NEEDS_CONFIRMATION"
    count = len(cards)
    original.status = "PARTIAL_RESULT"
    original.days[0].meal_slots = [MealSlotView(meal_role="LUNCH", after_activity_token=cards[0].activity_token)]
    original.coverage = TripRecognitionCoverage(recognized_place_count=count, confirmed_place_count=count - 1,
        unresolved_place_count=1, unclassified_mention_count=2, unprocessed_count=3, complete=False)
    verified = SimpleNamespace(name=cards[0].name, category=cards[0].category, city="北京", area_or_address="已核验地址")
    confirmed = apply_public_command(original, PlaceConfirmCommand(command_type="PLACE_CONFIRM",
        activity_token=cards[0].activity_token, candidate_token="c" * 40), confirmed_place=verified).result
    assert confirmed.coverage.confirmed_place_count == count
    assert confirmed.coverage.unresolved_place_count == 0
    assert confirmed.coverage.unclassified_mention_count == 2 and confirmed.coverage.unprocessed_count == 3
    assert confirmed.status == "PARTIAL_RESULT" and confirmed.coverage.complete is False
    assert confirmed.map.status == "NEEDS_UPDATE"
    assert confirmed.days[0].meal_slots[0].after_activity_token == confirmed.days[0].activities[0].activity_token
    assert original.coverage.confirmed_place_count == count - 1
    deleted = apply_public_command(confirmed, ActivityDeleteCommand(command_type="ACTIVITY_DELETE",
        activity_token=confirmed.days[0].activities[0].activity_token)).result
    assert deleted.coverage.recognized_place_count == count - 1
    assert deleted.coverage.confirmed_place_count == count - 1
    assert deleted.days[0].meal_slots[0].after_activity_token is None
    restored = apply_public_command(deleted, UndoCommand(command_type="UNDO"), undo_result=confirmed).result
    assert restored.coverage.recognized_place_count == count
    assert restored.coverage.confirmed_place_count == count
    assert restored.coverage.unclassified_mention_count == 2 and restored.coverage.complete is False
    assert restored.map.status == "NEEDS_UPDATE"
    assert restored.days[0].meal_slots[0].after_activity_token == restored.days[0].activities[0].activity_token


@pytest.mark.asyncio
async def test_confirming_the_only_unmatched_place_can_complete_an_assessed_source():
    original = (await build_demo_pipeline().run(DEMO_SOURCE_TEXT)).public_result
    cards = [card for day in original.days for card in day.activities]
    for card in cards:
        card.status = "READY"
    cards[0].status = "NEEDS_CONFIRMATION"
    original.coverage = TripRecognitionCoverage(recognized_place_count=len(cards), confirmed_place_count=len(cards) - 1,
        unresolved_place_count=1)
    confirmed = apply_public_command(original, PlaceConfirmCommand(command_type="PLACE_CONFIRM",
        activity_token=cards[0].activity_token, candidate_token="c" * 40),
        confirmed_place=SimpleNamespace(name=cards[0].name, category=cards[0].category, city="北京", area_or_address="已核验地址")).result
    assert confirmed.status == "READY" and confirmed.coverage.complete is True
    assert confirmed.coverage.unresolved_place_count == 0
