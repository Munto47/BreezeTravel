from app.constraints.base import RuleContext
from app.constraints.rules._utils import all_slots, category_value
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class DuplicateRule:
    rule_id = "duplicate"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        seen: dict[str, int] = {}
        duplicates: list[tuple[int, str]] = []
        for day, slot in all_slots(context.itinerary):
            # Reusing lodging is intentional.  Under a strict district filter,
            # repeating a real restaurant is also a safer degradation than
            # hallucinating a POI or leaving a meal empty. Attractions remain
            # unique because repetition there materially reduces trip value.
            if category_value(slot) in {"hotel", "food"}:
                continue
            if slot.place_id in seen:
                duplicates.append((day, slot.place_id))
            else:
                seen[slot.place_id] = day
        return [ConstraintCheck(
            constraint_id="system:no_duplicate_places",
            status=ConstraintStatus.VIOLATED if duplicates else ConstraintStatus.SATISFIED,
            reason_code="DUPLICATE_PLACE" if duplicates else "NO_DUPLICATE_PLACE",
            message=f"发现 {len(duplicates)} 个重复地点" if duplicates else "行程没有重复地点",
            day_index=duplicates[0][0] if duplicates else None,
            place_id=duplicates[0][1] if duplicates else None,
            repairable=bool(duplicates),
        )]
