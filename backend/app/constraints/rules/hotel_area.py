from app.constraints.base import RuleContext
from app.constraints.rules._utils import category_value, find_constraints, normalise
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class HotelAreaRule:
    rule_id = "hotel_area"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        constraints = find_constraints(context.task_spec, "hotel_area")
        if not constraints:
            return []
        constraint = constraints[0]
        target = normalise(constraint.value)
        hotels = [slot for day in context.itinerary.days for slot in day.slots if category_value(slot) == "hotel"]
        if not hotels:
            return [ConstraintCheck(
                constraint_id=constraint.id,
                status=ConstraintStatus.UNKNOWN,
                reason_code="HOTEL_NOT_SELECTED",
                message="尚未选择酒店，无法验证住宿区域",
            )]
        matched = [slot for slot in hotels if target in normalise(f"{(slot.place or {}).get('address', '')} {(slot.place or {}).get('district', '')}")]
        ok = len(matched) == len(hotels)
        return [ConstraintCheck(
            constraint_id=constraint.id,
            status=ConstraintStatus.SATISFIED if ok else ConstraintStatus.VIOLATED,
            reason_code="HOTEL_AREA_MATCH" if ok else "HOTEL_AREA_MISMATCH",
            message="酒店区域符合要求" if ok else f"有酒店不在 {constraint.value}",
            place_id=None if ok else next(slot.place_id for slot in hotels if slot not in matched),
            repairable=not ok,
        )]
