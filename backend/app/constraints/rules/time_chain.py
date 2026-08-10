from app.constraints.base import RuleContext
from app.constraints.rules._utils import minutes
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class TimeChainRule:
    rule_id = "time_chain"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        checks = []
        for day in context.itinerary.days:
            invalid = False
            unknown = False
            previous_end = None
            for slot in day.slots:
                start, end = minutes(slot.start_time), minutes(slot.end_time)
                if start is None or end is None:
                    unknown = True
                    continue
                if end <= start or (previous_end is not None and start < previous_end):
                    invalid = True
                previous_end = end
            status = ConstraintStatus.VIOLATED if invalid else (ConstraintStatus.UNKNOWN if unknown else ConstraintStatus.SATISFIED)
            checks.append(ConstraintCheck(
                constraint_id=f"system:time_chain:{day.day_index}",
                status=status,
                reason_code={ConstraintStatus.VIOLATED: "TIME_CHAIN_BROKEN", ConstraintStatus.UNKNOWN: "TIME_DATA_INVALID", ConstraintStatus.SATISFIED: "TIME_CHAIN_VALID"}[status],
                message=f"第 {day.day_index + 1} 天时间链" + ("存在重叠或倒序" if invalid else "缺少可解析时间" if unknown else "连续有效"),
                day_index=day.day_index,
                repairable=invalid,
            ))
        return checks
