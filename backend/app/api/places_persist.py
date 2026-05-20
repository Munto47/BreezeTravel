"""
房间景点持久化 + 路线保存 + HTML 导出接口。

POST /api/room/{room_id}/places/sync      — Yjs 景点批量同步到 DB
GET  /api/room/{room_id}/places           — 获取房间已持久化景点
POST /api/room/{room_id}/itinerary        — 保存优化路线（需登录）
GET  /api/room/{room_id}/itinerary        — 获取房间最新路线 JSON（需登录）
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


@router.get("/room/{room_id}/itinerary")
async def get_room_itinerary(room_id: str, user_id: str = Depends(get_current_user)):
    """返回该房间最新一条已保存路线的完整 JSON（用于跨设备恢复行程详情页）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, city, trip_days, itinerary_data, created_at
            FROM saved_itineraries
            WHERE room_id = $1 AND user_id = $2
            ORDER BY created_at DESC
            LIMIT 1
            """,
            room_id,
            user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="no itinerary found")
    return {
        "itinerary_id": str(row["id"]),
        "city": row["city"],
        "trip_days": row["trip_days"],
        "itinerary_data": json.loads(row["itinerary_data"]),
        "created_at": row["created_at"].isoformat(),
    }


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

    # Content-Disposition 中文文件名必须 URL 编码（RFC 5987），否则 latin-1 编码报 500
    from urllib.parse import quote
    city_name = (row["city"] or "旅行").replace(" ", "_")
    filename_raw = f"BreezeTravel_{city_name}_{row['trip_days']}天路线.html"
    filename_encoded = quote(filename_raw, safe="")
    disposition = f"attachment; filename=\"export.html\"; filename*=UTF-8''{filename_encoded}"
    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": disposition},
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

            tips = slot.get("tips", [])
            tips_html = ""
            if tips:
                tips_items = "".join(f'<span class="tip-item">💡 {t}</span>' for t in tips[:3])
                tips_html = f'<div class="tips">{tips_items}</div>'

            slots_html += f"""
            <div class="slot">
                <div class="slot-time">{start} – {end}</div>
                <div class="slot-body">
                    <div class="place-icon">{icon}</div>
                    <div class="place-info">
                        <div class="place-name">{name}</div>
                        {f'<div class="place-address">📍 {address}</div>' if address else ''}
                        {f'<div class="place-meta">⭐ {rating} &nbsp;&bull;&nbsp; 建议游览 {duration} 分钟</div>' if rating else f'<div class="place-meta">建议游览 {duration} 分钟</div>' if duration else ''}
                    </div>
                </div>
                {tips_html}
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
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif;
            background: #f8f7f5; color: #333; padding: 16px; }}

    /* ── 顶部操作栏 ── */
    .toolbar {{ display: flex; justify-content: center; gap: 12px; margin-bottom: 8px; }}
    .btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 8px 20px;
            border-radius: 20px; font-size: 13px; font-weight: 600; cursor: pointer;
            border: none; transition: opacity .15s; }}
    .btn:hover {{ opacity: .85; }}
    .btn-primary {{ background: #FF5A5F; color: #fff; }}
    .btn-secondary {{ background: #fff; color: #555; border: 1px solid #ddd; }}

    /* ── 页面主体 ── */
    .header {{ text-align: center; padding: 28px 16px 20px; }}
    .logo {{ font-size: 26px; font-weight: 800; color: #FF5A5F; letter-spacing: -0.5px; }}
    .subtitle {{ color: #888; font-size: 13px; margin-top: 5px; }}
    .meta {{ display: inline-flex; gap: 12px; margin-top: 12px; font-size: 13px; color: #555; flex-wrap: wrap; justify-content: center; }}
    .meta span {{ background: #fff; border: 1px solid #eee; border-radius: 20px;
                  padding: 4px 14px; box-shadow: 0 1px 3px rgba(0,0,0,.04); }}
    .day-card {{ background: #fff; border-radius: 16px; box-shadow: 0 2px 12px rgba(0,0,0,.07);
                 margin: 16px auto; max-width: 700px; overflow: hidden; page-break-inside: avoid; }}
    .day-header {{ background: linear-gradient(135deg, #FF5A5F 0%, #ff8a8e 100%);
                   color: #fff; padding: 14px 20px; display: flex; align-items: center; gap: 12px;
                   flex-wrap: wrap; }}
    .day-badge {{ font-size: 17px; font-weight: 800; }}
    .day-date {{ font-size: 13px; opacity: .85; }}
    .weather {{ font-size: 12px; opacity: .9; margin-left: auto; }}
    .day-slots {{ padding: 4px 0; }}
    .slot {{ padding: 14px 20px; border-bottom: 1px solid #f0f0f0; }}
    .slot:last-child {{ border-bottom: none; }}
    .slot-time {{ font-size: 11px; color: #bbb; font-weight: 600; letter-spacing: .5px; margin-bottom: 6px; }}
    .slot-body {{ display: flex; gap: 14px; align-items: flex-start; }}
    .place-icon {{ font-size: 22px; line-height: 1.2; flex-shrink: 0; }}
    .place-name {{ font-size: 15px; font-weight: 700; color: #111; }}
    .place-address {{ font-size: 12px; color: #999; margin-top: 2px; }}
    .place-meta {{ font-size: 12px; color: #aaa; margin-top: 2px; }}
    .tips {{ margin-top: 8px; }}
    .tip-item {{ font-size: 11px; color: #b45309; background: #fef3c7;
                 border-radius: 6px; padding: 4px 10px; margin-top: 4px; display: inline-block; }}
    .transport {{ margin: 8px 20px 0; padding: 7px 12px; background: #f5f5f5;
                  border-radius: 8px; font-size: 12px; color: #888; text-align: center; }}
    .footer {{ text-align: center; font-size: 12px; color: #ccc; margin: 28px auto 16px;
               padding-top: 14px; border-top: 1px solid #eee; max-width: 700px; }}

    /* ── 打印 / PDF 样式 ── */
    @media print {{
      body {{ background: #fff; padding: 0; }}
      .toolbar {{ display: none !important; }}
      .day-card {{ box-shadow: none; border: 1px solid #e5e5e5; margin: 12px 0; border-radius: 10px; }}
      .day-header {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
      .footer {{ margin-top: 20px; }}
      @page {{ margin: 18mm 14mm; size: A4; }}
    }}
    @media (max-width: 480px) {{
      .day-header {{ padding: 12px 14px; }}
      .slot {{ padding: 12px 14px; }}
      .toolbar {{ flex-direction: column; align-items: center; }}
    }}
  </style>
</head>
<body>
  <!-- 操作工具栏（打印时自动隐藏） -->
  <div class="toolbar">
    <button class="btn btn-primary" onclick="window.print()">🖨️ 打印 / 保存为 PDF</button>
    <button class="btn btn-secondary" onclick="window.close()">✕ 关闭</button>
  </div>

  <div class="header">
    <div class="logo">✈ BreezeTravel</div>
    <div class="subtitle">AI 智能旅行规划</div>
    <div class="meta">
      <span>📍 {city_name}</span>
      <span>📅 {days_count} 天</span>
      <span>🗓 {generated_at}</span>
    </div>
  </div>

  {day_sections}

  <div class="footer">
    由 BreezeTravel AI 生成 · {generated_at} · 如需保存请点击「打印 / 保存为 PDF」
  </div>
</body>
</html>"""
