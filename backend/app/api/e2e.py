"""Narrow cleanup hook for isolated public smoke data."""

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.db.connection import get_pool

router = APIRouter()


class CleanupRequest(BaseModel):
    room_id: str
    emails: list[str] = []


@router.post("/e2e/cleanup")
async def cleanup(body: CleanupRequest, x_e2e_cleanup_secret: str = Header(default="")):
    if not settings.e2e_cleanup_secret or x_e2e_cleanup_secret != settings.e2e_cleanup_secret:
        raise HTTPException(status_code=404, detail="not found")
    if not body.room_id.startswith("e2e-") or any(not email.startswith("e2e+") for email in body.emails):
        raise HTTPException(status_code=400, detail="only isolated E2E data may be removed")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM rooms WHERE room_id = $1", body.room_id)
        if body.emails:
            await conn.execute("DELETE FROM users WHERE email = ANY($1::text[])", body.emails)
    return {"ok": True}
