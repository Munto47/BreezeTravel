from app.constraints.base import RuleContext
from app.constraints.rules._utils import category_value, find_constraints
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class DailyHotelRule:
    rule_id = "daily_hotel"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        if not find_constraints(context.task_spec, "daily_hotel"):
            return []
        checks: list[ConstraintCheck] = []
        for day in context.itinerary.days:
            last = day.slots[-1] if day.slots else None
            category = category_value(last) if last else ""
            ok = last is not None and category == "hotel"
            checks.append(ConstraintCheck(
                constraint_id=f"c_daily_hotel:{day.day_index}",
                status=ConstraintStatus.SATISFIED if ok else ConstraintStatus.VIOLATED,
                reason_code="DAILY_HOTEL_ANCHORED" if ok else "DAILY_HOTEL_MISSING",
                message=(
                    f"第 {day.day_index + 1} 天以酒店收尾"
                    if ok else f"第 {day.day_index + 1} 天末尾缺少酒店"
                ),
                day_index=day.day_index,
                place_id=last.place_id if last and not ok else None,
                repairable=False,
            ))
        return checks
