"""Rule Fast Path（SPEC §4.2 / C4）

处理无需 LLM 的简单编辑意图：
  - remove_place：从某天某槽删除地点
  - swap_days：交换两天的地点列表（day_index ↔ swap_with_day）

fast_apply(patch, itinerary) → (new_itinerary, violations)

返回 violations 列表（Critic 硬规则检查结果）。
"""

from __future__ import annotations

import copy
from typing import Optional

from app.schemas.itinerary import DayPlan, Itinerary, TimeSlot
from app.schemas.patch import ItineraryPatch


# ─── Critic 轻量检查（不依赖 DB）─────────────────────────────────────────────

def _check_day_violations(day: DayPlan) -> list[dict]:
    """对单天 slot 列表做快速结构检查（不含 R_OPEN_HOURS）"""
    from app.agents.nodes.optimizer import _time_str_to_mins

    violations: list[dict] = []
    slots = day.slots

    # R_ZERO_FOOD_DAY（空行程也视为无餐饮）
    has_food = any(
        (s.place or {}).get("category") == "food"
        for s in slots
    )
    if not has_food:
        violations.append({
            "rule": "R_ZERO_FOOD_DAY",
            "day_index": day.day_index,
            "place_id": None,
            "message": "编辑后全天无餐饮安排",
        })

    # R_BUFFER_DEFICIT（简单时间序检查）
    filled = [s for s in slots if s.end_time and s.end_time != "次日12:00"]
    for i in range(1, len(filled)):
        try:
            prev_end = _time_str_to_mins(filled[i - 1].end_time)
            next_start = _time_str_to_mins(filled[i].start_time)
            if prev_end > next_start:
                violations.append({
                    "rule": "R_BUFFER_DEFICIT",
                    "day_index": day.day_index,
                    "place_id": filled[i].place_id,
                    "message": f"编辑后时间链断裂：{filled[i-1].end_time} → {filled[i].start_time}",
                })
        except Exception:
            pass

    return violations


# ─── remove_place ─────────────────────────────────────────────────────────────

def _apply_remove(patch: ItineraryPatch, itinerary: Itinerary) -> tuple[Itinerary, list[dict]]:
    it = copy.deepcopy(itinerary)
    target_day: Optional[DayPlan] = None
    for d in it.days:
        if d.day_index == patch.day_index:
            target_day = d
            break

    if target_day is None:
        return it, [{"rule": "PATCH_ERROR", "day_index": patch.day_index,
                     "place_id": None, "message": f"day_index={patch.day_index} 不存在"}]

    original_len = len(target_day.slots)
    target_day.slots = [
        s for s in target_day.slots
        if s.place_id != patch.target_place_id
    ]
    if len(target_day.slots) == original_len:
        return it, [{"rule": "PATCH_ERROR", "day_index": patch.day_index,
                     "place_id": patch.target_place_id,
                     "message": f"未找到 place_id={patch.target_place_id}"}]

    # 重新编号 slot_index（若 TimeSlot 有该字段）
    violations = _check_day_violations(target_day)
    return it, violations


# ─── swap_days ────────────────────────────────────────────────────────────────

def _apply_swap_days(patch: ItineraryPatch, itinerary: Itinerary) -> tuple[Itinerary, list[dict]]:
    """互换两天的槽位列表（slot 内容），day_index 和 date 标签不变"""
    it = copy.deepcopy(itinerary)
    day_map = {d.day_index: d for d in it.days}

    # swap_with_day 约定：放在 target_place_id 字段（复用传参），解析为 int
    try:
        other_idx = int(patch.target_place_id or "")
    except (ValueError, TypeError):
        return it, [{"rule": "PATCH_ERROR", "day_index": patch.day_index,
                     "place_id": None, "message": "swap_days 需要 target_place_id=<other_day_index>"}]

    if patch.day_index not in day_map or other_idx not in day_map:
        return it, [{"rule": "PATCH_ERROR", "day_index": patch.day_index,
                     "place_id": None, "message": f"swap_days day 不存在：{patch.day_index} / {other_idx}"}]

    day_a = day_map[patch.day_index]
    day_b = day_map[other_idx]
    day_a.slots, day_b.slots = day_b.slots, day_a.slots

    violations = _check_day_violations(day_a) + _check_day_violations(day_b)
    return it, violations


# ─── 主入口 ───────────────────────────────────────────────────────────────────

def fast_apply(
    patch: ItineraryPatch,
    itinerary: Itinerary,
) -> tuple[Itinerary, list[dict]]:
    """Rule Fast Path 主入口。返回 (新行程, 违规列表)。"""
    if patch.op == "remove_place":
        return _apply_remove(patch, itinerary)
    elif patch.op == "swap_days":
        return _apply_swap_days(patch, itinerary)
    else:
        return itinerary, [{
            "rule": "PATCH_ERROR",
            "day_index": patch.day_index,
            "place_id": None,
            "message": f"op={patch.op} 不在 Rule Fast Path 范围，请走 EditorAgent",
        }]
