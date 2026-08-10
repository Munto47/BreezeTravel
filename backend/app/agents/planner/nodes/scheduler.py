"""SchedulerAgent：时间表生成 + 酒店挂载 + 天气富集

按 day_index 顺序逐日装配 DayPlan：
1. 调用 _generate_time_slots 生成时间槽（含智能时长、夜间后移、用餐占位）
2. 从 hotels_pool 找最近酒店，挂载为当天最后一个 slot
3. 若 start_date 落在未来 3 天内，调用和风天气 API 富集
"""

from datetime import date, timedelta
from typing import Optional

import aiohttp

from app.agents.nodes.optimizer import (
    _estimate_driving,
    _fetch_weather,
    _generate_time_slots,
    _match_hotel,
    _time_str_to_mins,
)
from app.agents.planner.state import PlannerState
from app.config import settings
from app.schemas.itinerary import DayPlan, TimeSlot, TransportLeg, WeatherInfo


async def run(state: PlannerState) -> dict:
    orderings: dict[int, list] = state["orderings"]
    time_matrices: dict[int, dict] = state.get("time_matrices", {})
    hotels_pool: list = list(state.get("hotels_pool", []))
    start_date: Optional[str] = state.get("start_date")
    center_lat: float = state.get("center_lat", 0.0)
    center_lng: float = state.get("center_lng", 0.0)

    sorted_cluster_items = sorted(orderings.items())
    today = date.today()
    weather_enabled = bool(settings.qweather_api_key) and start_date is not None

    day_plans: list[DayPlan] = []

    async with aiohttp.ClientSession() as session:
        for day_index, (cluster_id, ordered) in enumerate(sorted_cluster_items):
            matrix = time_matrices.get(cluster_id, {})

            prev_end = (
                day_plans[-1].slots[-1].end_time
                if day_plans and day_plans[-1].slots
                else None
            )
            slots = _generate_time_slots(ordered, matrix, prev_day_last_slot_end=prev_end)

            # ── 酒店挂载（作为"今晚住宿"day-end marker，非游览 slot）─────
            if slots:
                sorted_ordered = sorted(ordered, key=lambda p: p.visit_order or 0)
                last_activity = sorted_ordered[-1]
                hotel = _match_hotel(last_activity, hotels_pool)
                if hotel:
                    dur_mins, dist_km = _estimate_driving(last_activity, hotel)
                    slots[-1] = slots[-1].model_copy(update={
                        "transport": TransportLeg(
                            mode="driving",
                            duration_mins=dur_mins,
                            distance_km=dist_km,
                        )
                    })
                    # check-in 时间：上一活动结束后 + 车程；但不早于 21:00（一天结束）
                    hotel_start = max(
                        _time_str_to_mins(slots[-1].end_time) + dur_mins,
                        21 * 60,
                    )
                    hotel = hotel.model_copy(update={
                        "cluster_id": day_index,
                        "visit_order": len(slots),
                        "tags": list(hotel.tags or []) + ["今晚住宿"],
                    })
                    slots.append(TimeSlot(
                        place_id=hotel.place_id,
                        place=hotel.model_dump(),
                        start_time=f"{hotel_start // 60:02d}:{hotel_start % 60:02d}",
                        end_time="次日 12:00",
                        transport=None,
                    ))

            # ── 天气富集 ───────────────────────────────────────────────
            weather_summary: Optional[WeatherInfo] = None
            day_date_str: Optional[str] = None
            if start_date:
                try:
                    trip_start = date.fromisoformat(start_date)
                    day_date = trip_start + timedelta(days=day_index)
                    day_date_str = day_date.isoformat()
                    if weather_enabled:
                        offset = (day_date - today).days
                        if 0 <= offset <= 2:
                            weather_summary = await _fetch_weather(
                                session, center_lat, center_lng, offset
                            )
                except Exception as e:
                    print(f"[Scheduler] 日期/天气处理失败：{e}")

            day_plans.append(DayPlan(
                day_index=day_index,
                date=day_date_str,
                cluster_id=cluster_id,
                slots=slots,
                weather_summary=weather_summary,
            ))

    day_plans.sort(key=lambda d: d.day_index)

    trace = state.get("trace", []) + [
        f"[Scheduler] 装配 {len(day_plans)} 天，剩余酒店={len(hotels_pool)}"
    ]
    return {"day_plans": day_plans, "hotels_pool": hotels_pool, "trace": trace}
