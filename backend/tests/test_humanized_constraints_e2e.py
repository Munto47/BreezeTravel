from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import HumanMessage

from app.agents.nodes.amap_search import _load_mock_places
from app.agents.nodes.router import _has_sufficient_place_evidence
from app.agents.nodes.task_parser import parse_task_spec
from app.agents.planner.nodes.scheduler_v2 import _select_stay_hotel, run as run_scheduler
from app.agents.planner.templates import select_template
from app.constraints.location import (
    extract_district_constraint,
    extract_district_from_messages,
    place_is_human_suitable,
    place_matches_district,
)
from app.constraints.verifier import ItineraryVerifier
from app.schemas.itinerary import Itinerary
from app.schemas.place import Coordinates, Place, PlaceCategory, PlaceSource
from app.schemas.preferences import GroupPreferences
from app.tools.runtime import SearchPlacesArgs


DATASET = Path(__file__).parents[1] / "eval_data" / "humanized_journeys" / "cases.json"


def _place(pid: str, name: str, category: PlaceCategory, district: str, lng: float, lat: float) -> Place:
    return Place(
        place_id=pid,
        name=name,
        category=category,
        address=f"{district}{name}地址",
        coords=Coordinates(lng=lng, lat=lat),
        city="上海",
        district=district,
        source=PlaceSource.AMAP_POI,
        tags=["亲子"] if category == PlaceCategory.ATTRACTION else [],
    )


def test_humanized_dataset_is_reviewable_and_covers_core_failures():
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert len(cases) >= 6
    assert all(case["persona"] and len(case["turns"]) >= 1 for case in cases)
    assert any(case["expected"].get("latest_constraint_wins") for case in cases)
    assert sum(bool(case["expected"].get("hotel_every_day")) for case in cases) >= 3


def test_latest_district_correction_wins_across_turns():
    messages = [
        HumanMessage(content="先看看浦东新区两日游"),
        HumanMessage(content="改一下，不要浦东新区，最终只在闵行区活动"),
    ]
    assert extract_district_from_messages(messages) == "闵行区"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("上海三日游，最终只在浦东新区活动", "浦东新区"),
        ("杭州两日游，限定西湖区", "西湖区"),
        ("深圳亲子游，只推荐南山区", "南山区"),
    ],
)
def test_district_constraint_is_city_agnostic(text: str, expected: str):
    assert extract_district_constraint(text) == expected


def test_minhang_mock_recommendations_never_spill_to_other_districts():
    places = _load_mock_places("上海", "只在闵行区推荐景点、美食和酒店")
    assert len(places) >= 6
    assert {place.district for place in places} == {"闵行区"}
    assert {place.category for place in places} >= {
        PlaceCategory.ATTRACTION, PlaceCategory.FOOD, PlaceCategory.HOTEL,
    }


def test_visible_other_district_branch_name_is_rejected():
    contradictory = _place(
        "CONFLICT", "玩木工坊(浦东店)", PlaceCategory.ATTRACTION,
        "闵行区", 121.36, 31.16,
    )
    assert place_matches_district(contradictory, "闵行区") is False


def test_retail_store_cannot_masquerade_as_child_attraction():
    retail = _place(
        "RETAIL", "孩子王虹桥天地优选店", PlaceCategory.ATTRACTION,
        "闵行区", 121.36, 31.16,
    )
    assert place_is_human_suitable(retail) is False


def test_fixed_family_hotel_prefers_rating_then_centrality():
    activity = _place("CENTER", "活动中心", PlaceCategory.ATTRACTION, "闵行区", 121.38, 31.15)
    low_quality = _place("H-LOW", "普通旅馆", PlaceCategory.HOTEL, "闵行区", 121.381, 31.15)
    low_quality.amap_rating = 3.5
    high_quality = _place("H-HIGH", "亲子酒店", PlaceCategory.HOTEL, "闵行区", 121.39, 31.15)
    high_quality.amap_rating = 4.8
    assert _select_stay_hotel([activity], [low_quality, high_quality]).place_id == "H-HIGH"


def test_task_parser_turns_minhang_into_machine_checkable_hard_constraint():
    parsed = parse_task_spec(
        "上海三日游，只在闵行区活动，酒店不要每天换",
        room_id="humanized-room",
    ).task_spec
    by_type = {constraint.type: constraint for constraint in parsed.hard_constraints}
    assert by_type["trip_area"].value == "闵行区"
    assert by_type["daily_hotel"].value is True


