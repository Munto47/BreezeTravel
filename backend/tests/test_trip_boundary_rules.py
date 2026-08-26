from __future__ import annotations

from datetime import date, datetime, timezone

from app.audit.system_constraints import with_system_constraints
from app.constraints.base import RuleContext
from app.constraints.rules.daily_hotel import DailyHotelRule
from app.constraints.rules.meal_window import MealWindowRule
from app.itineraries.adapters import revision_to_legacy
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    CommitmentKind,
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
)
from app.schemas.itinerary import DayPlan, Itinerary, TimeSlot
from app.schemas.task_spec import DateRange, HardConstraint, TripTaskSpec
from app.schemas.verification import ConstraintStatus


def _slot(
    place_id: str,
    category: str,
    start: str,
    end: str,
    *,
    commitment_kind: CommitmentKind | None = None,
) -> TimeSlot:
    place = {"place_id": place_id, "name": place_id, "category": category}
    if commitment_kind is not None:
        place["commitment_kind"] = commitment_kind.value
    return TimeSlot(
        place_id=place_id,
        place=place,
        start_time=start,
        end_time=end,
    )


def _itinerary(days: list[list[TimeSlot]]) -> Itinerary:
    return Itinerary(
        itinerary_id="boundary-itinerary",
        thread_id="boundary-thread",
        city="上海",
        days=[
            DayPlan(day_index=index, cluster_id=index, slots=slots)
            for index, slots in enumerate(days)
        ],
        generated_at="2026-08-21T00:00:00+00:00",
    )


def _task(*constraints: HardConstraint) -> TripTaskSpec:
    return TripTaskSpec(
        room_id="boundary-room",
        city="上海",
        date_range=DateRange(start=date(2026, 9, 1), days=3),
        hard_constraints=list(constraints),
    )


def _context(days: list[list[TimeSlot]], *constraints: HardConstraint) -> RuleContext:
    return RuleContext(task_spec=_task(*constraints), itinerary=_itinerary(days))


def test_system_hotel_constraint_only_covers_overnight_days() -> None:
    system_task = with_system_constraints(_task())
    hotel_constraint = next(
        item for item in system_task.hard_constraints if item.type == "daily_hotel"
    )
    context = RuleContext(
        task_spec=system_task,
        itinerary=_itinerary([
            [_slot("hotel-0", "hotel", "20:00", "21:00")],
            [_slot("museum", "attraction", "09:00", "11:00")],
            [_slot("station", "transport", "16:00", "16:30")],
        ]),
    )

    checks = DailyHotelRule().evaluate(context)

    assert hotel_constraint.scope == "overnight_days"
    assert [(check.day_index, check.reason_code) for check in checks] == [
        (0, "DAILY_HOTEL_ANCHORED"),
        (1, "DAILY_HOTEL_MISSING"),
    ]


def test_explicit_legacy_per_day_hotel_constraint_still_checks_departure_day() -> None:
    explicit = HardConstraint(
        id="user:hotel-every-day",
        type="daily_hotel",
        value=True,
        scope="per_day",
    )
    task = with_system_constraints(_task(explicit))

    checks = DailyHotelRule().evaluate(RuleContext(
        task_spec=task,
        itinerary=_itinerary([
            [_slot("hotel-0", "hotel", "20:00", "21:00")],
            [_slot("hotel-1", "hotel", "20:00", "21:00")],
            [_slot("station", "transport", "16:00", "16:30")],
        ]),
    ))

    assert [item for item in task.hard_constraints if item.type == "daily_hotel"] == [explicit]
    assert checks[-1].day_index == 2
    assert checks[-1].reason_code == "DAILY_HOTEL_MISSING"


def test_first_day_without_arrival_commitment_checks_lunch_normally() -> None:
    checks = MealWindowRule().evaluate(_context([
        [_slot("museum", "attraction", "09:00", "11:00")],
    ]))

    lunch = next(check for check in checks if check.constraint_id == "system:lunch:0")
    assert lunch.status == ConstraintStatus.VIOLATED
    assert lunch.reason_code == "MEAL_WINDOW_EMPTY"


