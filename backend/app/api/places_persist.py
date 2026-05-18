"""
房间景点持久化 + 路线保存 + HTML 导出接口。

POST /api/room/{room_id}/places/sync      — Yjs 景点批量同步到 DB
GET  /api/room/{room_id}/places           — 获取房间已持久化景点
POST /api/room/{room_id}/itinerary        — 保存优化路线（需登录）
GET  /api/itinerary/{itinerary_id}/export — 导出路线为 HTML（需登录）
"""

import json
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.db.connection import get_pool
from app.utils.auth import get_current_user, get_optional_user

router = APIRouter()


# =============================================
# 景点同步
# =============================================

class PlaceSyncRequest(BaseModel):
    places: list[dict]
    voted_by_map: Optional[dict[str, list[str]]] = None  # { place_id: [user_id, ...] }


@router.post("/room/{room_id}/places/sync")
async def sync_places(room_id: str, body: PlaceSyncRequest, _: Optional[str] = Depends(get_optional_user)):
    """把 Yjs 内存景点批量 UPSERT 到 DB（无需鉴权，房间内任意成员均可调用）。"""
    if not body.places:
        return {"ok": True, "synced": 0}

    pool = await get_pool()
    async with pool.acquire() as conn:
        synced = 0
        for place in body.places:
            place_id = place.get("place_id") or place.get("id")
            if not place_id:
                continue
            voted_by = (body.voted_by_map or {}).get(place_id, [])
            await conn.execute(
                """
                INSERT INTO room_places (room_id, place_id, place_data, voted_by, updated_at)
                VALUES ($1, $2, $3::jsonb, $4, NOW())
                ON CONFLICT (room_id, place_id) DO UPDATE
                  SET place_data = EXCLUDED.place_data,
                      voted_by   = EXCLUDED.voted_by,
                      updated_at = NOW()
                """,
                room_id,
                place_id,
                json.dumps(place, ensure_ascii=False),
                voted_by,
            )
            synced += 1
    return {"ok": True, "synced": synced}


@router.get("/room/{room_id}/places")
async def get_room_places(room_id: str):
    """返回房间持久化景点列表（进入房间时用于恢复状态）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT place_id, place_data, voted_by, added_at
            FROM room_places
            WHERE room_id = $1
            ORDER BY added_at ASC
            """,
            room_id,
        )
    return [
        {
            **json.loads(r["place_data"]),
            "voted_by": list(r["voted_by"] or []),
        }
        for r in rows
    ]


# =============================================
# 路线保存
# =============================================

class SaveItineraryRequest(BaseModel):
    itinerary_data: dict
    city: Optional[str] = None
    trip_days: Optional[int] = None


@router.post("/room/{room_id}/itinerary")
async def save_itinerary(room_id: str, body: SaveItineraryRequest, user_id: str = Depends(get_current_user)):
    """将优化后路线保存到 DB，返回 itinerary_id。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO saved_itineraries (room_id, user_id, city, trip_days, itinerary_data)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            RETURNING id
            """,
            room_id,
            user_id,
            body.city,
            body.trip_days,
            json.dumps(body.itinerary_data, ensure_ascii=False),
        )
    return {"ok": True, "itinerary_id": str(row["id"])}


# =============================================
# HTML 导出
# =============================================

