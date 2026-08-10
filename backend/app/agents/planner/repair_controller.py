"""Bounded, targeted itinerary repair.

Only deterministic violations marked ``repairable`` are modified. UNKNOWN
checks never enter this controller, and protected/must-include places are not
silently removed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable

from app.schemas.itinerary import Itinerary
from app.schemas.place import Place
from app.schemas.task_spec import TripTaskSpec
from app.schemas.verification import ConstraintCheck, ConstraintStatus


@dataclass
class RepairPlan:
    violation_signature: str
    targeted_days: set[int] = field(default_factory=set)
    actions: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


class TargetedRepairController:
    max_rounds = 2

    @staticmethod
    def signature(checks: Iterable[ConstraintCheck]) -> str:
        payload = sorted(
            (item.constraint_id, item.reason_code, item.day_index, item.place_id)
            for item in checks
            if item.status == ConstraintStatus.VIOLATED
        )
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def repair_once(
        self,
        itinerary: Itinerary,
        task_spec: TripTaskSpec,
        checks: Iterable[ConstraintCheck],
        candidates: Iterable[Place],
    ) -> tuple[Itinerary, RepairPlan]:
        violations = [item for item in checks if item.status == ConstraintStatus.VIOLATED]
        plan = RepairPlan(violation_signature=self.signature(violations))
        repaired = itinerary.model_copy(deep=True)
        must_terms = {"".join(item.value.lower().split()) for item in task_spec.must_include}

        def protected(slot) -> bool:
            place = slot.place or {}
            text = "".join(f"{place.get('name', '')}{place.get('tags', '')}".lower().split())
            return bool(place.get("isPinned") or place.get("is_pinned") or any(term in text for term in must_terms))

        def is_meal_anchor(slot) -> bool:
            place = slot.place or {}
            category = getattr(place.get("category"), "value", place.get("category"))
            try:
                hour, minute = map(int, slot.start_time.split(":"))
            except (AttributeError, TypeError, ValueError):
                return False
            start = hour * 60 + minute
            return category == "food" and (12 * 60 <= start < 13 * 60 + 30 or 18 * 60 <= start < 20 * 60)

        for violation in violations:
            if not violation.repairable:
                plan.unresolved.append(f"{violation.constraint_id}:{violation.reason_code}:not_repairable")
                continue
            day = next((item for item in repaired.days if item.day_index == violation.day_index), None)
            if violation.day_index is not None:
                plan.targeted_days.add(violation.day_index)

            if violation.reason_code == "DUPLICATE_PLACE" and violation.place_id:
                found = False
                for current_day in repaired.days:
                    kept = []
                    for slot in current_day.slots:
                        if slot.place_id == violation.place_id:
                            if found and not protected(slot):
                                plan.actions.append(f"remove_duplicate:{current_day.day_index}:{slot.place_id}")
                                continue
                            found = True
                        kept.append(slot)
                    current_day.slots = kept
                continue

            if violation.reason_code in {"EXCLUDED_PRESENT", "HOTEL_AREA_MISMATCH", "RAIN_OUTDOOR_CONFLICT"} and day and violation.place_id:
                before = len(day.slots)
                day.slots = [slot for slot in day.slots if slot.place_id != violation.place_id or protected(slot)]
                if len(day.slots) < before:
                    plan.actions.append(f"remove_target:{day.day_index}:{violation.place_id}")
                else:
                    plan.unresolved.append(f"{violation.constraint_id}:target_locked")
                continue

            if violation.reason_code == "DAILY_CAPACITY_EXCEEDED" and day:
                constraint = next((item for item in task_spec.hard_constraints if item.id == violation.constraint_id), None)
                limit = int(constraint.value) if constraint else len(day.slots)
                removable = sorted(
                    (slot for slot in reversed(day.slots) if not protected(slot)),
                    key=lambda slot: is_meal_anchor(slot),
                )
                while len(day.slots) > limit and removable:
                    slot = removable.pop(0)
                    day.slots.remove(slot)
                    plan.actions.append(f"trim_capacity:{day.day_index}:{slot.place_id}")
                if len(day.slots) > limit:
                    plan.unresolved.append(f"{violation.constraint_id}:locked_capacity")
                continue

            if violation.reason_code == "TIME_CHAIN_BROKEN" and day:
                cursor = 9 * 60
                for slot in day.slots:
                    try:
                        start_h, start_m = map(int, slot.start_time.split(":"))
                        end_h, end_m = map(int, slot.end_time.split(":"))
                    except (AttributeError, TypeError, ValueError):
                        continue
                    duration = max(30, end_h * 60 + end_m - (start_h * 60 + start_m))
                    start = max(cursor, start_h * 60 + start_m)
                    end = start + duration
                    slot.start_time = f"{start // 60:02d}:{start % 60:02d}"
                    slot.end_time = f"{end // 60:02d}:{end % 60:02d}"
                    cursor = end + 15
                plan.actions.append(f"repair_time_chain:{day.day_index}")
                continue

            if violation.reason_code == "MUST_INCLUDE_MISSING":
                requirement = violation.message.removeprefix("缺少必选项 ")
                term = "".join(requirement.lower().split())
                candidate = next((item for item in candidates if term in "".join(f"{item.name}{item.tags}".lower().split())), None)
                if candidate and repaired.days:
                    from app.schemas.itinerary import TimeSlot
                    target = min(repaired.days, key=lambda item: len(item.slots))
                    target.slots.append(TimeSlot(
                        place_id=candidate.place_id,
                        place=candidate.model_dump(mode="json"),
                        start_time="16:00",
                        end_time="18:00",
                    ))
                    plan.targeted_days.add(target.day_index)
                    plan.actions.append(f"insert_required:{target.day_index}:{candidate.place_id}")
                else:
                    plan.unresolved.append(f"{violation.constraint_id}:candidate_missing")
                continue

            if violation.reason_code == "MEAL_WINDOW_EMPTY" and day:
                is_lunch = violation.constraint_id.startswith("system:lunch")
                desired_start = "12:15" if is_lunch else "18:30"
                desired_end = "13:15" if is_lunch else "19:30"
                anchor_start = 12 * 60 + 15 if is_lunch else 18 * 60 + 30
                anchor_end = 13 * 60 + 15 if is_lunch else 19 * 60 + 30

                def move_overlaps_after(anchor) -> None:
                    """Keep a repaired meal fixed and shift only colliding slots.

                    The generic time-chain repair must not push a lunch out of
                    its verification window on the next round.
                    """
                    cursor = anchor_end
                    for other in sorted(
                        (item for item in day.slots if item is not anchor),
                        key=lambda item: item.start_time,
                    ):
                        try:
                            start_h, start_m = map(int, other.start_time.split(":"))
                            end_h, end_m = map(int, other.end_time.split(":"))
                        except (AttributeError, TypeError, ValueError):
                            continue
                        start = start_h * 60 + start_m
                        end = end_h * 60 + end_m
                        if start < anchor_end and end > anchor_start:
                            duration = max(30, end - start)
                            start = cursor + 15
                            end = start + duration
                            other.start_time = f"{start // 60:02d}:{start % 60:02d}"
                            other.end_time = f"{end // 60:02d}:{end % 60:02d}"
                            cursor = end

                occupied_meal_ids = {
                    current.place_id
                    for current in day.slots
                    if (current.start_time.startswith("12:") or current.start_time.startswith("18:") or current.start_time.startswith("19:"))
                    and (current.place or {}).get("category") == "food"
                }
                movable = next(
                    (
                        slot for slot in day.slots
                        if (slot.place or {}).get("category") == "food" and slot.place_id not in occupied_meal_ids
                    ),
                    None,
                )
                if movable:
                    movable.start_time = desired_start
                    movable.end_time = desired_end
                    move_overlaps_after(movable)
                    day.slots.sort(key=lambda item: item.start_time if not item.start_time.startswith("次日") else "99:99")
                    plan.actions.append(f"move_meal:{day.day_index}:{movable.place_id}")
                    continue
                used_ids = {slot.place_id for current_day in repaired.days for slot in current_day.slots}
                food = next((item for item in candidates if item.category.value == "food" and item.place_id not in used_ids), None)
                if food:
                    from app.schemas.itinerary import TimeSlot
                    day.slots.append(TimeSlot(
                        place_id=food.place_id,
                        place=food.model_dump(mode="json"),
                        start_time=desired_start,
                        end_time=desired_end,
                    ))
                    move_overlaps_after(day.slots[-1])
                    day.slots.sort(key=lambda item: item.start_time if not item.start_time.startswith("次日") else "99:99")
                    plan.actions.append(f"insert_meal:{day.day_index}:{food.place_id}")
                else:
                    # The scheduler may have consumed every food candidate but
                    # concentrated more than two on another day. Rebalance one
                    # surplus meal instead of inventing a POI or dropping the
                    # violation.
                    donor = None
                    for other_day in repaired.days:
                        if other_day.day_index == day.day_index:
                            continue
                        donor_food = [
                            item for item in other_day.slots
                            if getattr((item.place or {}).get("category"), "value", (item.place or {}).get("category")) == "food"
                        ]
                        if len(donor_food) > 2:
                            donor = (other_day, donor_food[-1])
                            break
                    if donor:
                        donor_day, donor_slot = donor
                        donor_day.slots.remove(donor_slot)
                        donor_slot.start_time = desired_start
                        donor_slot.end_time = desired_end
                        day.slots.append(donor_slot)
                        move_overlaps_after(donor_slot)
                        day.slots.sort(key=lambda item: item.start_time if not item.start_time.startswith("次日") else "99:99")
                        plan.actions.append(
                            f"rebalance_meal:{donor_day.day_index}:{day.day_index}:{donor_slot.place_id}"
                        )
                    else:
                        plan.unresolved.append(f"{violation.constraint_id}:food_candidate_missing")
                continue

            if violation.reason_code in {"BUDGET_EXCEEDED", "TRAVEL_TIME_EXCEEDED", "OUTSIDE_OPENING_HOURS"}:
                target_days = [day] if day else repaired.days
                removed = False
                for current_day in target_days:
                    candidates_to_remove = [
                        slot for slot in current_day.slots
                        if not protected(slot) and not is_meal_anchor(slot)
                    ]
                    if not candidates_to_remove:
                        candidates_to_remove = [slot for slot in current_day.slots if not protected(slot)]
                    if candidates_to_remove:
                        candidates_to_remove.sort(key=lambda slot: float((slot.place or {}).get("amap_price") or 0), reverse=True)
                        victim = candidates_to_remove[0]
                        current_day.slots.remove(victim)
                        plan.targeted_days.add(current_day.day_index)
                        plan.actions.append(f"remove_for_constraint:{current_day.day_index}:{victim.place_id}")
                        removed = True
                        break
                if not removed:
                    plan.unresolved.append(f"{violation.constraint_id}:no_unlocked_candidate")
                continue

            plan.unresolved.append(f"{violation.constraint_id}:{violation.reason_code}:unsupported")

        if plan.actions:
            repaired.version += 1
        return repaired, plan
