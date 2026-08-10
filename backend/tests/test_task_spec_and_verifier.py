from __future__ import annotations

from copy import deepcopy

import pytest

from app.agents.nodes.task_parser import parse_task_spec
from app.agents.planner.repair_controller import TargetedRepairController
from app.constraints.verifier import ItineraryVerifier
from app.schemas.itinerary import DayPlan, Itinerary, TimeSlot, TransportLeg, WeatherInfo
from app.schemas.place import Coordinates, Place, PlaceCategory, PlaceSource
from app.schemas.task_spec import (
    BudgetSpec, DateRange, HardConstraint, NamedRequirement, Travelers, TripTaskSpec,
)
from app.schemas.verification import ConstraintCheck, ConstraintStatus
from app.services.planning_hash import compute_planning_input_hash, is_verification_stale


def place(pid: str, name: str, category: str = "attraction", price: float | None = 30, opening: str | None = "08:00-22:00", tags=None):
    return Place(
        place_id=pid,
        name=name,
        category=PlaceCategory(category),
        address="西湖区",
        coords=Coordinates(lng=120.1, lat=30.2),
        city="杭州",
        source=PlaceSource.AMAP_POI,
        amap_price=price,
        opening_hours=opening,
        tags=tags or [],
    )


def slot(item: Place, start: str, end: str, *, transport: int | None = None):
    return TimeSlot(
        place_id=item.place_id,
        place=item.model_dump(mode="json"),
        start_time=start,
        end_time=end,
        transport=TransportLeg(duration_mins=transport, distance_km=2) if transport is not None else None,
    )


def itinerary(days: list[DayPlan]) -> Itinerary:
    return Itinerary(
        itinerary_id="itin-1",
        thread_id="thread-1",
        city="杭州",
        days=days,
        generated_at="2026-08-09T00:00:00Z",
    )


def spec(**updates) -> TripTaskSpec:
    base = TripTaskSpec(
        room_id="room-1",
        city="杭州",
        date_range=DateRange(days=1),
        travelers=Travelers(adults=1),
    )
    return base.model_copy(update=updates)


class TestTaskParser:
    def test_extracts_core_fields_and_constraints(self):
        result = parse_task_spec(
            "杭州三日游，3人含1个孩子，预算3000元，必须去西湖，不去高强度爬山，每日交通不超过120分钟",
            room_id="room-1",
        )
        parsed = result.task_spec
        assert parsed.city == "杭州"
        assert parsed.date_range.days == 3
        assert parsed.travelers.total == 3
        assert parsed.travelers.children == 1
        assert parsed.budget and parsed.budget.amount == 3000
        assert parsed.hard_constraints[0].value == 120
        assert not result.needs_clarification

    def test_missing_critical_fields_are_not_guessed(self):
        result = parse_task_spec("想安排一次轻松旅行", room_id="room-1")
        assert result.needs_clarification
        assert result.clarification_fields == ["city", "date_range.days"]

    def test_memory_is_only_soft_preference(self):
        parsed = parse_task_spec(
            "杭州两日游", room_id="room-1", memory_preferences=["喜欢博物馆"]
        ).task_spec
        memory = [item for item in parsed.soft_preferences if item.source.value == "memory"]
        assert memory and memory[0].weight < 0.5
        assert all(item.source.value != "memory" for item in parsed.hard_constraints)

    def test_collaboration_and_degradation_phrases_are_not_place_names(self):
        majority = parse_task_spec(
            "杭州三日游，保留多数投票地点且每天最多4个地点", room_id="room-1"
        ).task_spec
        assert not majority.must_include
        assert any(item.type == "preserve_majority_voted" for item in majority.hard_constraints)
        degraded = parse_task_spec(
            "成都两日游，RAG失败时保留实时地点并明确降级", room_id="room-1"
        ).task_spec
        assert not degraded.must_include
        assert any(item.type == "explicit_partial_result_degradation" for item in degraded.soft_preferences)

    def test_conflicting_must_and_exclude_requires_clarification(self):
        parsed = TripTaskSpec(
            room_id="room-1", city="杭州", date_range=DateRange(days=2),
            must_include=[NamedRequirement(value="西湖")],
            exclude=[NamedRequirement(value="西湖")],
        )
        assert parsed.needs_clarification
        assert "must_include conflicts" in parsed.conflicts[0]


