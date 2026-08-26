"""SchedulerAgent v2：鱼骨模板驱动的排线引擎（SPEC §3.3）

升级要点（相比 scheduler.py v1）：
1. 按鱼骨模板选槽位，而非简单地按 TSP 顺序堆叠时间
2. 候选筛选：闭馆时段 / 天气不合适的户外地点会被过滤
3. 用餐时段强制：12:00–13:30 / 18:00–20:00 必须有餐厅 slot
4. 体力曲线：上午 1.0 / 下午 0.7 / 晚间 0.4，重头景点排上午
5. 同一天相邻槽位不能是相同 category_l2
6. 排不下的地点进 backup_pool（A7）
7. 生成 DayPlannerState（day_states 字段）供 Critic 消费

place_meta 查询：
  - 有数据库时异步批量查，缺字段按品类默认值补（conf=low）
  - DEMO_MODE / 无 DB 时全走品类默认值
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import aiohttp

from app.agents.nodes.optimizer import (
    _estimate_driving,
    _fetch_weather,
    _haversine_km,
    _time_str_to_mins,
)
from app.agents.planner.state import DayPlannerState, PlannerState, Slot
from app.agents.planner.templates import TemplateSlot, select_template
from app.config import settings
from app.schemas.itinerary import DayPlan, TimeSlot, WeatherInfo
from app.schemas.place import Place, PlaceCategory
from app.schemas.preferences import GroupPreferences, WeatherDay

# ─── 品类默认 dwell 时间（SPEC §2.1 low 置信度默认值） ─────────────────────────
_CATEGORY_L2_DWELL: dict[str, int] = {
    "博物馆": 120, "历史遗址": 120, "纪念馆": 90, "展览馆": 90,
    "景区": 120, "5A景区": 240, "古迹": 120,
    "主题乐园": 360, "主题公园": 360, "动物园": 180, "科技馆": 120,
    "街区": 90, "古镇": 90, "老街": 90, "艺术区": 75,
    "公园": 75, "广场": 45, "观景台": 45, "夜景": 60,
    "咖啡馆": 60, "茶馆": 60, "甜品": 45,
    "餐厅": 75, "地方菜": 75, "早午餐": 60,
    "火锅": 90, "烧烤": 90, "串串": 90,
    "面馆": 45, "快餐": 30, "小吃": 30,
    "酒吧": 120, "清吧": 90, "夜市": 90,
    "酒店": 30,
}

_CATEGORY_L1_DWELL: dict[str, int] = {
    "景点": 120,
    "餐饮": 60,
    "夜生活": 90,
    "住宿": 30,
}

# ─── D24 取舍评分公式（SPEC §3.5） ────────────────────────────────────────────

def _pref_score(
    place: Place,
    prefs: Optional[GroupPreferences],
    used_l2_today: set[str],
    vote_counts: dict[str, int],
) -> float:
    """
    score(place) =
      +100  if name/tags match must_have
      -∞    if name/tags match no_go  （返回 -inf 表示硬剔除）
      +votes * 10                      投票主导
      +amap_rating * 3                 RAG 人气分近似（高德评分）
      +diversity_bonus * 5             当天品类多样性奖励（当前 l2 未用过）
    """
    if prefs is None:
        return 0.0

    text = f"{place.name} {' '.join(place.tags or [])}"

    # no_go → 硬剔除
    for no in prefs.no_go:
        if no and no in text:
            return float("-inf")

    score = 0.0

    # must_have → 锁定优先
    for mh in prefs.must_have:
        if mh and mh in text:
            score += 100.0
            break

    # nice_to_have 加分
    for nth in prefs.nice_to_have:
        if nth and nth in text:
            score += 5.0
            break

    # 投票权重（votes * 10）
    votes = vote_counts.get(place.place_id, 0)
    score += votes * 10.0

    # RAG 人气分近似（高德评分 * 3，满分 5 → 最多 +15）
    if place.amap_rating and place.amap_rating > 0:
        score += place.amap_rating * 3.0

    # 品类多样性奖励（当天未出现过该 l2 → +5）
    l2 = _guess_l2(place)
    if l2 and l2 not in used_l2_today:
        score += 5.0

    return score


def _is_no_go(place: Place, prefs: Optional[GroupPreferences]) -> bool:
    """快速判断地点是否在 no_go 列表中"""
    if not prefs or not prefs.no_go:
        return False
    text = f"{place.name} {' '.join(place.tags or [])}"
    return any(ng and ng in text for ng in prefs.no_go)


# ─── 户外判断：category_l2 是否倾向户外 ───────────────────────────────────────
_OUTDOOR_L2 = {
    "景区", "古迹", "街区", "古镇", "老街", "公园", "广场", "观景台",
    "夜景", "滨江", "湖畔", "步道", "绿道",
}


def _is_outdoor(place: Place) -> bool:
    tags_text = " ".join(place.tags or [])
    return any(kw in (place.name + tags_text) for kw in ["户外", "景区", "公园", "广场", "街区"])


# ─── 天气过滤 ──────────────────────────────────────────────────────────────────

def _weather_blocks_outdoor(weather: Optional[WeatherDay], prefs: Optional[GroupPreferences]) -> bool:
    """雨天 precip > 5mm 时户外槽应该被换成室内"""
    if weather is None:
        return False
    if weather.precip_mm > 5:
        return True
    return False


def _heat_window(time_mins: int) -> bool:
    """夏季 11:30–14:00 户外体力消耗过大"""
    return 11 * 60 + 30 <= time_mins < 14 * 60


# ─── 从 place_meta 或品类默认值获取 dwell ─────────────────────────────────────

def _dwell_minutes(place: Place, meta_cache: dict[str, dict]) -> int:
    """优先用 place_meta，没有则 estimated_duration，再用品类默认"""
    if place.place_id in meta_cache:
        m = meta_cache[place.place_id]
        if m.get("dwell_minutes"):
            return int(m["dwell_minutes"])
    if place.estimated_duration:
        return place.estimated_duration
    # 从 tags/name 猜 category_l2
    tags_text = " ".join(place.tags or []) + (place.name or "")
    for l2, mins in _CATEGORY_L2_DWELL.items():
        if l2 in tags_text:
            return mins
    cat_map = {
        PlaceCategory.ATTRACTION: 120,
        PlaceCategory.FOOD: 60,
        PlaceCategory.HOTEL: 30,
    }
    return cat_map.get(place.category, 75)


def _open_hours(place: Place, meta_cache: dict[str, dict], day_of_week: int) -> Optional[tuple[int, int]]:
    """返回 (open_min, close_min)；None 表示全天开放或无数据"""
    if place.place_id not in meta_cache:
        return None
    m = meta_cache[place.place_id]
    hours_json: Optional[dict] = m.get("open_hours_json")
    if not hours_json:
        return None
    day_key = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][day_of_week % 7]
    windows = hours_json.get(day_key)
    if not windows:
        return None  # 该天休息（闭馆）表示为 null → 也应被过滤，这里先返回 None 交由调用方处理
    # 取第一个窗口
    w = windows[0]
    return (int(w[0]) * 60, int(w[1]) * 60)


def _is_closed(place: Place, meta_cache: dict[str, dict], day_of_week: int) -> bool:
    """place_meta 中 open_hours_json[day_key] == null → 闭馆"""
    if place.place_id not in meta_cache:
        return False
    m = meta_cache[place.place_id]
    hours_json = m.get("open_hours_json")
    if not hours_json:
        return False
    day_key = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][day_of_week % 7]
    return hours_json.get(day_key) is None


# ─── 体力曲线 ──────────────────────────────────────────────────────────────────

def _stamina(time_mins: int) -> float:
    """上午 1.0 / 下午 0.7 / 晚间 0.4"""
    if time_mins < 12 * 60:
        return 1.0
    if time_mins < 18 * 60:
        return 0.7
    return 0.4


def _place_hardness(place: Place) -> float:
    """地点"体力消耗"值（0–1），博物馆/景区较高，咖啡/餐厅较低"""
    if place.category == PlaceCategory.FOOD:
        return 0.2
    text = " ".join(place.tags or []) + place.name
    if any(k in text for k in ["主题乐园", "景区", "5A", "博物馆", "古镇"]):
        return 0.9
    if any(k in text for k in ["公园", "街区", "广场"]):
        return 0.5
    return 0.6


# ─── 槽位分配：候选地点是否适合填入该模板槽 ─────────────────────────────────

def _slot_match_score(place: Place, t_slot: TemplateSlot) -> float:
    """候选地点与模板槽位的匹配分，≤0 表示不适合"""
    tags_text = " ".join(place.tags or []) + " " + place.name

    # category_l1 快速过滤
    l1_map = {
        "景点": PlaceCategory.ATTRACTION,
        "餐饮": PlaceCategory.FOOD,
        "住宿": PlaceCategory.HOTEL,
        "夜生活": PlaceCategory.FOOD,  # 酒吧等归在 FOOD
    }
    expected_l1_cat = l1_map.get(t_slot.category_l1)
    if expected_l1_cat and place.category != expected_l1_cat:
        return 0.0

    # category_l2 关键词匹配
    score = 0.0
    for l2 in t_slot.category_l2_candidates:
        if l2 in tags_text:
            score += 10.0
            break
    if score == 0.0 and expected_l1_cat == place.category:
        score = 1.0  # 同 l1 弱匹配

    return score


# ─── 主调度器 ──────────────────────────────────────────────────────────────────

async def _load_place_meta(place_ids: list[str]) -> dict[str, dict]:
    """从 DB 批量读取 place_meta；无 DB 时返回空 dict"""
    from app.config import get_settings
    if not place_ids or not get_settings().place_meta_lookup_enabled:
        return {}
    try:
        from app.db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM place_meta WHERE place_id = ANY($1::text[])",
                place_ids,
            )
        return {r["place_id"]: dict(r) for r in rows}
    except Exception as e:
        print(f"[SchedulerV2] place_meta 查询失败（降级到品类默认）：{e}")
        return {}


async def run(state: PlannerState) -> dict:
    orderings: dict[int, list[Place]] = state.get("orderings", {})
    hotels_pool: list[Place] = list(state.get("hotels_pool", []))
    start_date: Optional[str] = state.get("start_date")
    user_prefs: Optional[GroupPreferences] = state.get("user_prefs")
    weather_forecast: dict[int, WeatherDay] = state.get("weather_forecast", {})
    trip_days: int = state.get("trip_days", len(orderings))
    # D24：投票计数（place_id → 票数），由前端通过 OptimizeRequest 传入
    vote_counts: dict[str, int] = state.get("vote_counts", {})

    # 加载所有地点的 place_meta
    all_places = [p for places in orderings.values() for p in places]
    meta_cache = await _load_place_meta([p.place_id for p in all_places])
    stay_hotel = _select_stay_hotel(all_places, hotels_pool)
    # 备用餐厅仍然必须服从用户的硬性禁忌；否则跨簇补餐会把前面
    # 已经由 valid_candidates 排除的 no_go 地点重新塞回行程。
    meal_reserve = [
        place
        for place in all_places
        if place.category == PlaceCategory.FOOD
        and not _is_no_go(place, user_prefs)
    ]
    meal_catalog = list(meal_reserve)
    attraction_reserve = [
        place
        for place in all_places
        if place.category == PlaceCategory.ATTRACTION
        and not _is_no_go(place, user_prefs)
    ]

    # 确定出发日期（用于星期几判断）
    trip_start_date: Optional[date] = None
    if start_date:
        try:
            trip_start_date = date.fromisoformat(start_date)
        except Exception:
            pass

    today = date.today()
    weather_enabled = bool(settings.qweather_api_key) and start_date is not None

    day_plans: list[DayPlan] = []
    day_states: dict[int, DayPlannerState] = {}
    backup_pool: list[Place] = list(state.get("backup_pool", []))
    used_place_ids: set[str] = set()

    async with aiohttp.ClientSession() as http_session:
        for cluster_id, candidates in sorted(orderings.items()):
            day_index = cluster_id
            day_weather = weather_forecast.get(day_index)

            # 当天是星期几（0=周一）
            dow = 0
            day_date: Optional[date] = None
            if trip_start_date:
                day_date = trip_start_date + timedelta(days=day_index)
                dow = day_date.weekday()

            # 选模板
            template = select_template(day_index, trip_days, user_prefs)

            # 过滤候选：闭馆 / 天气不合适 / D24 no_go 硬剔除
            rain_block = day_weather and day_weather.precip_mm > 5
            used_l2_today: set[str] = set()
            valid_candidates = [
                p for p in candidates
                if p.place_id not in used_place_ids
                and not _is_closed(p, meta_cache, dow)
                and not _is_no_go(p, user_prefs)   # D24：no_go 硬剔除
            ]
            # Geographic clustering can occasionally produce a food-only day.
            # Borrow one unused attraction from the global reserve so a multi-
            # day trip never degenerates into "吃一顿然后等到回酒店".
            if not any(p.category == PlaceCategory.ATTRACTION for p in valid_candidates):
                borrowed = next(
                    (p for p in attraction_reserve if p.place_id not in used_place_ids),
                    None,
                )
                if borrowed is not None:
                    valid_candidates.append(borrowed)

            # 按模板槽位分配地点（贪心）
            day_slots: list[Slot] = []
            available = list(valid_candidates)
            prev_l2: Optional[str] = None
            cursor_mins = 9 * 60  # 当天起步时间 09:00

            for t_slot in template.slots:
                if not available and not t_slot.is_required:
                    continue

                # Template hints are real scheduling constraints, not labels.
                # Without this, a "dinner" slot could accidentally start at
                # 14:30 simply because the previous item finished early.
                cursor_mins = max(cursor_mins, t_slot.start_hint)

                # D24 + 模板匹配联合评分：slot_match * 10 + pref_score
                def _combined_score(p: Place, _slot=t_slot) -> float:
                    slot_s = _slot_match_score(p, _slot)
                    if slot_s <= 0:
                        return slot_s  # 不匹配则保持负数，排在后面
                    pref_s = _pref_score(p, user_prefs, used_l2_today, vote_counts)
                    if pref_s == float("-inf"):
                        return float("-inf")
                    return slot_s * 10 + pref_s

                scored = sorted(available, key=_combined_score, reverse=True)

                chosen: Optional[Place] = None
                for cand in scored:
                    if _combined_score(cand) == float("-inf"):
                        continue  # no_go
                    if _slot_match_score(cand, t_slot) <= 0:
                        break  # 后面都不匹配

                    remaining_days = max(0, trip_days - day_index - 1)
                    if (
                        cand.category == PlaceCategory.ATTRACTION
                        and not t_slot.is_required
                        and len(attraction_reserve) <= remaining_days
                    ):
                        continue  # 为后续每一天保留至少一个真实活动点

                    # 天气+体力约束：雨天/热窗口时户外槽换室内
                    if rain_block and _is_outdoor(cand) and not t_slot.is_required:
                        continue

                    # 相邻 category_l2 去重
                    cand_l2 = _guess_l2(cand)
                    if cand_l2 and cand_l2 == prev_l2:
                        continue

                    # 体力曲线：晚间不排高体力景点
                    if _stamina(cursor_mins) < 0.5 and _place_hardness(cand) > 0.8:
                        continue

                    chosen = cand
                    break

                if chosen is None:
                    if t_slot.is_required:
                        # 必须槽但没有合适候选，创建"空位占位"
                        day_slots.append(_make_empty_slot(t_slot, cursor_mins, day_index, len(day_slots)))
                    cursor_mins += t_slot.duration_minutes
                    continue

                # The template represents the promised human rhythm.  A generic
                # POI duration must not turn an optional 45-minute arrival-day
                # stroll into a two-hour late-night activity.
                dwell = min(_dwell_minutes(chosen, meta_cache), t_slot.duration_minutes)
                end_mins = cursor_mins + dwell + t_slot.buffer_minutes

                slot: Slot = {
                    "slot_index": len(day_slots),
                    "template_slot_id": t_slot.slot_id,
                    "place_id": chosen.place_id,
                    "place": chosen.model_dump(),
                    "start_time": _mins_to_str(cursor_mins),
                    "end_time": _mins_to_str(end_mins),
                    "category_l1": t_slot.category_l1,
                    "category_l2": _guess_l2(chosen) or t_slot.category_l2_candidates[0],
                    "is_required": t_slot.is_required,
                }
                day_slots.append(slot)
                available.remove(chosen)
                used_place_ids.add(chosen.place_id)
                if chosen in meal_reserve:
                    meal_reserve.remove(chosen)
                if chosen in attraction_reserve:
                    attraction_reserve.remove(chosen)
                l2_chosen = _guess_l2(chosen)
                prev_l2 = l2_chosen
                if l2_chosen:
                    used_l2_today.add(l2_chosen)   # D24 diversity_bonus 追踪
                cursor_mins = end_mins + 15  # 15 min 通勤 buffer

            # 确保用餐时段有餐厅（R_MEAL_SLOT_FILLED 硬规则）
            day_slots = _ensure_meal_slots(
                day_slots,
                available,
                day_index=day_index,
                trip_days=trip_days,
                used_place_ids=used_place_ids,
                meal_candidates=meal_reserve,
                reusable_meal_candidates=meal_catalog,
                template_id=template.template_id,
            )

            # 剩余排不下的候选进备选池
            deferred_meals = {
                p.place_id for p in available
                if p.category == PlaceCategory.FOOD and p in meal_reserve
            }
            deferred_attractions = {
                p.place_id for p in available
                if p.category == PlaceCategory.ATTRACTION and p in attraction_reserve
            }
            deferred_for_future = deferred_meals | deferred_attractions
            backup_pool.extend(
                p for p in available
                if p.place_id not in used_place_ids and p.place_id not in deferred_for_future
            )
            for p in available:
                if p.place_id in deferred_for_future:
                    continue
                used_place_ids.add(p.place_id)

            # 酒店挂载
            day_slots = _attach_hotel(day_slots, stay_hotel, day_index)

            # 天气信息
            weather_summary: Optional[WeatherInfo] = None
            day_date_str: Optional[str] = None
            if day_date:
                day_date_str = day_date.isoformat()
                forecast = state.get("weather_forecast", {}).get(day_index)
                if forecast is not None:
                    weather_summary = WeatherInfo(
                        condition=forecast.condition,
                        temp_high=round(forecast.temp_max),
                        temp_low=round(forecast.temp_min),
                        suggestion=(
                            "降水较明显，优先安排室内地点"
                            if forecast.precip_mm > 5
                            else "天气动态信息已进入本次审计"
                        ),
                        precip_mm=forecast.precip_mm,
                    )
                elif weather_enabled:
                    offset = (day_date - today).days
                    if 0 <= offset <= 2:
                        try:
                            center_lat = state.get("center_lat", 0.0)
                            center_lng = state.get("center_lng", 0.0)
                            weather_summary = await _fetch_weather(
                                http_session, center_lat, center_lng, offset
                            )
                        except Exception as e:
                            print(f"[SchedulerV2] 天气查询失败：{e}")

            # 转换为 DayPlan（兼容旧结构）
            time_slots = _slots_to_timeslots(day_slots)
            day_plan = DayPlan(
                day_index=day_index,
                date=day_date_str,
                cluster_id=cluster_id,
                slots=time_slots,
                weather_summary=weather_summary,
            )
            day_plans.append(day_plan)

            # DayPlannerState（供 Critic 消费）
            day_states[day_index] = DayPlannerState(
                day_index=day_index,
                template_id=template.template_id,
                slots=day_slots,
                locked=False,
                rationale=f"使用模板 {template.name}，排入 {len([s for s in day_slots if s.get('place_id')])} 个地点",
                overflow_places=[p.place_id for p in backup_pool],
            )

    day_plans.sort(key=lambda d: d.day_index)
    backup_ids = {place.place_id for place in backup_pool}
    backup_pool.extend(
        place for place in meal_reserve
        if place.place_id not in used_place_ids and place.place_id not in backup_ids
    )
    backup_ids = {place.place_id for place in backup_pool}
    backup_pool.extend(
        place for place in attraction_reserve
        if place.place_id not in used_place_ids and place.place_id not in backup_ids
    )

    trace = state.get("trace", []) + [
        f"[SchedulerV2] {len(day_plans)} 天，backup_pool={len(backup_pool)} 个备选"
    ]

    return {
        "day_plans": day_plans,
        "day_states": day_states,
        "hotels_pool": hotels_pool,
        "backup_pool": backup_pool,
        "trace": trace,
    }


# ─── 辅助函数 ──────────────────────────────────────────────────────────────────

def _mins_to_str(mins: int) -> str:
    h = (mins % (24 * 60)) // 60
    m = mins % 60
    return f"{h:02d}:{m:02d}"


def _guess_l2(place: Place) -> Optional[str]:
    """从 tags/name 猜 category_l2"""
    text = " ".join(place.tags or []) + " " + (place.name or "")
    for l2 in _CATEGORY_L2_DWELL:
        if l2 in text:
            return l2
    return None


def _make_empty_slot(t_slot: TemplateSlot, cursor_mins: int, day_index: int, idx: int) -> Slot:
    end_mins = cursor_mins + t_slot.duration_minutes
    return {
        "slot_index": idx,
        "template_slot_id": t_slot.slot_id,
        "place_id": None,
        "place": None,
        "start_time": _mins_to_str(cursor_mins),
        "end_time": _mins_to_str(end_mins),
        "category_l1": t_slot.category_l1,
        "category_l2": t_slot.category_l2_candidates[0] if t_slot.category_l2_candidates else "",
        "is_required": t_slot.is_required,
    }


def _has_meal_in_window(slots: list[Slot], window_start: int, window_end: int) -> bool:
    for s in slots:
        if not s.get("place_id"):
            continue
        if s.get("category_l1") != "餐饮":
            continue
        start = _time_str_to_mins(s["start_time"])
        if window_start <= start < window_end:
            return True
    return False


def _ensure_meal_slots(
    slots: list[Slot],
    available: Optional[list[Place]] = None,
    cursor_mins: Optional[int] = None,
    *,
    day_index: Optional[int] = None,
    trip_days: Optional[int] = None,
    used_place_ids: Optional[set[str]] = None,
    meal_candidates: Optional[list[Place]] = None,
    reusable_meal_candidates: Optional[list[Place]] = None,
    template_id: Optional[str] = None,
) -> list[Slot]:
    """Fill humane meal anchors with real restaurants when candidates exist.

    Arrival day does not force lunch before the traveller arrives.  Departure
    day allows an 11:00 early lunch and does not invent a dinner after leaving.
    """
    LUNCH_START, LUNCH_END = 12 * 60, 13 * 60 + 30
    DINNER_START, DINNER_END = 18 * 60, 20 * 60

    result = list(slots)
    if isinstance(available, int):
        available = []
    available = available or []
    used_place_ids = used_place_ids if used_place_ids is not None else set()
    meal_candidates = meal_candidates if meal_candidates is not None else available
    reusable_meal_candidates = reusable_meal_candidates or []
    windows: list[tuple[str, int, int, str, str]] = []
    legacy_all_day = day_index is None or trip_days is None
    is_arrival = template_id == "T_ARRIVAL" or (template_id is None and not legacy_all_day and day_index == 0)
    is_departure = template_id == "T_DEPARTURE" or (
        template_id is None and not legacy_all_day and day_index == trip_days - 1
    )
    if legacy_all_day or not is_arrival:
        lunch_start = 11 * 60 if is_departure else LUNCH_START
        windows.append(("lunch_fallback", lunch_start, LUNCH_END, _mins_to_str(lunch_start), _mins_to_str(lunch_start + 60)))
    if legacy_all_day or not is_departure:
        windows.append(("dinner_fallback", DINNER_START, DINNER_END, "18:30", "19:45"))

    for slot_id, window_start, window_end, start_time, end_time in windows:
        if _has_meal_in_window(result, window_start, window_end):
            continue
        food = next(
            (
                place for place in meal_candidates
                if place.category == PlaceCategory.FOOD and place.place_id not in used_place_ids
            ),
            None,
        )
        if food is None:
            # A strict district may expose fewer unique restaurants than the
            # number of meal anchors. Reusing a real family-suitable restaurant
            # on another day is preferable to inventing a POI or omitting food.
            today_ids = {slot.get("place_id") for slot in result}
            food = next(
                (
                    place for place in reusable_meal_candidates
                    if place.category == PlaceCategory.FOOD and place.place_id not in today_ids
                ),
                None,
            )
        if food:
            if food in available:
                available.remove(food)
            if food in meal_candidates:
                meal_candidates.remove(food)
            used_place_ids.add(food.place_id)
            result.append({
                "slot_index": len(result),
                "template_slot_id": slot_id,
                "place_id": food.place_id,
                "place": food.model_dump(),
                "start_time": start_time,
                "end_time": end_time,
                "category_l1": "餐饮",
                "category_l2": _guess_l2(food) or "餐厅",
                "is_required": True,
            })
        else:
            result.append({
                "slot_index": len(result),
                "template_slot_id": slot_id,
                "place_id": None,
                "place": None,
                "start_time": start_time,
                "end_time": end_time,
                "category_l1": "餐饮",
                "category_l2": "餐厅",
                "is_required": True,
            })

    # Meals are fixed human anchors.  If an optional/template activity runs
    # through lunch or dinner, keep the meal and move that activity to backup
    # instead of returning an impossible overlapping timetable.
    meal_anchors = [
        slot for slot in result
        if slot.get("place_id")
        and slot.get("category_l1") == "餐饮"
        and (
            11 * 60 <= _time_str_to_mins(slot["start_time"]) < LUNCH_END
            or DINNER_START <= _time_str_to_mins(slot["start_time"]) < DINNER_END
        )
    ]
    displaced: list[Slot] = []
    for slot in result:
        if slot in meal_anchors:
            continue
        slot_start = _time_str_to_mins(slot["start_time"])
        slot_end = _time_str_to_mins(slot["end_time"])
        if any(
            slot_start < _time_str_to_mins(anchor["end_time"])
            and slot_end > _time_str_to_mins(anchor["start_time"])
            for anchor in meal_anchors
        ):
            displaced.append(slot)
    for slot in displaced:
        result.remove(slot)
        if slot.get("place"):
            try:
                restored = Place(**slot["place"])
                if all(place.place_id != restored.place_id for place in available):
                    available.append(restored)
                used_place_ids.discard(restored.place_id)
            except Exception:
                pass

    result.sort(key=lambda slot: _time_str_to_mins(slot["start_time"]))
    for index, slot in enumerate(result):
        slot["slot_index"] = index
    return result


def _select_stay_hotel(activities: list[Place], hotels_pool: list[Place]) -> Optional[Place]:
    """Choose one stable base hotel for the whole trip.

    A single central hotel avoids the old behaviour where the pool was
    consumed one hotel per day and later days had no lodging at all.
    """
    if not hotels_pool:
        return None
    def rank(hotel: Place) -> tuple[float, float]:
        rating = hotel.amap_rating if hotel.amap_rating is not None else -1.0
        total_distance = (
            sum(_haversine_km(activity, hotel) for activity in activities)
            if activities else 0.0
        )
        # For a multi-night family base, accommodation quality is the primary
        # human-facing choice; centrality breaks ties between similarly rated
        # hotels instead of selecting a low-quality lodging solely by centroid.
        return rating, -total_distance

    return max(hotels_pool, key=rank)


def _attach_hotel(slots: list[Slot], hotel: Optional[Place], day_index: int) -> list[Slot]:
    """Append the same selected hotel as the final slot of every day."""
    last_place: Optional[Place] = None
    for s in reversed(slots):
        if s.get("place"):
            from app.schemas.place import Place as PlaceModel
            last_place = PlaceModel(**s["place"])
            break

    if last_place is None or hotel is None:
        return slots

    dur_mins, dist_km = _estimate_driving(last_place, hotel)
    last_end = _time_str_to_mins(slots[-1]["end_time"]) if slots else 21 * 60
    hotel_start = max(last_end + dur_mins, 21 * 60)

    slots.append({
        "slot_index": len(slots),
        "template_slot_id": "hotel_checkin" if day_index == 0 else "hotel_return",
        "place_id": hotel.place_id,
        "place": hotel.model_copy(update={
            "tags": list(hotel.tags or []) + (["办理入住", "今晚住宿"] if day_index == 0 else ["返回酒店", "今晚住宿"]),
        }).model_dump(),
        "start_time": _mins_to_str(hotel_start),
        "end_time": "次日12:00",
        "category_l1": "住宿",
        "category_l2": "酒店",
        "is_required": True,
    })
    return slots


def _slots_to_timeslots(slots: list[Slot]) -> list[TimeSlot]:
    """将 Slot 列表转换为 TimeSlot（兼容旧 itinerary 结构）"""
    result = []
    for s in slots:
        if not s.get("place_id") or not s.get("place"):
            continue  # 跳过空占位槽
        place_payload = dict(s["place"])
        # Keep the scheduler's deterministic secondary classification on the
        # legacy draft so the authoritative pacing rule can reproduce the
        # retired Critic check during the persistence/audit hand-off.
        if s.get("category_l2"):
            place_payload["category_l2"] = s["category_l2"]
        result.append(TimeSlot(
            place_id=s["place_id"],
            place=place_payload,
            start_time=s["start_time"],
            end_time=s["end_time"],
            transport=None,
        ))
    return result
