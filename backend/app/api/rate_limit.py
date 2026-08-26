"""Redis-backed public-demo rate limiting with an explicit local fallback."""

import time
from collections import defaultdict, deque
from uuid import uuid4

from fastapi import HTTPException, Request

from app.config import settings

_windows: dict[str, deque[float]] = defaultdict(deque)
_redis = None


def _client_ip(request: Request) -> str:
    # Only trust a forwarding header when the deployment explicitly opts in.
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


async def _get_redis():
    global _redis
    if _redis is None:
        try:
            import redis.asyncio as aioredis
            candidate = aioredis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=1)
            await candidate.ping()
            _redis = candidate
        except Exception:
            _redis = False
    return _redis or None


async def _redis_allowed(client, key: str, limit: int, now_ms: int) -> bool:
    """Atomic sorted-set sliding window; entries expire even when traffic stops."""
    window_ms = 60_000
    member = f"{now_ms}:{uuid4().hex}"
    script = """
local key, now, window, limit, member = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count >= limit then return 0 end
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, window)
return 1
"""
    return bool(await client.eval(script, 1, key, now_ms, window_ms, limit, member))


def _memory_allowed(key: str, limit: int, now: float) -> bool:
    bucket = _windows[key]
    while bucket and bucket[0] <= now - 60:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


async def check_public_chat_limit(request: Request) -> None:
    if not settings.public_demo_mode:
        return
    ip = _client_ip(request)
    limit = settings.public_demo_chat_requests_per_minute
    client = await _get_redis()
    try:
        allowed = await _redis_allowed(client, f"breezetravel:rate:chat:{ip}", limit, int(time.time() * 1000)) if client else _memory_allowed(ip, limit, time.monotonic())
    except Exception:
        allowed = _memory_allowed(ip, limit, time.monotonic())
    if not allowed:
        raise HTTPException(status_code=429, detail="公开演示请求过于频繁，请稍后再试")
