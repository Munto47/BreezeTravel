from app.constraints.base import RuleContext
from app.constraints.location import place_matches_district
from app.constraints.rules._utils import find_constraints
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class TripAreaRule:
    rule_id = "trip_area"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        constraints = find_constraints(context.task_spec, "trip_area")
        if not constraints:
            return []
        constraint = constraints[0]
        mismatches = [
            (day.day_index, slot)
            for day in context.itinerary.days
            for slot in day.slots
            if not place_matches_district(slot.place or {}, str(constraint.value))
        ]
        return [ConstraintCheck(
            constraint_id=constraint.id,
            status=ConstraintStatus.VIOLATED if mismatches else ConstraintStatus.SATISFIED,
            reason_code="TRIP_AREA_MISMATCH" if mismatches else "TRIP_AREA_MATCH",
            message=(
                f"有地点不在 {constraint.value}"
                if mismatches else f"全部地点均在 {constraint.value}"
            ),
            day_index=mismatches[0][0] if mismatches else None,
            place_id=mismatches[0][1].place_id if mismatches else None,
            repairable=bool(mismatches),
        )]
