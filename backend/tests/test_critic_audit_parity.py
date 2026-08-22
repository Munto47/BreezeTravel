from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.agents.planner.nodes.critic_v2 import (
    _check_daily_food_cap,
    _check_buffer_deficit,
    _check_hotel_day_end,
    _check_meal_slot_filled,
    _check_no_backtoback_l2,
    _check_open_hours,
    _check_weather_mismatch,
    _check_zero_food_day,
)
from app.constraints.base import RuleContext
from app.constraints.rules.daily_hotel import DailyHotelRule
from app.constraints.rules.meal_window import MealWindowRule
from app.constraints.rules.opening_hours import OpeningHoursRule
from app.constraints.rules.pacing import PacingRule
from app.constraints.rules.time_chain import TimeChainRule
from app.constraints.rules.weather import WeatherRule
from app.schemas.itinerary import DayPlan, Itinerary, TimeSlot, WeatherInfo
from app.schemas.preferences import WeatherDay
from app.schemas.task_spec import DateRange, HardConstraint, TripTaskSpec


DATASET = Path(__file__).parent.parent / "eval_data" / "auditor" / "critic_parity_v1.json"


def _cases():
    return json.loads(DATASET.read_text(encoding="utf-8"))


def _critic_flags(slots: list[dict]) -> set[str]:
    critic_slots = [
        {
            "place_id": slot["place_id"],
            "place": {
                "place_id": slot["place_id"],
                "name": slot["name"],
                "category": slot["category"],
            },
            "category_l1": "餐饮" if slot["category"] == "food" else "景点",
            "category_l2": slot["category_l2"],
            "start_time": slot["start_time"],
            "end_time": slot["end_time"],
        }
        for slot in slots
    ]
    flags: set[str] = set()
    if _check_zero_food_day(critic_slots, 0):
        flags.add("missing_food")
    if _check_daily_food_cap(critic_slots, 0):
        flags.add("food_cap")
    if _check_no_backtoback_l2(critic_slots, 0):
        flags.add("adjacent_category")
    return flags


def _audit_flags(slots: list[dict]) -> set[str]:
    itinerary = Itinerary(
        itinerary_id="critic-parity",
        thread_id="critic-parity",
        city="北京",
        days=[DayPlan(
            day_index=0,
            date="2026-09-01",
            cluster_id=0,
            slots=[
                TimeSlot(
                    place_id=slot["place_id"],
                    place={
                        "place_id": slot["place_id"],
                        "name": slot["name"],
                        "category": slot["category"],
                        "category_l2": slot["category_l2"],
                    },
                    start_time=slot["start_time"],
                    end_time=slot["end_time"],
                )
                for slot in slots
            ],
        )],
        generated_at="2026-08-20T00:00:00+00:00",
    )
    task = TripTaskSpec(
        room_id="critic-parity",
        city="北京",
        date_range=DateRange(start=date(2026, 9, 1), days=1),
        hard_constraints=[HardConstraint(
            id="system:pacing",
            type="system_pacing",
            value=True,
            scope="per_day",
        )],
    )
    reasons = {
        check.reason_code
        for check in PacingRule().evaluate(RuleContext(task_spec=task, itinerary=itinerary))
    }
    flags: set[str] = set()
    if "DAILY_FOOD_MISSING" in reasons:
        flags.add("missing_food")
    if "DAILY_FOOD_CAP_EXCEEDED" in reasons:
        flags.add("food_cap")
    if "ADJACENT_CATEGORY_REPEATED" in reasons:
        flags.add("adjacent_category")
    return flags


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["case_id"])
def test_critic_only_pacing_rules_have_authoritative_registry_parity(case):
    assert _audit_flags(case["slots"]) == _critic_flags(case["slots"])


def _context(slots: list[TimeSlot], *, weather: WeatherInfo | None = None) -> RuleContext:
    itinerary = Itinerary(
        itinerary_id="critic-full-parity",
        thread_id="critic-full-parity",
        city="北京",
        days=[DayPlan(
            day_index=0,
            date="2026-09-01",
            cluster_id=0,
            slots=slots,
            weather_summary=weather,
        )],
        generated_at="2026-08-20T00:00:00+00:00",
    )
    return RuleContext(
        task_spec=TripTaskSpec(
            room_id="critic-full-parity",
            city="北京",
            date_range=DateRange(start=date(2026, 9, 1), days=1),
            hard_constraints=[
                HardConstraint(
                    id="system:daily_hotel",
                    type="daily_hotel",
                    value=True,
                    scope="per_day",
                ),
                HardConstraint(
                    id="system:weather_exposure",
                    type="avoid_outdoor_on_rain",
                    value=True,
                    scope="per_day",
                ),
            ],
        ),
        itinerary=itinerary,
    )