@router.get("/itinerary/{itinerary_id}/export", response_class=HTMLResponse)
async def export_itinerary(itinerary_id: str, user_id: str = Depends(get_current_user)):
    """将路线渲染为响应式 HTML 文件并返回（浏览器触发下载）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, city, trip_days, itinerary_data FROM saved_itineraries WHERE id = $1",
            itinerary_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="路线不存在")
    if row["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="无权访问此路线")

    itinerary = json.loads(row["itinerary_data"])
    html = _render_itinerary_html(itinerary, row["city"], row["trip_days"])

    city_safe = (row["city"] or "旅行").replace(" ", "_")
    filename = f"BreezeTravel_{city_safe}_{row['trip_days']}天路线.html"
    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f'attachment; filename*=UTF-8\'\'{filename}'},
    )


# =============================================
# HTML 模板渲染（内联，无外部模板引擎依赖）
# =============================================

_CATEGORY_ICON = {
    "ATTRACTION": "🏛️",
    "FOOD": "🍜",
    "HOTEL": "🏨",
    "TRANSPORT": "🚌",
}


def _render_itinerary_html(itinerary: dict, city: Optional[str], trip_days: Optional[int]) -> str:
    days = itinerary.get("days", [])
    city_name = city or itinerary.get("city", "旅行")
    days_count = trip_days or len(days)

    day_sections = ""
    for day in days:
        slots = day.get("slots", [])
        weather = day.get("weather_summary", {})
        weather_html = ""
        if weather:
            weather_html = f"""
            <div class="weather">
                {weather.get("condition", "")}
                {weather.get("temp_high", "")}°/{weather.get("temp_low", "")}°C
                · {weather.get("suggestion", "")}
            </div>"""

        slots_html = ""
        for i, slot in enumerate(slots):
            place = slot.get("place", {})
            name = place.get("name", "")
            category = place.get("category", "")
            address = place.get("address", "")
            rating = place.get("amap_rating", "")
            duration = place.get("estimated_duration", 0)
            icon = _CATEGORY_ICON.get(category, "📍")
            start = slot.get("start_time", "")
            end = slot.get("end_time", "")
            transport = slot.get("transport", {})

            transport_html = ""
            if transport and i < len(slots) - 1:
                mode_map = {"driving": "🚗 驾车", "walking": "🚶 步行", "transit": "🚇 公交"}
                mode = mode_map.get(transport.get("mode", ""), "前往")
                dur = transport.get("duration_mins", "")
                dist = transport.get("distance_km", "")
                transport_html = f"""
                <div class="transport">
                    {mode} · 约 {dur} 分钟 · {dist} km
                </div>"""

            slots_html += f"""
            <div class="slot">
                <div class="slot-time">{start} – {end}</div>
                <div class="slot-body">
                    <div class="place-icon">{icon}</div>
                    <div class="place-info">
                        <div class="place-name">{name}</div>
                        {f'<div class="place-address">📍 {address}</div>' if address else ''}
                        {f'<div class="place-meta">⭐ {rating} &nbsp;&bull;&nbsp; 建议 {duration} 分钟</div>' if rating else ''}
                    </div>
                </div>
                {transport_html}
            </div>"""

        day_idx = day.get("day_index", 1)
        date_str = day.get("date", "")
        day_sections += f"""
        <div class="day-card">
            <div class="day-header">
                <span class="day-badge">Day {day_idx}</span>
                <span class="day-date">{date_str}</span>
                {weather_html}
            </div>
            <div class="day-slots">{slots_html}</div>
        </div>"""

    from datetime import datetime
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>BreezeTravel · {city_name} {days_count} 天路线</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f8f7f5; color: #333; padding: 16px; }}
    .header {{ text-align: center; padding: 32px 16px 24px; }}
    .logo {{ font-size: 28px; font-weight: 800; color: #FF5A5F; letter-spacing: -0.5px; }}
    .subtitle {{ color: #888; font-size: 14px; margin-top: 6px; }}
    .meta {{ display: inline-flex; gap: 16px; margin-top: 12px; font-size: 13px; color: #555; }}
    .meta span {{ background: #fff; border: 1px solid #eee; border-radius: 20px;
                  padding: 4px 12px; box-shadow: 0 1px 3px rgba(0,0,0,.04); }}
    .day-card {{ background: #fff; border-radius: 16px; box-shadow: 0 2px 12px rgba(0,0,0,.06);
                 margin: 16px auto; max-width: 680px; overflow: hidden; }}
    .day-header {{ background: linear-gradient(135deg, #FF5A5F 0%, #ff8086 100%);
                   color: #fff; padding: 16px 20px; display: flex; align-items: center; gap: 12px;
                   flex-wrap: wrap; }}
    .day-badge {{ font-size: 18px; font-weight: 800; }}
    .day-date {{ font-size: 13px; opacity: .85; }}
    .weather {{ font-size: 12px; opacity: .9; margin-left: auto; }}
    .day-slots {{ padding: 8px 0; }}
    .slot {{ padding: 16px 20px; border-bottom: 1px solid #f5f5f5; }}
    .slot:last-child {{ border-bottom: none; }}
    .slot-time {{ font-size: 11px; color: #aaa; font-weight: 600; letter-spacing: .5px;
                  margin-bottom: 8px; }}
    .slot-body {{ display: flex; gap: 14px; align-items: flex-start; }}
    .place-icon {{ font-size: 24px; line-height: 1; flex-shrink: 0; margin-top: 2px; }}
    .place-name {{ font-size: 15px; font-weight: 700; color: #222; }}
    .place-address {{ font-size: 12px; color: #888; margin-top: 3px; }}
    .place-meta {{ font-size: 12px; color: #999; margin-top: 3px; }}
    .transport {{ margin: 10px 20px 0; padding: 8px 12px; background: #f9f9f9;
                  border-radius: 8px; font-size: 12px; color: #777; text-align: center; }}
    .footer {{ text-align: center; font-size: 12px; color: #ccc; margin: 32px 0 16px;
               padding-top: 16px; border-top: 1px solid #eee; max-width: 680px; margin-inline: auto; }}
    @media (max-width: 480px) {{
      .day-header {{ padding: 12px 16px; }}
      .slot {{ padding: 14px 16px; }}
    }}
  </style>
</head>
<body>
  <div class="header">
    <div class="logo">BreezeTravel</div>
    <div class="subtitle">AI 智能旅行规划</div>
    <div class="meta">
      <span>📍 {city_name}</span>
      <span>📅 {days_count} 天</span>
    </div>
  </div>
  {day_sections}
  <div class="footer">
    由 BreezeTravel 生成 · {generated_at}
  </div>
</body>
</html>"""
