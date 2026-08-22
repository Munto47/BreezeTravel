from __future__ import annotations

from datetime import date

import pytest

from app.constraints.base import RuleContext
from app.constraints.rules.opening_hours import OpeningHoursRule
from app.schemas.itinerary import DayPlan, Itinerary, TimeSlot
from app.schemas.task_spec import DateRange, TripTaskSpec


def _evaluate(opening_hours: str, start: str, end: str):
    place_id = "boundary-place"
    itinerary = Itinerary(
        itinerary_id="opening-boundary",
        thread_id="opening-boundary",
        city="杭州",
        days=[
            DayPlan(
                day_index=0,
                date="2026-09-01",
                cluster_id=0,
                slots=[
                    TimeSlot(
                        place_id=place_id,
                        place={
                            "place_id": place_id,
                            "name": "营业边界地点",
                            "category": "attraction",
                        },
                        start_time=start,
                        end_time=end,
                    )
                ],
            )
        ],
        generated_at="2026-08-21T00:00:00+00:00",
    )
    task = TripTaskSpec(
        room_id="opening-boundary",
        city="杭州",
        date_range=DateRange(start=date(2026, 9, 1), days=1),
    )
    return OpeningHoursRule().evaluate(
        RuleContext(
            task_spec=task,
            itinerary=itinerary,
            place_meta={place_id: {"opening_hours": opening_hours}},
        )
    )[0]


@pytest.mark.parametrize("raw", ["24小时营业", "24 小时开放", "全天营业", "全天开放", "全天"])
def test_common_all_day_provider_values_cover_a_scheduled_visit(raw):
    assert _evaluate(raw, "10:30", "11:30").reason_code == "WITHIN_OPENING_HOURS"


def test_cross_midnight_window_covers_late_evening_and_early_morning():
    assert _evaluate("07:00-00:30", "23:30", "00:15").reason_code == "WITHIN_OPENING_HOURS"
    assert _evaluate("07:00-00:30", "00:05", "00:20").reason_code == "WITHIN_OPENING_HOURS"


def test_cross_midnight_window_still_rejects_closed_gap():
    assert _evaluate("07:00-00:30", "01:00", "02:00").reason_code == "OUTSIDE_OPENING_HOURS"


def test_unrecognized_marketing_text_remains_unknown():
    assert _evaluate("营业时间以现场公告为准", "10:00", "11:00").reason_code == "OPENING_HOURS_UNPARSEABLE"
