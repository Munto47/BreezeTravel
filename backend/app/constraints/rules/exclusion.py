from app.constraints.base import RuleContext
from app.constraints.rules._utils import all_slots, normalise, place_text
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class ExclusionRule:
    rule_id = "exclusion"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        slots = [(day, slot.place_id, place_text(slot)) for day, slot in all_slots(context.itinerary)]
        checks = []
        for index, requirement in enumerate(context.task_spec.exclude):
            target = normalise(requirement.value)
            match = next(((day, pid) for day, pid, text in slots if target and target in text), None)
            checks.append(ConstraintCheck(
                constraint_id=f"exclude:{index}:{target}",
                status=ConstraintStatus.VIOLATED if match else ConstraintStatus.SATISFIED,
                reason_code="EXCLUDED_PRESENT" if match else "EXCLUSION_RESPECTED",
                message=f"行程包含排除项 {requirement.value}" if match else f"未安排排除项 {requirement.value}",
                day_index=match[0] if match else None,
                place_id=match[1] if match else None,
                evidence_refs=[f"poi:{match[1]}"] if match else [],
                repairable=bool(match),
            ))
        return checks