def test_arrival_after_lunch_makes_only_lunch_not_applicable() -> None:
    checks = MealWindowRule().evaluate(_context([
        [_slot("hongqiao", "transport", "14:00", "14:10", commitment_kind=CommitmentKind.ARRIVAL)],
    ]))

    by_id = {check.constraint_id: check for check in checks}
    assert by_id["system:lunch:0"].reason_code == "MEAL_WINDOW_NOT_APPLICABLE"
    assert by_id["system:lunch:0"].status == ConstraintStatus.SATISFIED
    assert by_id["system:dinner:0"].reason_code == "MEAL_WINDOW_EMPTY"


def test_arrival_before_lunch_does_not_hide_a_missing_lunch() -> None:
    checks = MealWindowRule().evaluate(_context([
        [_slot("hongqiao", "transport", "10:00", "10:10", commitment_kind=CommitmentKind.ARRIVAL)],
    ]))

    lunch = next(check for check in checks if check.constraint_id == "system:lunch:0")
    assert lunch.reason_code == "MEAL_WINDOW_EMPTY"
    assert lunch.status == ConstraintStatus.VIOLATED


def test_return_before_dinner_makes_only_dinner_not_applicable() -> None:
    checks = MealWindowRule().evaluate(_context([
        [
            _slot("lunch", "food", "12:30", "13:15"),
            _slot(
                "hongqiao",
                "transport",
                "17:00",
                "17:30",
                commitment_kind=CommitmentKind.RETURN_DEPARTURE,
            ),
        ],
    ]))

    by_id = {check.constraint_id: check for check in checks}
    assert by_id["system:lunch:0"].reason_code == "MEAL_WINDOW_FILLED"
    assert by_id["system:dinner:0"].reason_code == "MEAL_WINDOW_NOT_APPLICABLE"
    assert by_id["system:dinner:0"].status == ConstraintStatus.SATISFIED


def test_internal_day_still_checks_both_meal_windows() -> None:
    checks = MealWindowRule().evaluate(_context([
        [_slot("arrival", "transport", "14:00", "14:10", commitment_kind=CommitmentKind.ARRIVAL)],
        [_slot("museum", "attraction", "09:00", "11:00")],
        [
            _slot(
                "station",
                "transport",
                "17:00",
                "17:30",
                commitment_kind=CommitmentKind.RETURN_DEPARTURE,
            ),
        ],
    ]))

    internal = [check for check in checks if check.day_index == 1]
    assert [check.reason_code for check in internal] == [
        "MEAL_WINDOW_EMPTY",
        "MEAL_WINDOW_EMPTY",
    ]


def test_revision_adapter_exposes_commitment_to_meal_rule() -> None:
    content = ItineraryRevisionContent(
        itinerary_id="adapter-boundary",
        workspace_id="adapter-boundary",
        revision=1,
        source_type=RevisionSource.IMPORT,
        city="上海",
        date_range=TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
        days=[
            ItineraryDay(day_index=0, stops=[ItineraryStop(
                stop_id="arrival-stop",
                place_id="hongqiao",
                day_index=0,
                order_index=0,
                start_time="14:00",
                end_time="14:10",
                commitment_kind=CommitmentKind.ARRIVAL,
                category="transport",
            )]),
            ItineraryDay(day_index=1, stops=[]),
        ],
        created_by="boundary-test",
        created_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    legacy = revision_to_legacy(
        with_content_hash(content),
        thread_id="adapter-boundary",
        preserve_unknown_times=True,
    )

    assert legacy.days[0].slots[0].place["commitment_kind"] == "ARRIVAL"
    checks = MealWindowRule().evaluate(RuleContext(task_spec=_task(), itinerary=legacy))
    lunch = next(check for check in checks if check.constraint_id == "system:lunch:0")
    assert lunch.reason_code == "MEAL_WINDOW_NOT_APPLICABLE"
