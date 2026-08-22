"""Single-use, loopback-only cleanup hook for the controlled restart gate."""

import asyncio
import hashlib
import hmac
import ipaddress

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from app.config import settings
from app.db.connection import get_pool

router = APIRouter()
_cleanup_lock = asyncio.Lock()
_cleanup_secret_consumed = False


class CleanupRequest(BaseModel):
    room_id: str | None = None
    room_ids: list[str] = Field(default_factory=list, max_length=12)
    emails: list[str] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_targets(self) -> "CleanupRequest":
        targets = ([self.room_id] if self.room_id else []) + self.room_ids
        if not targets:
            raise ValueError("at least one room_id is required")
        if len(targets) != len(set(targets)):
            raise ValueError("room cleanup targets must be unique")
        if len(self.emails) != len(set(self.emails)):
            raise ValueError("email cleanup targets must be unique")
        return self

    @property
    def target_room_ids(self) -> list[str]:
        return ([self.room_id] if self.room_id else []) + self.room_ids


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _secret_matches(supplied: str) -> bool:
    expected = settings.e2e_cleanup_secret
    if len(expected) < 32 or len(supplied) < 32:
        return False
    # Hash both sides first so compare_digest always receives equal-length
    # buffers and neither a prefix nor the supplied length affects timing.
    return hmac.compare_digest(
        hashlib.sha256(supplied.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


@router.post("/e2e/cleanup")
async def cleanup(
    body: CleanupRequest,
    request: Request,
    x_e2e_cleanup_secret: str = Header(default=""),
):
    if (
        not settings.e2e_restart_gate_mode
        or settings.runtime_profile != "local_fixture"
        or not _is_loopback(request)
        or not _secret_matches(x_e2e_cleanup_secret)
    ):
        raise HTTPException(status_code=404, detail="not found")
    if any(not room_id.startswith("e2e-") for room_id in body.target_room_ids) or any(
        not email.startswith("e2e+") for email in body.emails
    ):
        raise HTTPException(status_code=400, detail="only isolated E2E data may be removed")
    global _cleanup_secret_consumed
    async with _cleanup_lock:
        if _cleanup_secret_consumed:
            raise HTTPException(status_code=404, detail="not found")
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM rooms WHERE room_id = ANY($1::text[])",
                body.target_room_ids,
            )
            if body.emails:
                await conn.execute("DELETE FROM users WHERE email = ANY($1::text[])", body.emails)
        _cleanup_secret_consumed = True
    return {"ok": True, "room_count": len(body.target_room_ids), "email_count": len(body.emails)}
