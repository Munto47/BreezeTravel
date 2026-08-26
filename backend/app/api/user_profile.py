"""
用户个人信息接口（JWT 鉴权）。

GET  /api/user/me           — 当前用户信息（手机号脱敏）
PUT  /api/user/profile      — 更新昵称/头像/生日
GET  /api/user/rooms        — 用户参与过的所有房间
GET  /api/user/itineraries  — 用户已保存路线列表
"""

from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.db.connection import get_pool
from app.utils.auth import get_current_user

router = APIRouter()


def _mask_phone(phone: Optional[str]) -> Optional[str]:
    if not phone or len(phone) < 7:
        return phone
    return phone[:3] + "****" + phone[-4:]


class ProfileUpdateRequest(BaseModel):
    nickname: Optional[str] = None
    avatar_url: Optional[str] = None
    birthday: Optional[str] = None  # ISO date string: "1995-08-15"


@router.get("/user/me")
async def get_me(user_id: str = Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, nickname, phone, avatar_url, birthday, created_at FROM users WHERE user_id = $1",
            user_id,
        )
    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="用户不存在")

    return {
        "user_id": row["user_id"],
        "nickname": row["nickname"],
        "phone": _mask_phone(row["phone"]),
        "avatar_url": row["avatar_url"],
        "birthday": row["birthday"].isoformat() if row["birthday"] else None,
        "created_at": row["created_at"].isoformat(),
    }


@router.put("/user/profile")
async def update_profile(body: ProfileUpdateRequest, user_id: str = Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 只更新传入的字段
        updates = []
        params = []
        idx = 1
        if body.nickname is not None:
            updates.append(f"nickname = ${idx}")
            params.append(body.nickname.strip() or "旅行者")
            idx += 1
        if body.avatar_url is not None:
            updates.append(f"avatar_url = ${idx}")
            params.append(body.avatar_url)
            idx += 1
        if body.birthday is not None:
            updates.append(f"birthday = ${idx}")
            params.append(body.birthday or None)
            idx += 1
        if not updates:
            return {"ok": True}

        updates.append("updated_at = NOW()")
        params.append(user_id)
        await conn.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE user_id = ${idx}",
            *params,
        )
    return {"ok": True}


@router.get("/user/rooms")
async def get_user_rooms(user_id: str = Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.room_id, r.thread_id, r.trip_city, r.trip_days, r.phase, r.created_at,
                   COUNT(DISTINCT rp.place_id) AS place_count,
                   COUNT(DISTINCT si.id)        AS itinerary_count
            FROM rooms r
            LEFT JOIN room_places rp ON rp.room_id = r.room_id
            LEFT JOIN saved_itineraries si ON si.room_id = r.room_id
            WHERE r.room_id IN (
                SELECT room_id FROM room_members WHERE user_id = $1
                UNION
                SELECT room_id FROM saved_itineraries WHERE user_id = $1
            )
            GROUP BY r.room_id
            ORDER BY r.created_at DESC
            LIMIT 30
            """,
            user_id,
        )
    return [
        {
            "room_id": r["room_id"],
            "thread_id": r["thread_id"],
            "city": r["trip_city"] or "未知目的地",
            "trip_days": r["trip_days"] or 3,
            "phase": r["phase"] or "exploring",
            "place_count": r["place_count"],
            "itinerary_count": r["itinerary_count"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@router.get("/user/itineraries")
async def get_user_itineraries(user_id: str = Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, room_id, city, trip_days, created_at
            FROM saved_itineraries
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 50
            """,
            user_id,
        )
    return [
        {
            "id": str(r["id"]),
            "room_id": r["room_id"],
            "city": r["city"],
            "trip_days": r["trip_days"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]