class TestPlanningHash:
    def test_hash_is_order_independent_for_places(self):
        task = spec()
        places = [place("p1", "西湖"), place("p2", "灵隐寺")]
        assert compute_planning_input_hash(task, places, 1) == compute_planning_input_hash(task, reversed(places), 1)

    @pytest.mark.parametrize("mutation", ["revision", "version", "vote", "pin", "price"])
    def test_relevant_collaboration_change_invalidates_hash(self, mutation):
        task = spec()
        raw = [place("p1", "西湖").model_dump(mode="json")]
        original = compute_planning_input_hash(task, raw, 1)
        version = 1
        if mutation == "revision":
            task = task.model_copy(update={"task_revision": 2})
        elif mutation == "version":
            version = 2
        elif mutation == "vote":
            raw[0]["votedBy"] = ["user-1"]
        elif mutation == "pin":
            raw[0]["isPinned"] = True
        elif mutation == "price":
            raw[0]["amap_price"] = 99
        changed = compute_planning_input_hash(task, raw, version)
        assert is_verification_stale(changed, original)


class TestVerifierThreeState:
    def clean_fixture(self):
        attraction = place("p1", "西湖", transport=None) if False else place("p1", "西湖")
        lunch = place("p2", "午餐馆", "food")
        dinner = place("p3", "晚餐馆", "food")
        plan = itinerary([DayPlan(
            day_index=0, cluster_id=0,
            slots=[
                slot(attraction, "09:00", "11:00", transport=20),
                slot(lunch, "12:00", "13:00", transport=20),
                slot(dinner, "18:00", "19:00"),
            ],
            weather_summary=WeatherInfo(condition="晴", temp_high=28, temp_low=20, suggestion="适宜"),
        )])
        task = spec(
            must_include=[NamedRequirement(value="西湖")],
            exclude=[NamedRequirement(value="爬山")],
            hard_constraints=[
                HardConstraint(id="travel", type="max_daily_travel_minutes", operator="lte", value=60, unit="minute", scope="per_day"),
                HardConstraint(id="capacity", type="max_daily_places", operator="lte", value=4, unit="place", scope="per_day"),
            ],
            budget=BudgetSpec(amount=500, scope="total"),
        )
        return task, plan, [attraction, lunch, dinner]

    def test_clean_deterministic_fixture_is_satisfied(self):
        task, plan, places = self.clean_fixture()
        report = ItineraryVerifier().verify(task, plan, places=places)
        assert report.overall_status == ConstraintStatus.SATISFIED
        assert all(item.status == ConstraintStatus.SATISFIED for item in report.checks)

    def test_majority_vote_is_unknown_without_complete_snapshot(self):
        task, plan, places = self.clean_fixture()
        task = task.model_copy(update={
            "hard_constraints": task.hard_constraints + [
                HardConstraint(
                    id="c_preserve_majority_vote", type="preserve_majority_voted",
                    operator="eq", value=True, scope="trip",
                )
            ]
        })
        report = ItineraryVerifier().verify(task, plan, places=places)
        check = next(item for item in report.checks if item.constraint_id == "c_preserve_majority_vote")
        assert check.status == ConstraintStatus.UNKNOWN
        assert check.reason_code == "VOTE_SNAPSHOT_MISSING"

    @pytest.mark.parametrize(
        ("field", "reason"),
        [("price", "PRICE_DATA_MISSING"), ("opening", "OPENING_HOURS_MISSING"), ("transport", "TRAVEL_TIME_MISSING")],
    )
    def test_missing_evidence_is_unknown_not_passed(self, field, reason):
        task, plan, places = self.clean_fixture()
        if field == "price":
            for day in plan.days:
                day.slots[0].place["amap_price"] = None
        elif field == "opening":
            plan.days[0].slots[0].place["opening_hours"] = None
        else:
            plan.days[0].slots[0].transport = None
        report = ItineraryVerifier().verify(task, plan, places=places)
        matching = [item for item in report.checks if item.reason_code == reason]
        assert matching and matching[0].status == ConstraintStatus.UNKNOWN
        assert report.overall_status == ConstraintStatus.UNKNOWN

    @pytest.mark.parametrize(
        ("mutation", "reason"),
        [
            ("duplicate", "DUPLICATE_PLACE"),
            ("exclude", "EXCLUDED_PRESENT"),
            ("capacity", "DAILY_CAPACITY_EXCEEDED"),
            ("time", "TIME_CHAIN_BROKEN"),
            ("travel", "TRAVEL_TIME_EXCEEDED"),
            ("opening", "OUTSIDE_OPENING_HOURS"),
            ("budget", "BUDGET_EXCEEDED"),
        ],
    )
    def test_known_bad_data_is_violated(self, mutation, reason):
        task, plan, places = self.clean_fixture()
        if mutation == "duplicate":
            plan.days[0].slots.append(deepcopy(plan.days[0].slots[0]))
        elif mutation == "exclude":
            task = task.model_copy(update={"exclude": [NamedRequirement(value="西湖")]})
        elif mutation == "capacity":
            task = task.model_copy(update={"hard_constraints": [HardConstraint(id="capacity", type="max_daily_places", operator="lte", value=2, unit="place")]})
        elif mutation == "time":
            plan.days[0].slots[1].start_time = "10:00"
        elif mutation == "travel":
            plan.days[0].slots[0].transport.duration_mins = 100
        elif mutation == "opening":
            plan.days[0].slots[0].place["opening_hours"] = "12:00-14:00"
        elif mutation == "budget":
            task = task.model_copy(update={"budget": BudgetSpec(amount=10, scope="total")})
        report = ItineraryVerifier().verify(task, plan, places=places)
        assert any(item.reason_code == reason and item.status == ConstraintStatus.VIOLATED for item in report.checks)
        assert report.overall_status == ConstraintStatus.VIOLATED


