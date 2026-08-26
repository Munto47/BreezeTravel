from app.constraints.base import RuleContext
from app.constraints.rules._utils import minutes, normalise
from app.itineraries.models import CommitmentKind
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class MealWindowRule:
    rule_id = "meal_window"
    windows = (("lunch", 12 * 60, 13 * 60 + 30), ("dinner", 18 * 60, 20 * 60))

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        checks = []
        for day in context.itinerary.days:
            availability_start, availability_end = self._availability_bounds(day.slots)
            for name, start, end in self.windows:
                if availability_start >= end or availability_end <= start:
                    checks.append(ConstraintCheck(
                        constraint_id=f"system:{name}:{day.day_index}",
                        status=ConstraintStatus.SATISFIED,
                        reason_code="MEAL_WINDOW_NOT_APPLICABLE",
                        message=(
                            f"第 {day.day_index + 1} 天"
                            f"{'午餐' if name == 'lunch' else '晚餐'}时段在到达/返程边界之外"
                        ),
                        day_index=day.day_index,
                        repairable=False,
                    ))
                    continue
                found = False
                for slot in day.slots:
                    category = normalise((slot.place or {}).get("category"))
                    tags = normalise((slot.place or {}).get("tags"))
                    slot_start = minutes(slot.start_time)
                    if slot_start is not None and start <= slot_start < end and (category == "food" or "餐" in tags or "美食" in tags):
                        found = True
                        break
                checks.append(ConstraintCheck(
                    constraint_id=f"system:{name}:{day.day_index}",
                    status=ConstraintStatus.SATISFIED if found else ConstraintStatus.VIOLATED,
                    reason_code="MEAL_WINDOW_FILLED" if found else "MEAL_WINDOW_EMPTY",
                    message=f"第 {day.day_index + 1} 天{'午餐' if name == 'lunch' else '晚餐'}" + ("已安排" if found else "时段没有餐饮地点"),
                    day_index=day.day_index,
                    repairable=not found,
                ))
        return checks

    @staticmethod
    def _availability_bounds(slots) -> tuple[int, int]:
        arrival_times: list[int] = []
        departure_times: list[int] = []
        for slot in slots:
            raw_kind = (slot.place or {}).get("commitment_kind")
            kind = getattr(raw_kind, "value", raw_kind)
            if kind == CommitmentKind.ARRIVAL.value:
                boundary = minutes(slot.end_time)
                if boundary is None:
                    boundary = minutes(slot.start_time)
                if boundary is not None:
                    arrival_times.append(boundary)
            elif kind == CommitmentKind.RETURN_DEPARTURE.value:
                boundary = minutes(slot.start_time)
                if boundary is None:
                    boundary = minutes(slot.end_time)
                if boundary is not None:
                    departure_times.append(boundary)
        return (
            min(arrival_times, default=0),
            min(departure_times, default=24 * 60),
        )
