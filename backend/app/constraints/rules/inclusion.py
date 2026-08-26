from app.constraints.base import RuleContext
from app.constraints.rules._utils import all_slots, normalise, place_text
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class InclusionRule:
    rule_id = "inclusion"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        texts = [(day, slot.place_id, place_text(slot)) for day, slot in all_slots(context.itinerary)]
        checks = []
        for index, requirement in enumerate(context.task_spec.must_include):
            target = normalise(requirement.value)
            match = next(((day, pid) for day, pid, text in texts if target and target in text), None)
            checks.append(ConstraintCheck(
                constraint_id=f"must_include:{index}:{target}",
                status=ConstraintStatus.SATISFIED if match else ConstraintStatus.VIOLATED,
                reason_code="MUST_INCLUDE_PRESENT" if match else "MUST_INCLUDE_MISSING",
                message=f"已包含 {requirement.value}" if match else f"缺少必选项 {requirement.value}",
                day_index=match[0] if match else None,
                place_id=match[1] if match else None,
                evidence_refs=[f"poi:{match[1]}"] if match else [],
                repairable=not bool(context.task_spec.conflicts),
            ))
        return checks