def test_task_parser_preserves_family_senior_and_low_walking_context():
    parsed = parse_task_spec(
        "上海三日游，2位成人带1个孩子和1位长辈，只在闵行区，步行别太累",
        room_id="family-room",
    ).task_spec
    assert parsed.travelers.children == 1
    assert parsed.travelers.seniors == 1
    preference_types = {item.type for item in parsed.soft_preferences}
    assert {"family_friendly", "senior_friendly", "low_walking"} <= preference_types


def test_tool_runtime_accepts_explicit_district_argument():
    parsed = SearchPlacesArgs.model_validate({
        "query": "亲子景点",
        "city": "上海",
        "district": "闵行区",
    })
    assert parsed.district == "闵行区"


def test_router_stops_after_enough_diverse_grounded_places():
    places = [
        _place(
            f"ENOUGH-{index}",
            f"地点{index}",
            PlaceCategory.ATTRACTION if index < 4 else PlaceCategory.FOOD,
            "闵行区",
            121.35 + index * 0.001,
            31.15,
        )
        for index in range(8)
    ]
    assert _has_sufficient_place_evidence(places) is True
    assert _has_sufficient_place_evidence(places[:7]) is False


def test_family_trip_without_edge_times_keeps_all_three_days_useful():
    prefs = GroupPreferences(style="family", has_kids=True, trip_city="上海", trip_days=3)
    assert [select_template(day, 3, prefs).template_id for day in range(3)] == [
        "T_FAMILY_LIGHT", "T_FAMILY_LIGHT", "T_FAMILY_LIGHT",
    ]
    prefs.arrival_time = "16:00"
    prefs.departure_time = "14:00"
    assert select_template(0, 3, prefs).template_id == "T_ARRIVAL"
    assert select_template(2, 3, prefs).template_id == "T_DEPARTURE"


def test_three_day_route_ends_at_same_hotel_every_night():
    hotel = _place("H-MH", "闵行固定酒店", PlaceCategory.HOTEL, "闵行区", 121.38, 31.15)
    orderings = {
        day: [
            _place(f"A{day}", f"闵行景点{day}", PlaceCategory.ATTRACTION, "闵行区", 121.35 + day * 0.01, 31.15),
            _place(f"F{day}", f"闵行餐厅{day}", PlaceCategory.FOOD, "闵行区", 121.36 + day * 0.01, 31.16),
        ]
        for day in range(3)
    }

    async def execute():
        with patch(
            "app.agents.planner.nodes.scheduler_v2._load_place_meta",
            AsyncMock(return_value={}),
        ):
            return await run_scheduler({
                "orderings": orderings,
                "hotels_pool": [hotel],
                "trip_days": 3,
                "trace": [],
                "backup_pool": [],
                "vote_counts": {},
                "weather_forecast": {},
            })

    result = asyncio.run(execute())
    day_plans = result["day_plans"]
    assert len(day_plans) == 3
    assert [day.slots[-1].place_id for day in day_plans] == [hotel.place_id] * 3
    assert all(day.slots[-1].place["category"] == "hotel" for day in day_plans)
    assert "办理入住" in day_plans[0].slots[-1].place["tags"]
    assert all("返回酒店" in day.slots[-1].place["tags"] for day in day_plans[1:])
    assert day_plans[0].slots[-1].start_time <= "21:30"

    from app.agents.planner.nodes.critic_v2 import _check_hotel_day_end
    for day_index, day_state in result["day_states"].items():
        assert _check_hotel_day_end(day_state["slots"], day_index) is None

    task = parse_task_spec(
        "上海三日游，只在闵行区活动",
        room_id="humanized-room",
    ).task_spec
    itinerary = Itinerary(
        itinerary_id="humanized-itinerary",
        thread_id="humanized-thread",
        city="上海",
        days=day_plans,
        generated_at="2026-08-10T00:00:00Z",
    )
    report = ItineraryVerifier().verify(
        task,
        itinerary,
        places=[place for values in orderings.values() for place in values] + [hotel],
    )
    assert not any(check.reason_code == "TRIP_AREA_MISMATCH" for check in report.checks)
    hotel_checks = [check for check in report.checks if check.reason_code == "DAILY_HOTEL_ANCHORED"]
    assert len(hotel_checks) == 3


