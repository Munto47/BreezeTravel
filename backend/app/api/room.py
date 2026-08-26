"""
房间与用户管理接口

GET  /api/room/{room_id}/state   - 房间状态查询
POST /api/room                   - 创建房间
POST /api/room/{room_id}/join    - 加入房间
POST /api/user                   - 注册/更新用户昵称
GET  /api/user/{user_id}         - 获取用户信息

房间元数据持久化到 PostgreSQL rooms 表（schema 见 db/init.sql）。
用户信息持久化到 users 表，room_members 表记录成员关系。
place_count 字段由 Yjs 层管理，后端固定返回 0。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.db.connection import get_pool
from app.schemas.api import RoomStateResponse
from app.utils.auth import create_room_token, get_current_user, get_optional_user
from app.config import get_settings
from app.services.room_access import reject_claimed_identity, require_room_member

router = APIRouter()


def _resolved_user(current_user: Optional[str], claimed_user: Optional[str] = None) -> str:
    if current_user:
        reject_claimed_identity(claimed_user, current_user)
        return current_user
    if get_settings().demo_mode:
        return claimed_user or "demo-user"
    raise HTTPException(status_code=401, detail="请先登录")


# =============================================
# 用户相关
# =============================================

class UpsertUserRequest(BaseModel):
    user_id: str
    nickname: str


class UserResponse(BaseModel):
    user_id: str
    nickname: str


@router.post("/user", response_model=UserResponse)
async def upsert_user(body: UpsertUserRequest, current_user: Optional[str] = Depends(get_optional_user)):
    """注册或更新用户昵称（幂等，前端每次启动时调用）"""
    current_user = _resolved_user(current_user, body.user_id)
    if not body.user_id or not body.nickname:
        raise HTTPException(status_code=400, detail="user_id 和 nickname 必填")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (user_id, nickname, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE
              SET nickname = EXCLUDED.nickname, updated_at = NOW()
            RETURNING user_id, nickname
            """,
            body.user_id,
            body.nickname.strip() or "旅行者",
        )
    return UserResponse(**dict(row))


@router.get("/user/{user_id}", response_model=UserResponse)
async def get_user(user_id: str, current_user: Optional[str] = Depends(get_optional_user)):
    """获取用户信息"""
    current_user = _resolved_user(current_user, user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, nickname FROM users WHERE user_id = $1",
            user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail=f"用户 {user_id} 不存在")
    return UserResponse(**dict(row))


# =============================================
# 房间相关
# =============================================

@router.get("/room/{room_id}/state", response_model=RoomStateResponse)
async def get_room_state(room_id: str, user_id: Optional[str] = Depends(get_optional_user)):
    """获取房间状态元数据"""
    pool = await get_pool()
    if not get_settings().demo_mode:
        await require_room_member(room_id, _resolved_user(user_id), pool=pool)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT room_id, thread_id, phase, trip_city, trip_days FROM rooms WHERE room_id = $1",
            room_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail=f"房间 {room_id} 不存在")
    return RoomStateResponse(**dict(row), place_count=0)


@router.get("/room/{room_id}/members")
async def get_room_members(room_id: str, user_id: Optional[str] = Depends(get_optional_user)):
    """获取房间成员列表（持久化部分，Yjs awareness 的补充）"""
    pool = await get_pool()
    if not get_settings().demo_mode:
        await require_room_member(room_id, _resolved_user(user_id), pool=pool)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id, u.nickname, rm.joined_at
            FROM room_members rm
            JOIN users u ON u.user_id = rm.user_id
            WHERE rm.room_id = $1
            ORDER BY rm.joined_at ASC
            """,
            room_id,
        )
    return [{"userId": r["user_id"], "nickname": r["nickname"], "joinedAt": r["joined_at"].isoformat()} for r in rows]


class CreateRoomRequest(BaseModel):
    room_id: str
    thread_id: str
    trip_city: Optional[str] = None
    trip_days: int = 3
    user_id: Optional[str] = None
    nickname: Optional[str] = None


@router.post("/room")
async def create_room(body: CreateRoomRequest, current_user: Optional[str] = Depends(get_optional_user)):
    """
    创建新房间（幂等）。
    同时注册房间创建者到 users 表和 room_members 表。

    请求体：{room_id, thread_id, trip_city?, trip_days?, user_id?, nickname?}
    """
    room_id = body.room_id
    thread_id = body.thread_id
    user_id = _resolved_user(current_user, body.user_id)
    nickname = body.nickname

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 创建房间（幂等）
        await conn.execute(
            """
            INSERT INTO rooms (room_id, thread_id, trip_city, trip_days, phase)
            VALUES ($1, $2, $3, $4, 'exploring')
            ON CONFLICT (room_id) DO NOTHING
            """,
            room_id,
            thread_id,
            body.trip_city,
            body.trip_days,
        )

        # 注册创建者（如果提供了 user_id）
        if user_id:
            await conn.execute(
                """
                INSERT INTO users (user_id, nickname, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (user_id) DO UPDATE
                  SET nickname = EXCLUDED.nickname, updated_at = NOW()
                """,
                user_id,
                (nickname or "旅行者").strip() or "旅行者",
            )
            await conn.execute(
                """
                INSERT INTO room_members (room_id, user_id, role)
                VALUES ($1, $2, 'owner')
                ON CONFLICT DO NOTHING
                """,
                room_id,
                user_id,
            )

    return {"status": "ok", "room_id": room_id}


class JoinRoomRequest(BaseModel):
    user_id: Optional[str] = None
    nickname: Optional[str] = "旅行者"


@router.post("/room/{room_id}/join")
async def join_room(room_id: str, body: JoinRoomRequest, current_user: Optional[str] = Depends(get_optional_user)):
    """
    加入房间（记录到 room_members 表，关键操作：返回 thread_id + 城市 + 天数）。

    请求体：{user_id, nickname}
    """
    user_id = _resolved_user(current_user, body.user_id)
    nickname = (body.nickname or "旅行者").strip() or "旅行者"

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 核心操作：查询房间（必须成功）
        room = await conn.fetchrow(
            "SELECT thread_id, trip_city, trip_days FROM rooms WHERE room_id = $1",
            room_id,
        )
        if not room:
            raise HTTPException(status_code=404, detail=f"房间 {room_id} 不存在")

        # 非核心操作：注册用户 + 加入记录（失败不阻断）
        try:
            await conn.execute(
                """
                INSERT INTO users (user_id, nickname, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (user_id) DO UPDATE
                  SET nickname = EXCLUDED.nickname, updated_at = NOW()
                """,
                user_id,
                nickname,
            )
            await conn.execute(
                """
                INSERT INTO room_members (room_id, user_id, role)
                VALUES ($1, $2, 'member')
                ON CONFLICT DO NOTHING
                """,
                room_id,
                user_id,
            )
        except Exception as e:
            # 用户/成员表可能不存在（旧数据库），不影响加入房间
            print(f"[JoinRoom] 用户记录写入失败（非致命）：{e}")

    return {
        "status": "ok",
        "room_id": room_id,
        "thread_id": room["thread_id"],
        "trip_city": room["trip_city"],
        "trip_days": room["trip_days"],
    }


@router.post("/room/{room_id}/ws-token")
async def issue_ws_token(room_id: str, user_id: str = Depends(get_current_user)):
    """Issue a short-lived room-bound token; the raw JWT is never logged."""
    access = await require_room_member(room_id, user_id)
    return {
        "token": create_room_token(user_id, room_id),
        "room_id": access.room_id,
        "expires_in_seconds": 300,
    }
