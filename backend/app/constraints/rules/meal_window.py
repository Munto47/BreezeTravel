from app.constraints.base import RuleContext
from app.constraints.rules._utils import minutes, normalise
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class MealWindowRule:
    rule_id = "meal_window"
    windows = (("lunch", 12 * 60, 13 * 60 + 30), ("dinner", 18 * 60, 20 * 60))

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        checks = []
        last_day_index = max((day.day_index for day in context.itinerary.days), default=0)
        for day in context.itinerary.days:
            for name, start, end in self.windows:
                if len(context.itinerary.days) > 1 and day.day_index == 0 and name == "lunch":
                    continue
                if len(context.itinerary.days) > 1 and day.day_index == last_day_index and name == "dinner":
                    continue
                if len(context.itinerary.days) > 1 and day.day_index == last_day_index and name == "lunch":
                    start = 11 * 60
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
