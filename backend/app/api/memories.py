from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.db.connection import get_pool
from app.memory.governance import (
    delete_memory, list_memories, memory_enabled, set_memory_enabled, update_memory,
)
from app.schemas.memory import MemoryRecord, MemorySettingsRequest, MemorySettingsResponse, MemoryUpdateRequest
from app.utils.auth import get_current_user


router = APIRouter()


@router.get("/user/memories", response_model=list[MemoryRecord])
async def get_memories(user_id: str = Depends(get_current_user)):
    return [MemoryRecord.model_validate(dict(row)) for row in await list_memories(user_id, await get_pool())]


@router.patch("/user/memories/{memory_id}", response_model=MemoryRecord)
async def patch_memory(memory_id: UUID, body: MemoryUpdateRequest, user_id: str = Depends(get_current_user)):
    try:
        row = await update_memory(user_id, memory_id, body.model_dump(exclude_unset=True), await get_pool())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return MemoryRecord.model_validate(dict(row))


@router.delete("/user/memories/{memory_id}", status_code=204)
async def remove_memory(memory_id: UUID, user_id: str = Depends(get_current_user)):
    if not await delete_memory(user_id, memory_id, await get_pool()):
        raise HTTPException(status_code=404, detail="memory not found")


@router.post("/user/memory-settings", response_model=MemorySettingsResponse)
async def update_memory_settings(body: MemorySettingsRequest, user_id: str = Depends(get_current_user)):
    row = await set_memory_enabled(user_id, body.enabled, await get_pool())
    return MemorySettingsResponse.model_validate(dict(row))


@router.get("/user/memory-settings", response_model=MemorySettingsResponse)
async def read_memory_settings(user_id: str = Depends(get_current_user)):
    enabled = await memory_enabled(user_id, await get_pool())
    return MemorySettingsResponse(enabled=enabled)