def test_verifier_rejects_out_of_area_place_and_missing_nightly_hotel():
    task = parse_task_spec("上海一日游，只在闵行区活动", room_id="bad-room").task_spec
    outside = _place("OUT", "外滩", PlaceCategory.ATTRACTION, "黄浦区", 121.49, 31.24)
    from app.schemas.itinerary import DayPlan, TimeSlot
    itinerary = Itinerary(
        itinerary_id="bad-itinerary",
        thread_id="bad-thread",
        city="上海",
        days=[DayPlan(
            day_index=0,
            cluster_id=0,
            slots=[TimeSlot(
                place_id=outside.place_id,
                place=outside.model_dump(mode="json"),
                start_time="09:00",
                end_time="11:00",
            )],
        )],
        generated_at="2026-08-10T00:00:00Z",
    )
    report = ItineraryVerifier().verify(task, itinerary, places=[outside])
    reasons = {check.reason_code for check in report.checks}
    assert "TRIP_AREA_MISMATCH" in reasons
    assert "DAILY_HOTEL_MISSING" in reasons


def test_meal_reserve_can_fill_a_day_whose_cluster_has_no_restaurant():
    hotel = _place("H-RESERVE", "固定酒店", PlaceCategory.HOTEL, "闵行区", 121.38, 31.15)
    foods = [
        _place(f"RF{i}", f"餐厅{i}", PlaceCategory.FOOD, "闵行区", 121.36 + i * 0.001, 31.16)
        for i in range(4)
    ]
    orderings = {
        0: [_place("RA0", "公园0", PlaceCategory.ATTRACTION, "闵行区", 121.35, 31.15), *foods[:3]],
        1: [_place("RA1", "博物馆1", PlaceCategory.ATTRACTION, "闵行区", 121.36, 31.15)],
        2: [_place("RA2", "公园2", PlaceCategory.ATTRACTION, "闵行区", 121.37, 31.15), foods[3]],
    }

    async def execute():
        with patch(
            "app.agents.planner.nodes.scheduler_v2._load_place_meta",
            AsyncMock(return_value={}),
        ):
            return await run_scheduler({
                "orderings": orderings,
                "hotels_pool": [hotel],
                "trip_days": 3,
                "trace": [],
                "backup_pool": [],
                "vote_counts": {},
                "weather_forecast": {},
            })

    result = asyncio.run(execute())
    from app.agents.planner.nodes.critic_v2 import _check_buffer_deficit, _check_meal_slot_filled
    middle = result["day_states"][1]
    assert _check_meal_slot_filled(middle["slots"], 1, middle["template_id"]) == []
    assert _check_buffer_deficit(middle["slots"], 1) == []


def test_attraction_reserve_prevents_food_only_days_and_keeps_hotels():
    hotel = _place("H-ACT", "高评分亲子酒店", PlaceCategory.HOTEL, "闵行区", 121.38, 31.15)
    hotel.amap_rating = 4.8
    attractions = [
        _place(f"AA{i}", f"亲子景点{i}", PlaceCategory.ATTRACTION, "闵行区", 121.35 + i * 0.002, 31.15)
        for i in range(3)
    ]
    foods = [
        _place(f"AF{i}", f"家庭餐厅{i}", PlaceCategory.FOOD, "闵行区", 121.36 + i * 0.002, 31.16)
        for i in range(4)
    ]
    orderings = {
        0: [*attractions, *foods[:2]],
        1: [foods[2]],
        2: [foods[3]],
    }

    async def execute():
        with patch(
            "app.agents.planner.nodes.scheduler_v2._load_place_meta",
            AsyncMock(return_value={}),
        ):
            return await run_scheduler({
                "orderings": orderings,
                "hotels_pool": [hotel],
                "trip_days": 3,
                "trace": [],
                "backup_pool": [],
                "vote_counts": {},
                "weather_forecast": {},
                "user_prefs": GroupPreferences(style="family", has_kids=True),
            })

    result = asyncio.run(execute())
    assert all(
        any(slot.place["category"] == "attraction" for slot in day.slots)
        for day in result["day_plans"]
    )
    assert [day.slots[-1].place["category"] for day in result["day_plans"]] == ["hotel"] * 3
    assert all(
        sum(slot.place["category"] == "food" for slot in day.slots) == 2
        for day in result["day_plans"]
    )
