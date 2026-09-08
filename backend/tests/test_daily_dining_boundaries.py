"""Synthetic source-position boundaries, independent of restaurant recall quality."""
from types import SimpleNamespace as NS

import pytest

from app.trip_understanding.daily_dining import build_daily_meals, meal_context
from tests.test_daily_dining import card, day_context


@pytest.mark.asyncio
@pytest.mark.parametrize("name,meal_role", [("楼外楼孤山路店", None), ("合成午餐饭店", "LUNCH")])
async def test_named_source_lunch_waiting_for_identity_does_not_offer_a_second_lunch(name, meal_role):
    # First row reproduces the public field shape of the observed Hangzhou failure.
    lunch = card(name, 1, category="餐饮", status="NEEDS_CONFIRMATION")
    lunch.meal_role = meal_role
    day, stops = day_context([card("上午景点", 0), lunch, card("下午景点", 2)])

    async def forbidden(**_):
        pytest.fail("Confirm the original named meal before searching for another lunch")

    rows = await build_daily_meals(NS(days=[day]), NS(stops=stops), search=forbidden)
    assert rows[0]["status"] == "NEEDS_CONFIRMATION"
    assert rows[0]["existing_activity_token"] == lunch.activity_token
    assert rows[0]["meal_role"] == "LUNCH"
    assert rows[0]["candidates"] == []


@pytest.mark.asyncio
async def test_unconfirmed_explicit_lunch_neighbors_do_not_move_lunch_to_evening():
    morning = card("未确认上午景点", 0, status="NEEDS_CONFIRMATION")
    afternoon = card("未确认午后景点", 1, status="NEEDS_CONFIRMATION")
    day, stops = day_context([morning, afternoon, card("已确认傍晚景点", 2)])
    day.meal_slots = [NS(meal_role="LUNCH", after_activity_token=morning.activity_token,
                         before_activity_token=afternoon.activity_token)]
    async def forbidden(**_):
        pytest.fail("An unrelated evening visit cannot anchor an explicit lunch gap")
    result = await build_daily_meals(NS(days=[day]), NS(stops=stops), search=forbidden)
    assert result[0]["status"] == "NEEDS_CONFIRMATION"
    assert not result[0].get("after_activity_token")
    assert not result[0]["candidates"]


def test_leading_legacy_unnamed_lunch_stays_before_first_visit():
    day, stops = day_context([card("午餐：牛肉面", 0, category="餐饮", status="NEEDS_CONFIRMATION"),
                              card("午后景点", 1), card("傍晚景点", 2)])
    view, anchor, following = meal_context(day, stops)
    assert view["insert_before"] and view["meal_role"] == "LUNCH"
    assert anchor.name == "午后景点" and following is None


def test_fallback_after_unknown_first_stop_inserts_before_the_confirmed_later_stop():
    day, stops = day_context([card("首站未确认", 0, status="NEEDS_CONFIRMATION"), card("末站已确认", 1)])
    view, anchor, following = meal_context(day, stops)
    assert anchor.name == "末站已确认" and view["insert_before"]
    assert following is None


def test_source_lunch_gap_is_not_suppressed_by_an_untimed_unclassified_food_visit():
    snack = card("合成咖啡店", 0, category="餐饮")
    visit = card("午后景点", 1)
    day, stops = day_context([snack, visit])
    day.meal_slots = [NS(meal_role="LUNCH", after_activity_token=None,
                         before_activity_token=visit.activity_token)]
    view, anchor, _ = meal_context(day, stops)
    assert view["status"] != "EXISTING" and view["insert_before"]
    assert anchor.name == "午后景点"


def test_whole_trip_hotel_cannot_anchor_lunch_even_if_present_in_input_stops():
    hotel, visit = card("全程酒店", 0, category="住宿"), card("午后景点", 1)
    day, stops = day_context([hotel, visit])
    stops[0] = stops[0].model_copy(update={"lodging_scope": "WHOLE_TRIP", "lodging_event": "OVERNIGHT"})
    day.meal_slots = [NS(meal_role="LUNCH", after_activity_token=hotel.activity_token,
                         before_activity_token=visit.activity_token)]
    view, anchor, _ = meal_context(day, stops)
    assert view["insert_before"] and anchor.name == "午后景点"


def test_explicit_cross_city_gap_does_not_claim_a_local_next_station():
    day, stops = day_context([card("北京上午景点", 0), card("上海傍晚景点", 1)])
    stops[1] = stops[1].model_copy(update={"city": "上海"})
    day.meal_slots = [NS(meal_role="LUNCH", after_activity_token=stops[0].activity_token,
                         before_activity_token=stops[1].activity_token)]
    view, anchor, following = meal_context(day, stops)
    assert anchor.city == "北京" and following is None and view["next_name"] is None
