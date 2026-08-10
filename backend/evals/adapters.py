from __future__ import annotations

import time

from app.agents.nodes.task_parser import parse_task_spec
from app.agents.planner.graph import run_planner
from app.agents.routing_policy import plan_simple_tools, plan_tools
from app.constraints.verifier import ItineraryVerifier
from evals.schema import EvalCase
from app.schemas.itinerary import DayPlan, Itinerary, TimeSlot, TransportLeg, WeatherInfo
from app.schemas.place import Coordinates, Place, PlaceCategory, PlaceSource
from app.schemas.task_spec import BudgetSpec, DateRange, HardConstraint, Travelers, TripTaskSpec


def router_adapter(case: EvalCase):
    started = time.perf_counter()
    plan = plan_tools(case.input["query"]) or plan_simple_tools(case.input["query"])
    actual = list(plan.tools) if plan else []
    return {
        "passed": set(actual) == set(case.expected["tool_set"]),
        "tool_set": actual,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "reproduce": f"python -m scripts.run_local_eval_suite --case {case.id}",
    }


def task_parse_adapter(case: EvalCase):
    started = time.perf_counter()
    parsed = parse_task_spec(case.input["text"], room_id="eval-room")
    spec = parsed.task_spec
    expected = case.expected
    actual = {
        "city": spec.city,
        "days": spec.date_range.days,
        "budget": spec.budget.amount if spec.budget else None,
        "budget_scope": spec.budget.scope if spec.budget else None,
        "must": [item.value for item in spec.must_include],
        "exclude": [item.value for item in spec.exclude],
        "constraints": [item.type for item in spec.hard_constraints],
        "clarification": spec.missing_fields,
        "conflict": bool(spec.conflicts),
    }
    passed = all(
        (
            actual.get(key) == value
            if key not in {"constraint", "constraints", "must", "exclude"}
            else value in actual["constraints"]
            if key == "constraint"
            else set(value) <= set(actual["constraints"])
            if key == "constraints"
            else value in actual[key]
        )
        for key, value in expected.items()
    )
    return {
        "passed": passed, **actual,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "reproduce": f"python -m scripts.run_local_eval_suite --case {case.id}",
    }


def _place(pid, name, category="attraction", price=30, hours="08:00-22:00", tags=None):
    return Place(
        place_id=pid, name=name, category=PlaceCategory(category), address="市中心",
        coords=Coordinates(lng=120.1 + int(pid[-1]) * 0.001, lat=30.2), city="杭州",
        source=PlaceSource.AMAP_POI, amap_price=price, opening_hours=hours,
        estimated_duration=60, tags=tags or [],
    )