class TestTargetedRepair:
    def test_duplicate_repair_does_not_change_unrelated_day(self):
        p1 = place("p1", "西湖")
        p2 = place("p2", "博物馆")
        plan = itinerary([
            DayPlan(day_index=0, cluster_id=0, slots=[slot(p1, "09:00", "11:00"), slot(p1, "14:00", "16:00")]),
            DayPlan(day_index=1, cluster_id=1, slots=[slot(p2, "09:00", "11:00")]),
        ])
        untouched = plan.days[1].model_dump()
        check = ConstraintCheck(
            constraint_id="system:no_duplicate_places", status=ConstraintStatus.VIOLATED,
            reason_code="DUPLICATE_PLACE", message="duplicate", day_index=0, place_id="p1", repairable=True,
        )
        repaired, repair_plan = TargetedRepairController().repair_once(plan, spec(), [check], [p1, p2])
        assert len(repaired.days[0].slots) == 1
        assert repaired.days[1].model_dump() == untouched
        assert repair_plan.targeted_days == {0}

    def test_unknown_is_never_modified(self):
        p1 = place("p1", "西湖")
        plan = itinerary([DayPlan(day_index=0, cluster_id=0, slots=[slot(p1, "09:00", "11:00")])])
        check = ConstraintCheck(
            constraint_id="budget", status=ConstraintStatus.UNKNOWN,
            reason_code="PRICE_DATA_MISSING", message="unknown", repairable=False,
        )
        repaired, repair_plan = TargetedRepairController().repair_once(plan, spec(), [check], [p1])
        assert repaired == plan
        assert not repair_plan.actions

    def test_same_violation_signature_is_stable(self):
        check = ConstraintCheck(
            constraint_id="x", status=ConstraintStatus.VIOLATED,
            reason_code="Y", message="bad", day_index=0, repairable=True,
        )
        controller = TargetedRepairController()
        assert controller.signature([check]) == controller.signature([check])
