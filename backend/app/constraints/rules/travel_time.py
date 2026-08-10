from app.constraints.base import RuleContext
from app.constraints.rules._utils import find_constraints
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class TravelTimeRule:
    rule_id = "travel_time"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        constraints = find_constraints(context.task_spec, "max_daily_travel_minutes")
        if not constraints:
            return []
        constraint = constraints[0]
        limit = int(constraint.value)
        checks = []
        for day in context.itinerary.days:
            legs = [slot.transport for slot in day.slots[:-1] if slot.transport is not None]
            if len(day.slots) > 1 and len(legs) < len(day.slots) - 1:
                status, code, message = ConstraintStatus.UNKNOWN, "TRAVEL_TIME_MISSING", "缺少完整交通时长，无法验证"
            else:
                total = sum(max(0, leg.duration_mins) for leg in legs)
                ok = total <= limit
                status = ConstraintStatus.SATISFIED if ok else ConstraintStatus.VIOLATED
                code = "TRAVEL_TIME_WITHIN_LIMIT" if ok else "TRAVEL_TIME_EXCEEDED"
                message = f"交通时间 {total}/{limit} 分钟"
            checks.append(ConstraintCheck(
                constraint_id=constraint.id,
                status=status,
                reason_code=code,
                message=f"第 {day.day_index + 1} 天{message}",
                day_index=day.day_index,
                repairable=status == ConstraintStatus.VIOLATED,
            ))
        return checks
