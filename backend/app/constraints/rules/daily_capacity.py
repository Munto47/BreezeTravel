from app.constraints.base import RuleContext
from app.constraints.rules._utils import category_value, find_constraints
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class DailyCapacityRule:
    rule_id = "daily_capacity"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        constraints = find_constraints(context.task_spec, "max_daily_places")
        if not constraints:
            return []
        limit = int(constraints[0].value)
        checks = []
        for day in context.itinerary.days:
            # A hotel is the night anchor, not another sightseeing workload.
            count = sum(
                1 for slot in day.slots
                if category_value(slot) != "hotel"
            )
            checks.append(ConstraintCheck(
                constraint_id=constraints[0].id,
                status=ConstraintStatus.SATISFIED if count <= limit else ConstraintStatus.VIOLATED,
                reason_code="DAILY_CAPACITY_OK" if count <= limit else "DAILY_CAPACITY_EXCEEDED",
                message=f"第 {day.day_index + 1} 天安排 {count}/{limit} 个地点",
                day_index=day.day_index,
                repairable=count > limit,
            ))
        return checks
