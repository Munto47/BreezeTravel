from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.db.connection import get_pool
from app.schemas.task_spec import TaskParseResult
from app.services.room_access import require_room_member
from app.services.task_spec_service import TaskSpecService
from app.utils.auth import get_current_user


router = APIRouter()


class ParseTaskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    default_city: str = ""
    default_days: int = Field(default=0, ge=0, le=30)
    start_date: Optional[date] = None


@router.post("/room/{room_id}/task/parse", response_model=TaskParseResult)
async def parse_room_task(room_id: str, body: ParseTaskRequest, user_id: str = Depends(get_current_user)):
    pool = await get_pool()
    await require_room_member(room_id, user_id, pool=pool)
    service = TaskSpecService()
    current = await service.load_latest(room_id, pool)
    result = service.parse(
        body.text,
        room_id=room_id,
        default_city=body.default_city,
        default_days=body.default_days,
        current_revision=current.task_revision if current else 0,
        start_date=body.start_date,
    )
    await service.save(result.task_spec, pool)
    return result
