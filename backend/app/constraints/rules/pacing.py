from __future__ import annotations

from app.constraints.base import RuleContext
from app.constraints.rules._utils import category_value, find_constraints, normalise
from app.schemas.verification import ConstraintCheck, ConstraintStatus


_SECONDARY_CATEGORY_TOKENS = (
    "博物馆",
    "美术馆",
    "古迹",
    "寺庙",
    "公园",
    "景区",
    "街区",
    "商圈",
    "火锅",
    "餐厅",
    "小吃",
    "咖啡",
    "酒吧",
    "酒店",
)


def _secondary_category(place: dict) -> str:
    """Return the deterministic category used by the retired planner Critic.

    Planner-created POIs may carry the scheduler's explicit category_l2.  For
    imported itineraries the same value is derived from provider-backed name
    and tags.  Unknown categories stay empty rather than being guessed as a
    satisfied constraint.
    """

    explicit = normalise(place.get("category_l2"))
    if explicit:
        return explicit
    text = normalise(f"{place.get('name', '')} {' '.join(place.get('tags') or [])}")
    return next((token for token in _SECONDARY_CATEGORY_TOKENS if token in text), "")


class PacingRule:
    """Authoritative migration of the three Critic-only pacing checks."""

    rule_id = "pacing"
    daily_food_limit = 3

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        if not find_constraints(context.task_spec, "system_pacing"):
            return []
        checks: list[ConstraintCheck] = []
        for day in context.itinerary.days:
            slots = [slot for slot in day.slots if slot.place_id]
            food_slots = [slot for slot in slots if category_value(slot) == "food"]

            food_count = len(food_slots)
            checks.append(ConstraintCheck(
                constraint_id=f"system:daily_food:{day.day_index}",
                status=(
                    ConstraintStatus.SATISFIED
                    if 1 <= food_count <= self.daily_food_limit
                    else ConstraintStatus.VIOLATED
                ),
                reason_code=(
                    "DAILY_FOOD_MISSING"
                    if food_count == 0
                    else "DAILY_FOOD_CAP_EXCEEDED"
                    if food_count > self.daily_food_limit
                    else "DAILY_FOOD_BALANCED"
                ),
                message=(
                    f"第 {day.day_index + 1} 天没有餐饮安排"
                    if food_count == 0
                    else f"第 {day.day_index + 1} 天餐饮地点 {food_count} 个，超过上限 {self.daily_food_limit} 个"
                    if food_count > self.daily_food_limit
                    else f"第 {day.day_index + 1} 天餐饮数量在允许范围内"
                ),
                day_index=day.day_index,
                repairable=food_count == 0 or food_count > self.daily_food_limit,
            ))

            repeated_pairs: list[tuple[str, str, str]] = []
            for previous, current in zip(slots, slots[1:]):
                previous_l2 = _secondary_category(previous.place or {})
                current_l2 = _secondary_category(current.place or {})
                if previous_l2 and previous_l2 == current_l2:
                    repeated_pairs.append((previous.place_id, current.place_id, current_l2))

            if repeated_pairs:
                for previous_id, current_id, category_l2 in repeated_pairs:
                    checks.append(ConstraintCheck(
                        constraint_id=f"system:adjacent_category:{day.day_index}:{previous_id}:{current_id}",
                        status=ConstraintStatus.VIOLATED,
                        reason_code="ADJACENT_CATEGORY_REPEATED",
                        message=f"第 {day.day_index + 1} 天相邻地点连续为 {category_l2}，节奏重复",
                        day_index=day.day_index,
                        place_id=current_id,
                        repairable=True,
                    ))
            else:
                checks.append(ConstraintCheck(
                    constraint_id=f"system:adjacent_category:{day.day_index}",
                    status=ConstraintStatus.SATISFIED,
                    reason_code="ADJACENT_CATEGORY_DIVERSE",
                    message=f"第 {day.day_index + 1} 天相邻地点没有可确认的同类重复",
                    day_index=day.day_index,
                ))
        return checks
