"""Central room authorization boundary used by HTTP, SSE and Yjs tokens."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from fastapi import HTTPException, status

from app.db.connection import get_pool


class RoomPermission(str, Enum):
    READ = "room:read"
    WRITE = "room:write"
    OPTIMIZE = "itinerary:write"
    EXPORT = "itinerary:export"


@dataclass(frozen=True)
class RoomAccess:
    room_id: str
    thread_id: str
    user_id: str
    role: str


async def require_room_member(
    room_id: str,
    user_id: str,
    *,
    thread_id: Optional[str] = None,
    pool=None,
) -> RoomAccess:
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    pool = pool or await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT r.room_id, r.thread_id, COALESCE(rm.role, 'member') AS role
            FROM rooms r
            JOIN room_members rm ON rm.room_id = r.room_id
            WHERE r.room_id = $1 AND rm.user_id = $2
            """,
            room_id,
            user_id,
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="不是该房间成员")
    if thread_id is not None and row["thread_id"] != thread_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="thread_id 不属于该房间")
    return RoomAccess(room_id=row["room_id"], thread_id=row["thread_id"], user_id=user_id, role=row["role"])


def reject_claimed_identity(claimed_user_id: Optional[str], authenticated_user_id: str) -> None:
    if claimed_user_id and claimed_user_id != authenticated_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请求体 user_id 不是可信身份")