def _slot(place_id: str, category: str, start: str, end: str, *, tags: list[str] | None = None):
    return TimeSlot(
        place_id=place_id,
        place={
            "place_id": place_id,
            "name": place_id,
            "category": category,
            "address": "测试地址",
            "coords": {"lng": 116.4, "lat": 39.9},
            "city": "北京",
            "tags": tags or [],
        },
        start_time=start,
        end_time=end,
    )


def _critic_slot(slot: TimeSlot, *, category_l1: str, category_l2: str) -> dict:
    return {
        "place_id": slot.place_id,
        "place": slot.place,
        "category_l1": category_l1,
        "category_l2": category_l2,
        "start_time": slot.start_time,
        "end_time": slot.end_time,
    }


def test_opening_hours_violation_has_registry_parity():
    slot = _slot("museum", "attraction", "09:00", "11:00")
    old = _check_open_hours(
        _critic_slot(slot, category_l1="景点", category_l2="博物馆"),
        0,
        {"museum": {"open_hours_json": {"tue": [[13, 18]]}}},
        1,
    )
    context = _context([slot])
    context = RuleContext(
        task_spec=context.task_spec,
        itinerary=context.itinerary,
        place_meta={"museum": {"opening_hours": "13:00-18:00"}},
    )
    checks = OpeningHoursRule().evaluate(context)
    assert old is not None
    assert any(check.reason_code == "OUTSIDE_OPENING_HOURS" for check in checks)


def test_meal_window_violation_has_registry_parity():
    attraction = _slot("museum", "attraction", "09:00", "11:00")
    critic_slots = [_critic_slot(attraction, category_l1="景点", category_l2="博物馆")]
    assert _check_meal_slot_filled(critic_slots, 0)
    assert any(
        check.reason_code == "MEAL_WINDOW_EMPTY"
        for check in MealWindowRule().evaluate(_context([attraction]))
    )


def test_time_chain_violation_has_registry_parity():
    first = _slot("a", "attraction", "09:00", "11:00")
    second = _slot("b", "attraction", "10:30", "12:00")
    critic_slots = [
        _critic_slot(first, category_l1="景点", category_l2="博物馆"),
        _critic_slot(second, category_l1="景点", category_l2="街区"),
    ]
    assert _check_buffer_deficit(critic_slots, 0)
    assert any(
        check.reason_code == "TIME_CHAIN_BROKEN"
        for check in TimeChainRule().evaluate(_context([first, second]))
    )


def test_weather_exposure_violation_has_registry_parity():
    first = _slot("park-a", "attraction", "09:00", "10:00", tags=["公园", "户外"])
    second = _slot("park-b", "attraction", "11:00", "12:00", tags=["景区", "户外"])
    critic_slots = [
        _critic_slot(first, category_l1="景点", category_l2="公园"),
        _critic_slot(second, category_l1="景点", category_l2="景区"),
    ]
    old_weather = WeatherDay(date="2026-09-01", condition="rainy", precip_mm=8)
    assert _check_weather_mismatch(critic_slots, 0, old_weather) is not None
    checks = WeatherRule().evaluate(_context(
        [first, second],
        weather=WeatherInfo(
            condition="rainy",
            temp_high=25,
            temp_low=20,
            suggestion="优先室内",
            precip_mm=8,
        ),
    ))
    assert any(check.reason_code == "RAIN_OUTDOOR_CONFLICT" for check in checks)


def test_daily_hotel_violation_has_registry_parity_without_user_constraint():
    attraction = _slot("museum", "attraction", "09:00", "11:00")
    old = _check_hotel_day_end(
        [_critic_slot(attraction, category_l1="景点", category_l2="博物馆")],
        0,
    )
    assert old is not None
    assert any(
        check.reason_code == "DAILY_HOTEL_MISSING"
        for check in DailyHotelRule().evaluate(_context([attraction]))
    )
