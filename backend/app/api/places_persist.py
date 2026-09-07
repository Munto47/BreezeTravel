"""
房间景点持久化 + 路线保存 + HTML 导出接口。

POST /api/room/{room_id}/places/sync      — Yjs 景点批量同步到 DB
GET  /api/room/{room_id}/places           — 获取房间已持久化景点
POST /api/room/{room_id}/itinerary        — 保存优化路线（需登录）
GET  /api/room/{room_id}/itinerary        — 获取房间最新路线 JSON（需登录）
GET  /api/itinerary/{itinerary_id}/export — 导出路线为 HTML（需登录）
"""

import json
import math
from collections.abc import Mapping
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.db.connection import get_pool
from app.utils.auth import get_current_user, get_optional_user
from app.services.room_access import require_room_member
from app.config import get_settings

router = APIRouter()


# =============================================
# 景点同步
# =============================================

class PlaceSyncRequest(BaseModel):
    places: list[dict]
    # Compatibility-only. Yjs is not an account identity authority, so these
    # claimed user ids are intentionally ignored by the persistence layer.
    voted_by_map: Optional[dict[str, list[str]]] = None


def _sanitize_shared_place(place: dict) -> dict:
    """Project untrusted CRDT JSON onto public place facts only."""

    def text(key: str, limit: int) -> str:
        value = place.get(key)
        if not isinstance(value, str):
            return ""
        return " ".join(value.split())[:limit]

    def number(key: str, minimum: float, maximum: float) -> float | None:
        value = place.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        numeric = float(value)
        return numeric if math.isfinite(numeric) and minimum <= numeric <= maximum else None

    place_id = text("place_id", 200) or text("id", 200)
    name = text("name", 120)
    category = text("category", 40)
    coords = place.get("coords")
    if (
        not place_id
        or not name
        or category not in {"attraction", "food", "hotel", "transport"}
        or not isinstance(coords, Mapping)
    ):
        return {}
    lng = coords.get("lng")
    lat = coords.get("lat")
    if (
        isinstance(lng, bool)
        or isinstance(lat, bool)
        or not isinstance(lng, (int, float))
        or not isinstance(lat, (int, float))
        or not math.isfinite(float(lng))
        or not math.isfinite(float(lat))
        or abs(float(lng)) > 180
        or abs(float(lat)) > 90
    ):
        return {}
    source = text("source", 40)
    if source not in {"amap_poi", "rag", "synthesized"}:
        source = "synthesized"
    photos = place.get("amap_photos")
    tags = place.get("tags")
    selected = place.get("room_selected") is True or (
        isinstance(place.get("votedBy"), list) and bool(place["votedBy"])
    ) or (
        isinstance(place.get("voted_by"), list) and bool(place["voted_by"])
    )
    projected = {
        "place_id": place_id,
        "name": name,
        "category": category,
        "address": text("address", 240),
        "coords": {"lng": float(lng), "lat": float(lat)},
        "city": text("city", 80),
        "district": text("district", 80) or None,
        "source": source,
        "amap_rating": number("amap_rating", 0, 5),
        "amap_price": number("amap_price", 0, 1_000_000),
        "opening_hours": text("opening_hours", 160) or None,
        "phone": text("phone", 80) or None,
        "amap_photos": [
            " ".join(item.split())[:2048]
            for item in photos[:5]
            if isinstance(item, str) and item.startswith(("https://", "http://"))
        ]
        if isinstance(photos, list)
        else [],
        "description": text("description", 1_000) or None,
        "tags": [
            " ".join(item.split())[:40]
            for item in tags[:12]
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(tags, list)
        else [],
        "constraint_evidence": [],
        "geo_evidence": [],
        "confirmation_actions": [],
        "estimated_duration": number("estimated_duration", 0, 24 * 60),
        "room_selected": selected,
    }
    return projected


@router.post("/room/{room_id}/places/sync")
async def sync_places(room_id: str, body: PlaceSyncRequest, user_id: Optional[str] = Depends(get_optional_user)):
    """把 Yjs 内存景点批量 UPSERT 到 DB（仅房间成员）。"""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        if not get_settings().demo_mode:
            if user_id is None:
                raise HTTPException(status_code=401, detail="请先登录")
            member = await conn.fetchval(
                """
                SELECT 1
                FROM room_members
                WHERE room_id = $1 AND user_id = $2
                FOR KEY SHARE
                """,
                room_id,
                user_id,
            )
            if member is None:
                raise HTTPException(status_code=403, detail="不是该房间成员")
        sanitized_places = [
            sanitized
            for place in body.places
            if (sanitized := _sanitize_shared_place(place))
        ]
        if body.places and not sanitized_places:
            raise HTTPException(status_code=422, detail="地点数据不可用")
        synced = 0
        retained_place_ids: list[str] = []
        for sanitized in sanitized_places:
            place_id = sanitized.get("place_id") or sanitized.get("id")
            if not place_id:
                continue
            place_id = str(place_id)
            retained_place_ids.append(place_id)
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
                json.dumps(sanitized, ensure_ascii=False),
                [],
            )
            synced += 1
        if retained_place_ids:
            await conn.execute(
                """
                DELETE FROM room_places
                WHERE room_id = $1 AND NOT (place_id = ANY($2::text[]))
                """,
                room_id,
                retained_place_ids,
            )
        else:
            await conn.execute(
                "DELETE FROM room_places WHERE room_id = $1",
                room_id,
            )
    return {"ok": True, "synced": synced}


