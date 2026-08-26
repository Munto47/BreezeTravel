from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

import httpx

from app.db.connection import get_pool


class WechatAuthError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class WechatSession:
    openid: str


@dataclass(frozen=True)
class WechatIdentity:
    user_id: str
    nickname: str
    is_new_user: bool


class WechatSessionProvider(Protocol):
    async def exchange(self, code: str) -> WechatSession: ...


class WechatIdentityRepository(Protocol):
    async def find_or_create(self, *, app_id: str, openid_hmac: str, nickname: str) -> WechatIdentity: ...


class HttpxWechatSessionProvider:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        endpoint: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.endpoint = endpoint
        self.transport = transport

    async def exchange(self, code: str) -> WechatSession:
        if not self.app_id or not self.app_secret:
            raise WechatAuthError(
                "WECHAT_AUTH_UNAVAILABLE",
                "微信登录尚未配置",
                status_code=503,
            )
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=5.0) as client:
                response = await client.get(
                    self.endpoint,
                    params={
                        "appid": self.app_id,
                        "secret": self.app_secret,
                        "js_code": code,
                        "grant_type": "authorization_code",
                    },
                )
        except httpx.HTTPError as exc:
            raise WechatAuthError(
                "WECHAT_AUTH_PROVIDER_UNAVAILABLE",
                "微信登录服务暂不可用",
                status_code=502,
            ) from exc
        if response.status_code != 200:
            raise WechatAuthError(
                "WECHAT_AUTH_PROVIDER_UNAVAILABLE",
                "微信登录服务暂不可用",
                status_code=502,
            )
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise WechatAuthError(
                "WECHAT_AUTH_PROVIDER_UNAVAILABLE",
                "微信登录服务返回无效响应",
                status_code=502,
            ) from exc
        openid = payload.get("openid")
        if not isinstance(openid, str) or not openid:
            if payload.get("errcode") in {40029, 45011, 40226, -1}:
                raise WechatAuthError(
                    "WECHAT_LOGIN_CODE_INVALID",
                    "微信登录凭证无效或已过期",
                    status_code=401,
                )
            raise WechatAuthError(
                "WECHAT_AUTH_PROVIDER_UNAVAILABLE",
                "微信登录服务未返回有效身份",
                status_code=502,
            )
        return WechatSession(openid=openid)


class PostgresWechatIdentityRepository:
    def __init__(self, pool: Any | None = None) -> None:
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def find_or_create(self, *, app_id: str, openid_hmac: str, nickname: str) -> WechatIdentity:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            lock_key = f"{app_id}:{openid_hmac}"
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", lock_key)
            row = await conn.fetchrow(
                """
                SELECT identity.user_id, users.nickname
                FROM wechat_identities AS identity
                JOIN users ON users.user_id = identity.user_id
                WHERE identity.app_id = $1 AND identity.openid_hmac = $2
                """,
                app_id,
                openid_hmac,
            )
            if row is not None:
                await conn.execute(
                    """
                    UPDATE wechat_identities SET last_login_at = NOW()
                    WHERE app_id = $1 AND openid_hmac = $2
                    """,
                    app_id,
                    openid_hmac,
                )
                return WechatIdentity(user_id=row["user_id"], nickname=row["nickname"], is_new_user=False)

            user_id = str(uuid4())
            await conn.execute(
                "INSERT INTO users (user_id, nickname, updated_at) VALUES ($1, $2, NOW())",
                user_id,
                nickname,
            )
            await conn.execute(
                """
                INSERT INTO wechat_identities (app_id, openid_hmac, user_id)
                VALUES ($1, $2, $3)
                """,
                app_id,
                openid_hmac,
                user_id,
            )
            return WechatIdentity(user_id=user_id, nickname=nickname, is_new_user=True)


class InMemoryWechatIdentityRepository:
    def __init__(self) -> None:
        self.identities: dict[tuple[str, str], WechatIdentity] = {}

    async def find_or_create(self, *, app_id: str, openid_hmac: str, nickname: str) -> WechatIdentity:
        key = (app_id, openid_hmac)
        existing = self.identities.get(key)
        if existing is not None:
            return WechatIdentity(existing.user_id, existing.nickname, False)
        identity = WechatIdentity(str(uuid4()), nickname, True)
        self.identities[key] = identity
        return identity


class WechatAuthService:
    def __init__(
        self,
        *,
        app_id: str,
        identity_hash_key: str,
        provider: WechatSessionProvider,
        repository: WechatIdentityRepository,
    ) -> None:
        self.app_id = app_id
        self.identity_hash_key = identity_hash_key
        self.provider = provider
        self.repository = repository

    async def login(self, *, code: str, nickname: str | None = None) -> WechatIdentity:
        if not self.app_id or not self.identity_hash_key:
            raise WechatAuthError(
                "WECHAT_AUTH_UNAVAILABLE",
                "微信登录尚未配置",
                status_code=503,
            )
        session = await self.provider.exchange(code)
        identity_hash = hmac.new(
            self.identity_hash_key.encode("utf-8"),
            session.openid.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        safe_nickname = (nickname or "微信旅行者").strip()[:20] or "微信旅行者"
        return await self.repository.find_or_create(
            app_id=self.app_id,
            openid_hmac=identity_hash,
            nickname=safe_nickname,
        )
