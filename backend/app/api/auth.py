"""
手机号 + 短信验证码登录/注册接口。

POST /api/auth/send-code  — 发送验证码（1 分钟防刷）
POST /api/auth/verify     — 验证码验证 + 登录/注册，返回 JWT
"""

import random
import re
import string
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.authentication.wechat import (
    HttpxWechatSessionProvider,
    PostgresWechatIdentityRepository,
    WechatAuthError,
    WechatAuthService,
)
from app.config import settings
from app.db.connection import get_pool
from app.utils.sms import send_code
from app.utils.auth import create_token
from app.utils.password import hash_password, verify_password

router = APIRouter()


def get_wechat_auth_service() -> WechatAuthService:
    return WechatAuthService(
        app_id=settings.wechat_miniprogram_app_id,
        identity_hash_key=settings.wechat_identity_hash_key,
        provider=HttpxWechatSessionProvider(
            app_id=settings.wechat_miniprogram_app_id,
            app_secret=settings.wechat_miniprogram_app_secret,
            endpoint=settings.wechat_code2session_url,
        ),
        repository=PostgresWechatIdentityRepository(),
    )


WechatAuthServiceDep = Annotated[WechatAuthService, Depends(get_wechat_auth_service)]

# 邮箱兜底登录密码强度：≥8 位，含字母 + 数字（不强制特殊字符以降低注册阻力）
_PASSWORD_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{8,64}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _validate_password(pw: str) -> None:
    if not _PASSWORD_RE.match(pw or ""):
        raise HTTPException(
            status_code=400,
            detail="密码需要 8-64 位，且至少包含 1 个字母和 1 个数字",
        )


def _normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not _EMAIL_RE.match(e):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    return e


def _gen_code(length: int = 6) -> str:
    if settings.dev_login_bypass:
        return settings.dev_login_code
    return "".join(random.choices(string.digits, k=length))


class SendCodeRequest(BaseModel):
    phone: str


class VerifyRequest(BaseModel):
    phone: str
    code: str
    nickname: Optional[str] = None


class WechatLoginRequest(BaseModel):
    code: str
    nickname: Optional[str] = None


class WechatLoginResponse(BaseModel):
    token: str
    user_id: str
    nickname: str
    is_new_user: bool


@router.post("/auth/wechat/login", response_model=WechatLoginResponse)
async def wechat_login(body: WechatLoginRequest, service: WechatAuthServiceDep):
    code = body.code.strip()
    if not code or len(code) > 256:
        raise HTTPException(
            status_code=400,
            detail={"code": "WECHAT_LOGIN_CODE_INVALID", "message": "微信登录凭证格式无效"},
        )
    try:
        identity = await service.login(code=code, nickname=body.nickname)
    except WechatAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
    return WechatLoginResponse(
        token=create_token(identity.user_id),
        user_id=identity.user_id,
        nickname=identity.nickname,
        is_new_user=identity.is_new_user,
    )


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

        # 日级配额自查，避免触发运营商日级流控
        if not settings.dev_login_bypass:
            daily = await conn.fetchval(
                """
                SELECT COUNT(*) FROM sms_verifications
                WHERE phone = $1 AND created_at > NOW() - INTERVAL '24 hours'
                """,
                phone,
            )
            if daily and daily >= settings.sms_daily_limit_per_phone:
                raise HTTPException(
                    status_code=429,
                    detail=f"今日已发送 {daily} 次，已达 {settings.sms_daily_limit_per_phone} 次上限，请明日再试",
                )

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

    if settings.dev_login_bypass:
        print(f"[Auth DEV BYPASS] {phone} -> code={code}", flush=True)
        return {"ok": True, "dev_bypass": True}

    ok = await send_code(phone, code)
    if not ok:
        raise HTTPException(status_code=502, detail="短信发送失败，请稍后重试")

    return {"ok": True}


@router.post("/auth/test-login")
async def test_login():
    """测试账号一键登录。仅在 settings.dev_login_bypass=true 时启用。

    生产环境强制返回 403，避免泄露。本地/演示环境用于快速进入主流程，无需验证码。
    """
    if not settings.dev_login_bypass:
        raise HTTPException(status_code=403, detail="测试账号仅在开发模式下可用")

    import uuid
    phone = settings.test_account_phone
    nickname = settings.test_account_nickname

    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT user_id, nickname FROM users WHERE phone = $1",
            phone,
        )
        is_new_user = user is None
        if is_new_user:
            user_id = str(uuid.uuid4())
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
        "phone": phone,
    }


class EmailRegisterRequest(BaseModel):
    email: str
    password: str
    nickname: Optional[str] = None


class EmailLoginRequest(BaseModel):
    email: str
    password: str


@router.post("/auth/email-register")
async def email_register(body: EmailRegisterRequest):
    """邮箱+密码注册。作为短信兜底通道：运营商故障或日级流控触顶时仍可创建账号。"""
    email = _normalize_email(body.email)
    _validate_password(body.password)
    nickname = (body.nickname or "").strip() or email.split("@")[0]
    password_hash = hash_password(body.password)

    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval("SELECT 1 FROM users WHERE email = $1", email)
        if existing:
            raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录")

        import uuid
        user_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO users (user_id, nickname, email, password_hash, updated_at)
            VALUES ($1, $2, $3, $4, NOW())
            """,
            user_id,
            nickname,
            email,
            password_hash,
        )

    token = create_token(user_id)
    return {
        "token": token,
        "user_id": user_id,
        "nickname": nickname,
        "is_new_user": True,
        "email": email,
    }


@router.post("/auth/email-login")
async def email_login(body: EmailLoginRequest):
    """邮箱+密码登录。失败统一返回 401 + 模糊文案，避免邮箱枚举。"""
    email = _normalize_email(body.email)
    if not body.password:
        raise HTTPException(status_code=400, detail="密码不能为空")

    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT user_id, nickname, password_hash FROM users WHERE email = $1",
            email,
        )

    if not user or not user["password_hash"]:
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    if not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")

    token = create_token(user["user_id"])
    return {
        "token": token,
        "user_id": user["user_id"],
        "nickname": user["nickname"],
        "is_new_user": False,
        "email": email,
    }


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