@router.get("/room/{room_id}/places")
async def get_room_places(room_id: str, user_id: Optional[str] = Depends(get_optional_user)):
    """返回房间持久化景点列表（进入房间时用于恢复状态）。"""
    pool = await get_pool()
    if not get_settings().demo_mode:
        if user_id is None:
            raise HTTPException(status_code=401, detail="请先登录")
        await require_room_member(room_id, user_id, pool=pool)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT place_id, place_data, added_at
            FROM room_places
            WHERE room_id = $1
            ORDER BY added_at ASC
            """,
            room_id,
        )
    return [
        {
            **_sanitize_shared_place(json.loads(r["place_data"])),
            "voted_by": [],
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
    await require_room_member(room_id, user_id, pool=pool)
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
    await require_room_member(room_id, user_id, pool=pool)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, city, trip_days, itinerary_data, created_at
            FROM saved_itineraries
            WHERE room_id = $1 AND user_id = $2
            ORDER BY created_at DESC NULLS LAST, id DESC
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

# 与前端详情页完全对齐的配色
_CLUSTER_COLORS = ["#FF5A5F", "#3B82F6", "#10B981", "#F59E0B", "#8B5CF6", "#06B6D4"]

_CATEGORY_ICON = {
    "attraction": "🏛️", "ATTRACTION": "🏛️",
    "food": "🍜",       "FOOD": "🍜",
    "hotel": "🏨",      "HOTEL": "🏨",
    "transport": "🚉",  "TRANSPORT": "🚉",
}
_CATEGORY_LABEL = {
    "attraction": "景点", "ATTRACTION": "景点",
    "food": "餐饮",       "FOOD": "餐饮",
    "hotel": "住宿",      "HOTEL": "住宿",
    "transport": "交通",  "TRANSPORT": "交通",
}
_WEATHER_EMOJI = {
    "晴": "☀️", "多云": "⛅", "阴": "☁️",
    "雨": "🌧️", "雪": "❄️", "雷": "⛈️",
}

def _weather_icon(condition: str) -> str:
    for k, v in _WEATHER_EMOJI.items():
        if k in condition:
            return v
    return "🌤️"


def _render_itinerary_html(itinerary: dict, city: Optional[str], trip_days: Optional[int]) -> str:
    from datetime import datetime
    days = itinerary.get("days", [])
    city_name = city or itinerary.get("city", "旅行")
    days_count = trip_days or len(days)
    total_places = sum(len(d.get("slots", [])) for d in days)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── 每日色条（与前端完全一致）────────────────────────────
    color_bars = "".join(
        f'<div style="flex:1;height:6px;background:{_CLUSTER_COLORS[i % len(_CLUSTER_COLORS)]}"></div>'
        for i in range(days_count)
    )

    # ── 每日行程区块 ─────────────────────────────────────────
    day_sections = ""
    for day_idx_0, day in enumerate(days):
        color = _CLUSTER_COLORS[day_idx_0 % len(_CLUSTER_COLORS)]
        slots = day.get("slots", [])
        day_num = day.get("day_index", day_idx_0) + 1

        # 日期标签
        raw_date = day.get("date", "")
        date_label = ""
        if raw_date:
            try:
                d = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                date_label = d.strftime("%-m月%-d日")
            except Exception:
                date_label = raw_date[:10]

        # 天气
        weather = day.get("weather_summary") or {}
        weather_html = ""
        if weather:
            cond = weather.get("condition", "")
            w_icon = _weather_icon(cond)
            t_high = weather.get("temp_high", "")
            t_low  = weather.get("temp_low", "")
            suggestion = weather.get("suggestion", "")
            weather_html = (
                f'<div class="weather-card">'
                f'  <span class="w-icon">{w_icon}</span>'
                f'  <div>'
                f'    <div class="w-temp">{cond} {t_low}°–{t_high}°C</div>'
                f'    <div class="w-tip">{suggestion}</div>'
                f'  </div>'
                f'</div>'
            )

        # 每个地点槽位
        slots_html = ""
        for s_idx, slot in enumerate(slots):
            is_last = s_idx == len(slots) - 1
            place = slot.get("place", {})

            name       = place.get("name", "")
            category   = place.get("category", "")
            address    = place.get("address", "")
            description = place.get("description", "")
            rating     = place.get("amap_rating") or place.get("amapRating") or ""
            price      = place.get("amap_price") or place.get("amapPrice") or ""
            duration   = place.get("estimated_duration") or place.get("estimatedDuration") or 0
            opening    = place.get("opening_hours") or place.get("openingHours") or ""
            phone      = place.get("phone") or ""
            photos     = place.get("amap_photos") or place.get("amapPhotos") or []
            tags       = place.get("tags") or []
            rag_tip    = (place.get("rag_meta") or place.get("ragMeta") or {})
            rag_snippet = (rag_tip.get("tip_snippets") or rag_tip.get("tipSnippets") or [""])[0] if rag_tip else ""

            icon  = _CATEGORY_ICON.get(category, "📍")
            label = _CATEGORY_LABEL.get(category, category)
            start_t = slot.get("start_time") or slot.get("startTime") or ""
            end_t   = slot.get("end_time")   or slot.get("endTime")   or ""
            tips    = slot.get("tips") or []

            # 图片
            photo_html = ""
            if photos:
                photo_html = (
                    f'<div class="photo-wrap">'
                    f'  <img src="{photos[0]}" alt="{name}" class="photo" />'
                    f'</div>'
                )

            # 标签行
            tags_html = ""
            if tags:
                tag_items = "".join(
                    f'<span class="tag">{t}</span>' for t in tags[:4]
                )
                tags_html = f'<div class="tags">{tag_items}</div>'

            # 评分 / 价格 / 时长
            meta_parts = []
            if rating:
                meta_parts.append(f'<span class="rating">⭐ {rating}</span>')
            if price:
                meta_parts.append(f'<span class="price">¥{price}/人</span>')
            if duration:
                meta_parts.append(f'<span class="dur">⏱ 建议 {duration} 分钟</span>')
            meta_html = f'<div class="meta-row">{"  ".join(meta_parts)}</div>' if meta_parts else ""

            # 营业时间 / 电话
            info_parts = []
            if opening:
                info_parts.append(f'🕐 {opening}')
            if phone:
                info_parts.append(f'📞 {phone}')
            info_html = (
                f'<div class="info-row">{"  ·  ".join(info_parts)}</div>'
                if info_parts else ""
            )

            # RAG 游记提示
            rag_html = ""
            if rag_snippet:
                rag_html = (
                    f'<div class="rag-tip">⚠️ {rag_snippet}</div>'
                )

            # AI 温馨提示
            ai_tips_html = ""
            if tips:
                tip_items = "".join(f'<div class="ai-tip">💡 {t}</div>' for t in tips[:3])
                ai_tips_html = f'<div class="ai-tips">{tip_items}</div>'

            # 驾车连接段（非最后一个）
            transport = slot.get("transport") or {}
            transport_html = ""
            if not is_last and transport:
                mode_map = {"driving": "🚗 驾车", "walking": "🚶 步行", "transit": "🚇 公交"}
                mode = mode_map.get(transport.get("mode", ""), "🚗 前往")
                dur  = transport.get("duration_mins") or transport.get("durationMins") or ""
                dist = transport.get("distance_km")   or transport.get("distanceKm")   or ""
                transport_html = (
                    f'<div class="transport">'
                    f'  {mode}&nbsp;&nbsp;约 {dur} 分钟&nbsp;·&nbsp;{dist} km'
                    f'</div>'
                )

            # 时间轴连接线（非最后一个）
            line_html = (
                f'<div class="timeline-line" style="border-color:{color}33"></div>'
                if not is_last else ""
            )

            slots_html += f"""
        <div class="slot-wrap">
          <div class="timeline-col">
            <div class="timeline-dot" style="background:{color}20;border-color:{color}60">{icon}</div>
            {line_html}
          </div>
          <div class="slot-body">
            <div class="slot-card">
              {photo_html}
              <div class="slot-content">
                <div class="slot-header">
                  <span class="slot-time">⏰ {start_t} – {end_t}</span>
                  <span class="cat-badge" style="background:{color}18;color:{color}">{label}</span>
                </div>
                <div class="place-name">{name}</div>
                {f'<div class="place-desc">{description}</div>' if description else f'<div class="place-addr">📍 {address}</div>' if address else ''}
                {tags_html}
                {meta_html}
                {info_html}
                {rag_html}
                {ai_tips_html}
              </div>
            </div>
            {transport_html}
          </div>
        </div>"""

        day_sections += f"""
      <section class="day-section" style="--day-color:{color}">
        <div class="day-head">
          <div class="day-badge" style="background:{color}">&nbsp;D{day_num}&nbsp;</div>
          <div>
            <div class="day-title">第 {day_num} 天{f" · {date_label}" if date_label else ""}</div>
            <div class="day-sub">{len(slots)} 个地点</div>
          </div>
          {weather_html}
        </div>
        <div class="day-slots">
          {slots_html}
        </div>
      </section>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>BreezeTravel · {city_name} {days_count} 天路线</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;
         background:#f3f4f6;color:#1f2937;padding:12px}}

    /* ── 工具栏 ── */
    .toolbar{{display:flex;justify-content:center;gap:12px;padding:12px 0 8px;flex-wrap:wrap}}
    .btn{{display:inline-flex;align-items:center;gap:6px;padding:9px 22px;border-radius:22px;
          font-size:13px;font-weight:600;cursor:pointer;border:none;transition:opacity .15s}}
    .btn:hover{{opacity:.85}}
    .btn-pdf{{background:#FF5A5F;color:#fff;box-shadow:0 2px 8px rgba(255,90,95,.35)}}
    .btn-close{{background:#fff;color:#555;border:1px solid #d1d5db}}

    /* ── 概览横幅 ── */
    .banner{{border-radius:20px;overflow:hidden;max-width:700px;margin:0 auto 20px;
             box-shadow:0 4px 20px rgba(0,0,0,.12)}}
    .banner-body{{background:linear-gradient(135deg,#FF5A5F 0%,#3B82F6 100%);
                  color:#fff;padding:24px 28px}}
    .banner-label{{font-size:11px;opacity:.7;margin-bottom:4px;display:flex;align-items:center;gap:4px}}
    .banner-title{{font-size:26px;font-weight:800;margin-bottom:16px}}
    .banner-stats{{display:flex;gap:32px}}
    .stat-label{{font-size:11px;opacity:.6}}
    .stat-val{{font-size:14px;font-weight:700;margin-top:2px}}
    .color-bar{{display:flex;height:6px}}

    /* ── 每日区块 ── */
    .day-section{{max-width:700px;margin:0 auto 28px;page-break-inside:avoid}}
    .day-head{{display:flex;align-items:center;gap:12px;margin-bottom:14px;
               padding:8px 4px;background:#f3f4f6;position:sticky;top:0;z-index:10;flex-wrap:wrap}}
    .day-badge{{color:#fff;font-size:14px;font-weight:800;border-radius:10px;
                padding:6px 14px;flex-shrink:0;box-shadow:0 2px 6px rgba(0,0,0,.15)}}
    .day-title{{font-size:15px;font-weight:700;color:#111}}
    .day-sub{{font-size:12px;color:#9ca3af;margin-top:1px}}
    .weather-card{{margin-left:auto;display:flex;align-items:center;gap:8px;
                   background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;
                   padding:6px 12px}}
    .w-icon{{font-size:20px}}
    .w-temp{{font-size:12px;font-weight:600;color:#1e40af}}
    .w-tip{{font-size:11px;color:#3b82f6}}

    /* ── 时间轴槽位 ── */
    .day-slots{{padding-left:4px}}
    .slot-wrap{{display:flex;gap:0;position:relative;align-items:stretch}}
    .timeline-col{{display:flex;flex-direction:column;align-items:center;width:44px;flex-shrink:0}}
    .timeline-dot{{width:40px;height:40px;border-radius:50%;border:2px solid;
                   display:flex;align-items:center;justify-content:center;font-size:18px;
                   background:#fff;flex-shrink:0;z-index:1;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
    .timeline-line{{flex:1;width:2px;min-height:20px;border-left:2px dashed;margin:4px 0;opacity:.4}}
    .slot-body{{flex:1;padding-bottom:20px;padding-left:12px}}
    .slot-card{{background:#fff;border-radius:14px;border:1px solid #f0f0f0;
                overflow:hidden;box-shadow:0 1px 6px rgba(0,0,0,.06);transition:box-shadow .2s}}
    .photo-wrap{{height:140px;overflow:hidden;position:relative}}
    .photo{{width:100%;height:100%;object-fit:cover;display:block}}
    .slot-content{{padding:14px 16px}}
    .slot-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}}
    .slot-time{{font-size:11px;color:#9ca3af;font-weight:600;letter-spacing:.4px}}
    .cat-badge{{font-size:10px;font-weight:600;padding:2px 8px;border-radius:20px}}
    .place-name{{font-size:16px;font-weight:700;color:#111;margin-bottom:4px}}
    .place-desc{{font-size:12px;color:#6b7280;line-height:1.5;margin-bottom:6px}}
    .place-addr{{font-size:12px;color:#9ca3af;margin-bottom:6px}}
    .tags{{display:flex;flex-wrap:wrap;gap:4px;margin:6px 0}}
    .tag{{font-size:10px;padding:2px 7px;background:#f9fafb;color:#6b7280;
          border:1px solid #e5e7eb;border-radius:6px}}
    .meta-row{{display:flex;align-items:center;gap:12px;margin-top:8px;flex-wrap:wrap}}
    .rating{{font-size:12px;color:#d97706;font-weight:600}}
    .price{{font-size:12px;color:#6b7280}}
    .dur{{font-size:12px;color:#9ca3af}}
    .info-row{{font-size:11px;color:#9ca3af;margin-top:5px}}
    .rag-tip{{margin-top:10px;padding:8px 12px;background:#fffbeb;border:1px solid #fde68a;
              border-radius:8px;font-size:12px;color:#92400e;line-height:1.5}}
    .ai-tips{{margin-top:8px}}
    .ai-tip{{padding:6px 10px;background:#eff6ff;border:1px solid #bfdbfe;
             border-radius:8px;font-size:12px;color:#1d4ed8;margin-top:4px;line-height:1.5}}

    /* ── 驾车连接 ── */
    .transport{{margin-top:10px;padding:8px 14px;background:#f9fafb;border-radius:10px;
                font-size:12px;color:#6b7280;text-align:center;border:1px solid #f3f4f6}}

    /* ── 页脚 ── */
    .footer{{text-align:center;font-size:12px;color:#d1d5db;margin:32px auto 16px;
             padding-top:16px;border-top:1px solid #e5e7eb;max-width:700px}}

    /* ── 打印 / PDF ── */
    @media print{{
      body{{background:#fff;padding:0}}
      .toolbar{{display:none!important}}
      .day-head{{position:static;background:#fff}}
      .slot-card{{box-shadow:none;border:1px solid #e5e7eb}}
      .banner{{box-shadow:none}}
      .banner-body{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
      .day-badge{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
      @page{{margin:16mm 12mm;size:A4}}
    }}
    @media(max-width:500px){{
      .banner-body{{padding:18px 20px}}
      .banner-title{{font-size:22px}}
      .slot-content{{padding:12px}}
    }}
  </style>
</head>
<body>

  <!-- 工具栏（打印时隐藏） -->
  <div class="toolbar">
    <button class="btn btn-pdf" onclick="window.print()">🖨️ 打印 / 保存为 PDF</button>
    <button class="btn btn-close" onclick="window.close()">✕ 关闭</button>
  </div>

  <!-- 概览横幅 -->
  <div class="banner">
    <div class="banner-body">
      <div class="banner-label">✈ AI 智能排线结果</div>
      <div class="banner-title">{city_name} {days_count} 日游</div>
      <div class="banner-stats">
        <div><div class="stat-label">景点总数</div><div class="stat-val">{total_places} 个</div></div>
        <div><div class="stat-label">行程天数</div><div class="stat-val">{days_count} 天</div></div>
        <div><div class="stat-label">排线算法</div><div class="stat-val">K-Means + TSP</div></div>
        <div><div class="stat-label">生成时间</div><div class="stat-val">{generated_at}</div></div>
      </div>
    </div>
    <div class="color-bar">{color_bars}</div>
  </div>

  <!-- 每日行程 -->
  {day_sections}

  <div class="footer">
    由 BreezeTravel AI 生成 · {generated_at} · 点击顶部「打印 / 保存为 PDF」可导出 PDF
  </div>

</body>
</html>"""
