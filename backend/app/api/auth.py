"""
手机号 + 短信验证码登录/注册接口。

POST /api/auth/send-code  — 发送验证码（1 分钟防刷）
POST /api/auth/verify     — 验证码验证 + 登录/注册，返回 JWT
"""

import random
import string
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.db.connection import get_pool
from app.utils.sms import send_code
from app.utils.auth import create_token

router = APIRouter()


def _gen_code(length: int = 6) -> str:
    return "".join(random.choices(string.digits, k=length))


class SendCodeRequest(BaseModel):
    phone: str


class VerifyRequest(BaseModel):
    phone: str
    code: str
    nickname: Optional[str] = None


@router.post("/auth/send-code")
async def send_verification_code(body: SendCodeRequest):
    """发送短信验证码（每分钟限 1 次）。"""
    phone = body.phone.strip()
    if not phone or len(phone) < 11:
        raise HTTPException(status_code=400, detail="手机号格式不正确")

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1 分钟内不重复发送
        recent = await conn.fetchval(
            """
            SELECT COUNT(*) FROM sms_verifications
            WHERE phone = $1 AND used = FALSE AND created_at > NOW() - INTERVAL '60 seconds'
            """,
            phone,
        )
        if recent:
            raise HTTPException(status_code=429, detail="发送过于频繁，请 60 秒后重试")

        code = _gen_code()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        await conn.execute(
            """
            INSERT INTO sms_verifications (phone, code, expires_at)
            VALUES ($1, $2, $3)
            """,
            phone,
            code,
            expires_at,
        )

    ok = await send_code(phone, code)
    if not ok:
        raise HTTPException(status_code=502, detail="短信发送失败，请稍后重试")

    return {"ok": True}


@router.post("/auth/verify")
async def verify_code(body: VerifyRequest):
    """
    验证短信验证码，成功后登录或自动注册，返回 JWT。
    is_new_user=true 时前端应引导用户设置昵称。
    """
    phone = body.phone.strip()
    code = body.code.strip()

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, used, expires_at FROM sms_verifications
            WHERE phone = $1 AND code = $2
            ORDER BY created_at DESC
            LIMIT 1
            """,
            phone,
            code,
        )
        if not row:
            raise HTTPException(status_code=400, detail="验证码错误")
        if row["used"]:
            raise HTTPException(status_code=400, detail="验证码已使用")
        if row["expires_at"] < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="验证码已过期")

        # 标记已使用
        await conn.execute(
            "UPDATE sms_verifications SET used = TRUE WHERE id = $1",
            row["id"],
        )

        # 查或建用户
        user = await conn.fetchrow(
            "SELECT user_id, nickname FROM users WHERE phone = $1",
            phone,
        )
        is_new_user = user is None
        if is_new_user:
            import uuid
            user_id = str(uuid.uuid4())
            nickname = (body.nickname or "旅行者").strip() or "旅行者"
            await conn.execute(
                """
                INSERT INTO users (user_id, nickname, phone, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (user_id) DO NOTHING
                """,
                user_id,
                nickname,
                phone,
            )
        else:
            user_id = user["user_id"]
            nickname = user["nickname"]

    token = create_token(user_id)
    return {
        "token": token,
        "user_id": user_id,
        "nickname": nickname,
        "is_new_user": is_new_user,
    }
