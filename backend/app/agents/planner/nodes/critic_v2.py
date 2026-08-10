"""CriticV2：排线结果硬规则检验（SPEC §3.6）

7 条硬规则，违反则触发 Planner 对应天重跑（最多 1 次）：

  R_OPEN_HOURS        — 安排时间 ∉ open_hours_json
  R_NO_BACKTOBACK_L2  — 相邻 slot 同 category_l2
  R_MEAL_SLOT_FILLED  — 12:00–13:30 / 18:00–20:00 无餐饮 slot
  R_DAILY_FOOD_CAP    — 当天餐厅类 ≥ 4
  R_ZERO_FOOD_DAY     — 全天无餐厅
  R_WEATHER_MISMATCH  — 雨天 > 5mm 且 ≥ 2 个户外 slot
  R_BUFFER_DEFICIT    — 通勤时间 > slot 间间隔（时间链断裂）

每条规则返回 list[dict]，dict 格式：
  { rule, day_index, place_id, message }
"""

from __future__ import annotations

from typing import Optional

from app.agents.planner.nodes.scheduler_v2 import (
    _is_outdoor,
)
from app.agents.planner.state import DayPlannerState, PlannerState, Slot
from app.agents.nodes.optimizer import _time_str_to_mins
from app.schemas.preferences import WeatherDay


Violation = dict  # {rule, day_index, place_id, message}


# ─── 单条规则函数 ──────────────────────────────────────────────────────────────

def _check_open_hours(
    slot: Slot,
    day_index: int,
    meta_cache: dict[str, dict],
    dow: int,
) -> Optional[Violation]:
    """R_OPEN_HOURS：slot 安排时间在闭馆时段"""
    pid = slot.get("place_id")
    if not pid or pid not in meta_cache:
        return None
    m = meta_cache[pid]
    hours_json = m.get("open_hours_json")
    if not hours_json:
        return None
    day_key = ["mon","tue","wed","thu","fri","sat","sun"][dow % 7]
    windows = hours_json.get(day_key)
    if windows is None:
        return {
            "rule": "R_OPEN_HOURS",
            "day_index": day_index,
            "place_id": pid,
            "message": f"该地点 {dow_name(dow)} 闭馆，但被安排在第 {day_index+1} 天",
        }
    start_mins = _time_str_to_mins(slot["start_time"])
    end_mins = _time_str_to_mins(slot["end_time"])
    for window in windows:
        open_m, close_m = int(window[0]) * 60, int(window[1]) * 60
        # 跨夜处理（如酒吧 18:00–次日2:00）
        if close_m < open_m:
            close_m += 24 * 60
        if open_m <= start_mins and end_mins <= close_m:
            return None  # 在营业时段内，OK
    return {
        "rule": "R_OPEN_HOURS",
        "day_index": day_index,
        "place_id": pid,
        "message": f"安排时间 {slot['start_time']}–{slot['end_time']} 不在营业时段内",
    }


def _check_no_backtoback_l2(
    slots: list[Slot], day_index: int
) -> list[Violation]:
    """R_NO_BACKTOBACK_L2：相邻两个有地点的 slot 同 category_l2"""
    violations = []
    filled = [s for s in slots if s.get("place_id")]
    for i in range(1, len(filled)):
        l2_a = filled[i - 1].get("category_l2", "")
        l2_b = filled[i].get("category_l2", "")
        if l2_a and l2_b and l2_a == l2_b:
            violations.append({
                "rule": "R_NO_BACKTOBACK_L2",
                "day_index": day_index,
                "place_id": filled[i].get("place_id"),
                "message": f"相邻 slot 都是 [{l2_a}]，品类重复",
            })
    return violations


def _check_meal_slot_filled(slots: list[Slot], day_index: int) -> list[Violation]:
    """R_MEAL_SLOT_FILLED：12:00–13:30 / 18:00–20:00 必须有餐饮 slot"""
    violations = []
    LUNCH = (12 * 60, 13 * 60 + 30)
    DINNER = (18 * 60, 20 * 60)

    def has_food_in(window_start, window_end):
        for s in slots:
            if not s.get("place_id"):
                continue
            if s.get("category_l1") != "餐饮":
                continue
            sm = _time_str_to_mins(s["start_time"])
            if window_start <= sm < window_end:
                return True
        return False

    if not has_food_in(*LUNCH):
        violations.append({
            "rule": "R_MEAL_SLOT_FILLED",
            "day_index": day_index,
            "place_id": None,
            "message": "12:00–13:30 无餐饮 slot（缺午餐）",
        })
    if not has_food_in(*DINNER):
        violations.append({
            "rule": "R_MEAL_SLOT_FILLED",
            "day_index": day_index,
            "place_id": None,
            "message": "18:00–20:00 无餐饮 slot（缺晚餐）",
        })
    return violations


def _check_daily_food_cap(slots: list[Slot], day_index: int) -> Optional[Violation]:
    """R_DAILY_FOOD_CAP：当天餐厅类 slot ≥ 4（含空占位）"""
    food_count = sum(1 for s in slots if s.get("category_l1") == "餐饮")
    if food_count >= 4:
        return {
            "rule": "R_DAILY_FOOD_CAP",
            "day_index": day_index,
            "place_id": None,
            "message": f"当天餐饮 slot {food_count} 个，超过上限 3 个",
        }
    return None


