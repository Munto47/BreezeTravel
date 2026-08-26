from app.constraints.base import RuleContext
from app.constraints.rules._utils import all_slots
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class BudgetRule:
    rule_id = "budget"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        budget = context.task_spec.budget
        if budget is None:
            return []
        prices = []
        missing = []
        for _, slot in all_slots(context.itinerary):
            place = slot.place or {}
            category = str(place.get("category", ""))
            if category == "transport" or (category == "hotel" and not budget.include_hotel):
                continue
            price = place.get("amap_price")
            if price is None:
                missing.append(slot.place_id)
            else:
                prices.append(float(price))
        if missing:
            return [ConstraintCheck(
                constraint_id="budget",
                status=ConstraintStatus.UNKNOWN,
                reason_code="PRICE_DATA_MISSING",
                message=f"{len(missing)} 个地点缺少价格，无法确认预算",
                evidence_refs=[f"poi:{item}" for item in missing],
            )]
        amount = sum(prices)
        people = context.task_spec.travelers.total
        days = max(1, context.task_spec.date_range.days)
        if budget.scope in {"total", "per_day"}:
            amount *= people
        allowed = budget.amount * (days if budget.scope == "per_day" else people if budget.scope == "per_person" else people * days if budget.scope == "per_person_per_day" else 1)
        ok = amount <= allowed
        return [ConstraintCheck(
            constraint_id="budget",
            status=ConstraintStatus.SATISFIED if ok else ConstraintStatus.VIOLATED,
            reason_code="BUDGET_WITHIN_LIMIT" if ok else "BUDGET_EXCEEDED",
            message=f"可验证费用 {amount:.0f}/{allowed:.0f} {budget.currency}",
            repairable=not ok,
        )]