def _verifier_fixture(profile: str):
    attraction = _place("p1", "城市公园", tags=["户外"])
    lunch = _place("p2", "午餐馆", "food")
    dinner = _place("p3", "晚餐馆", "food")
    places = [attraction, lunch, dinner]
    slots = [
        TimeSlot(place_id="p1", place=attraction.model_dump(mode="json"), start_time="09:00", end_time="11:00", transport=TransportLeg(duration_mins=20, distance_km=2)),
        TimeSlot(place_id="p2", place=lunch.model_dump(mode="json"), start_time="12:00", end_time="13:00", transport=TransportLeg(duration_mins=20, distance_km=2)),
        TimeSlot(place_id="p3", place=dinner.model_dump(mode="json"), start_time="18:00", end_time="19:00"),
    ]
    weather = WeatherInfo(condition="晴", temp_high=25, temp_low=18, suggestion="适宜")
    hard = []
    budget = None
    exclude = []
    if profile == "missing_price":
        slots[0].place["amap_price"] = None
        budget = BudgetSpec(amount=500, scope="total")
    elif profile == "missing_hours":
        slots[0].place["opening_hours"] = None
    elif profile == "duplicate":
        slots.append(slots[0].model_copy(deep=True))
    elif profile == "excluded":
        from app.schemas.task_spec import NamedRequirement
        exclude = [NamedRequirement(value="城市公园")]
    elif profile == "capacity":
        hard = [HardConstraint(id="cap", type="max_daily_places", operator="lte", value=2, unit="place")]
    elif profile == "time_overlap":
        slots[1].start_time = "10:00"
    elif profile in {"travel_missing", "travel_exceeded"}:
        hard = [HardConstraint(id="travel", type="max_daily_travel_minutes", operator="lte", value=60, unit="minute")]
        if profile == "travel_missing":
            slots[0].transport = None
        else:
            slots[0].transport.duration_mins = 100
    elif profile in {"rain_unknown", "rain_outdoor"}:
        hard = [HardConstraint(id="rain", type="avoid_outdoor_on_rain", operator="eq", value=True)]
        weather = None if profile == "rain_unknown" else WeatherInfo(condition="大雨", temp_high=22, temp_low=18, suggestion="室内")
    elif profile == "budget_ok":
        budget = BudgetSpec(amount=500, scope="total")
    task = TripTaskSpec(
        room_id="eval", city="杭州", date_range=DateRange(days=1), travelers=Travelers(adults=1),
        budget=budget, hard_constraints=hard, exclude=exclude,
    )
    plan = Itinerary(
        itinerary_id="eval-itin", thread_id="eval-thread", city="杭州",
        days=[DayPlan(day_index=0, cluster_id=0, slots=slots, weather_summary=weather)],
        generated_at="2026-08-09T00:00:00Z",
    )
    return task, plan, places


def verifier_adapter(case: EvalCase):
    started = time.perf_counter()
    task, plan, places = _verifier_fixture(case.input["fixture_profile"])
    report = ItineraryVerifier().verify(task, plan, places=places)
    return {
        "passed": report.overall_status.value == case.expected["status"],
        "status": report.overall_status.value,
        "checks": len(report.checks),
        "unknown_honesty": not any(item.status.value == "SATISFIED" and "MISSING" in item.reason_code for item in report.checks),
        "latency_ms": (time.perf_counter() - started) * 1000,
        "reproduce": f"python -m scripts.run_local_eval_suite --case {case.id}",
    }


async def end_to_end_adapter(case: EvalCase):
    started = time.perf_counter()
    prompt = case.input["turns"][-1]["content"]
    parsed = parse_task_spec(prompt, room_id=f"eval-{case.id}", default_city=case.city, default_days=3)
    candidates = [
        _place("p1", "城市博物馆", tags=["室内", "文化"]), _place("p2", "城市公园", tags=["户外"]),
        _place("p3", "历史街区"), _place("p4", "午餐馆A", "food"),
        _place("p5", "晚餐馆A", "food"), _place("p6", "午餐馆B", "food"),
        _place("p7", "晚餐馆B", "food"), _place("p8", "午餐馆C", "food"),
        _place("p9", "晚餐馆C", "food"), _place("p0", "中心酒店", "hotel", price=180),
    ]
    result = await run_planner(
        candidates, max(1, parsed.task_spec.date_range.days), f"thread-{case.id}",
        task_spec=parsed.task_spec,
    )
    report = result.verification_report
    unknown_honesty = bool(report) and not any(item.status.value == "SATISFIED" and "MISSING" in item.reason_code for item in report.checks)
    has_violation = bool(report) and any(item.status.value == "VIOLATED" for item in report.checks)
    violations = [
        {
            "constraint_id": item.constraint_id,
            "reason_code": item.reason_code,
            "day_index": item.day_index,
        }
        for item in (report.checks if report else [])
        if item.status.value == "VIOLATED"
    ]
    return {
        "passed": bool(result.itinerary.days) and bool(report) and unknown_honesty and not has_violation,
        "days": len(result.itinerary.days),
        "verification_status": report.overall_status.value if report else None,
        "violations": violations,
        "unknown_honesty": unknown_honesty,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "reproduce": f"python -m scripts.run_local_eval_suite --case {case.id}",
    }