def _check_zero_food_day(slots: list[Slot], day_index: int) -> Optional[Violation]:
    """R_ZERO_FOOD_DAY：全天无餐厅"""
    has_food = any(s.get("category_l1") == "餐饮" and s.get("place_id") for s in slots)
    if not has_food:
        return {
            "rule": "R_ZERO_FOOD_DAY",
            "day_index": day_index,
            "place_id": None,
            "message": "全天无任何餐饮安排",
        }
    return None


def _check_weather_mismatch(
    slots: list[Slot],
    day_index: int,
    weather: Optional[WeatherDay],
) -> Optional[Violation]:
    """R_WEATHER_MISMATCH：雨天 > 5mm 且排了 ≥ 2 个户外 slot"""
    if weather is None or weather.precip_mm <= 5:
        return None
    from app.schemas.place import Place as PlaceModel
    outdoor_count = 0
    for s in slots:
        if not s.get("place"):
            continue
        try:
            p = PlaceModel(**s["place"])
            if _is_outdoor(p):
                outdoor_count += 1
        except Exception:
            pass
    if outdoor_count >= 2:
        return {
            "rule": "R_WEATHER_MISMATCH",
            "day_index": day_index,
            "place_id": None,
            "message": f"雨天降水 {weather.precip_mm}mm，但排了 {outdoor_count} 个户外 slot",
        }
    return None


def _check_buffer_deficit(slots: list[Slot], day_index: int) -> list[Violation]:
    """R_BUFFER_DEFICIT：相邻 slot 的结束时间晚于下一个 slot 开始时间（时间链断裂）"""
    violations = []
    filled = [s for s in slots if s.get("place_id") and s.get("end_time") not in ("次日12:00",)]
    for i in range(1, len(filled)):
        prev_end = _time_str_to_mins(filled[i - 1]["end_time"])
        next_start = _time_str_to_mins(filled[i]["start_time"])
        if prev_end > next_start:
            violations.append({
                "rule": "R_BUFFER_DEFICIT",
                "day_index": day_index,
                "place_id": filled[i].get("place_id"),
                "message": (
                    f"时间链断裂：上一 slot 结束 {filled[i-1]['end_time']}，"
                    f"本 slot 开始 {filled[i]['start_time']}"
                ),
            })
    return violations


# ─── 主入口 ───────────────────────────────────────────────────────────────────

async def run(state: PlannerState) -> dict:
    day_states: dict[int, DayPlannerState] = state.get("day_states", {})
    weather_forecast = state.get("weather_forecast", {})

    if not day_states:
        # v1 兼容：没有 day_states 时跳过 Critic v2
        return {"critic_violations": [], "trace": state.get("trace", []) + ["[CriticV2] 无 day_states，跳过"]}

    # 加载 place_meta（用于 R_OPEN_HOURS 检查）
    all_pids = [
        s["place_id"]
        for ds in day_states.values()
        for s in ds.get("slots", [])
        if s.get("place_id")
    ]
    from app.config import get_settings
    try:
        if not get_settings().place_meta_lookup_enabled:
            raise LookupError("place_meta lookup disabled for controlled test profile")
        from app.db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM place_meta WHERE place_id = ANY($1::text[])",
                all_pids,
            )
        meta_cache = {r["place_id"]: dict(r) for r in rows}
    except Exception as e:
        if get_settings().place_meta_lookup_enabled:
            print(f"[CriticV2] place_meta 查询失败：{e}")
        meta_cache = {}

    all_violations: list[Violation] = []

    for day_index, ds in day_states.items():
        slots = ds.get("slots", [])
        weather = weather_forecast.get(day_index)

        # 确定星期几
        start_date = state.get("start_date")
        dow = 0
        if start_date:
            try:
                from datetime import date, timedelta
                trip_start = date.fromisoformat(start_date)
                dow = (trip_start + timedelta(days=day_index)).weekday()
            except Exception:
                pass

        # 逐条规则
        for s in slots:
            v = _check_open_hours(s, day_index, meta_cache, dow)
            if v:
                all_violations.append(v)

        all_violations.extend(_check_no_backtoback_l2(slots, day_index))
        all_violations.extend(_check_meal_slot_filled(slots, day_index))

        v = _check_daily_food_cap(slots, day_index)
        if v:
            all_violations.append(v)

        v = _check_zero_food_day(slots, day_index)
        if v:
            all_violations.append(v)

        v = _check_weather_mismatch(slots, day_index, weather)
        if v:
            all_violations.append(v)

        all_violations.extend(_check_buffer_deficit(slots, day_index))

    trace = state.get("trace", []) + [
        f"[CriticV2] 检查完成，发现 {len(all_violations)} 条违规"
    ]

    if all_violations:
        for v in all_violations:
            print(f"[CriticV2] {v['rule']} day={v['day_index']} place={v.get('place_id')} — {v['message']}")

    return {"critic_violations": all_violations, "trace": trace}


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def dow_name(dow: int) -> str:
    return ["周一","周二","周三","周四","周五","周六","周日"][dow % 7]


def has_violations(state: PlannerState) -> bool:
    return bool(state.get("critic_violations"))


def violations_by_day(state: PlannerState) -> dict[int, list[Violation]]:
    result: dict[int, list[Violation]] = {}
    for v in state.get("critic_violations", []):
        result.setdefault(v["day_index"], []).append(v)
    return result
