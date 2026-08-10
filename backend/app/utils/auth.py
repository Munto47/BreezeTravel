"""JWT 工具：生成 / 验证 token，FastAPI 依赖注入。"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Header, HTTPException, status

from app.config import settings

_ALGORITHM = "HS256"
_EXPIRE_DAYS = 30


def create_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=_EXPIRE_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=_ALGORITHM)


def verify_token(token: str) -> str:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[_ALGORITHM])
        user_id: Optional[str] = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效 token")
        return user_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token 已过期，请重新登录")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效 token")


def create_room_token(user_id: str, room_id: str, expires_minutes: int = 5) -> str:
    """Mint a short-lived token exclusively for a Yjs room handshake."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": user_id,
            "room_id": room_id,
            "scope": ["room:read", "room:write", "yjs:connect"],
            "token_type": "room_ws",
            "aud": "breezetravel-yjs",
            "iat": now,
            "exp": now + timedelta(minutes=max(1, min(expires_minutes, 15))),
        },
        settings.jwt_secret_key,
        algorithm=_ALGORITHM,
    )


def verify_room_token(token: str, room_id: str) -> dict:
    """Validate the same room-bound contract enforced by y-websocket."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[_ALGORITHM],
            audience="breezetravel-yjs",
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="room token 已过期") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效 room token") from exc
    if claims.get("token_type") != "room_ws" or claims.get("room_id") != room_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="room token 与房间不匹配")
    scopes = claims.get("scope") or []
    if "yjs:connect" not in scopes:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="room token 缺少 yjs 权限")
    return claims


async def get_current_user(authorization: str = Header(default="")) -> str:
    """FastAPI 依赖注入：从 Authorization: Bearer <token> 中提取 user_id。"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    token = authorization.removeprefix("Bearer ").strip()
    return verify_token(token)


async def get_optional_user(authorization: str = Header(default="")) -> Optional[str]:
    """可选鉴权：有 token 则验证，无 token 返回 None（不抛错）。"""
    if not authorization.startswith("Bearer "):
        return None
    try:
        token = authorization.removeprefix("Bearer ").strip()
        return verify_token(token)
    except HTTPException:
        return None
