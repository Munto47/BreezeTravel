from __future__ import annotations

import hashlib
import json
from typing import Protocol


GEOMETRY_TTL_SECONDS = 24 * 60 * 60


class RouteGeometryCache(Protocol):
    async def put(self, points: list[dict[str, float]]) -> str | None: ...

    async def get(self, geometry_ref: str) -> list[dict[str, float]] | None: ...


def _geometry_ref(points: list[dict[str, float]]) -> str:
    payload = json.dumps(points, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"rg3_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:40]}"


class InMemoryRouteGeometryCache:
    def __init__(self) -> None:
        self._items: dict[str, list[dict[str, float]]] = {}

    async def put(self, points: list[dict[str, float]]) -> str | None:
        if len(points) < 2:
            return None
        reference = _geometry_ref(points)
        self._items[reference] = [dict(point) for point in points]
        return reference

    async def get(self, geometry_ref: str) -> list[dict[str, float]] | None:
        value = self._items.get(geometry_ref)
        return [dict(point) for point in value] if value is not None else None

    def expire(self, geometry_ref: str) -> None:
        self._items.pop(geometry_ref, None)


class RedisRouteGeometryCache:
    """Short-lived route geometry cache; failures degrade to route summaries."""

    def __init__(self, redis_url: str, *, fallback: InMemoryRouteGeometryCache | None = None) -> None:
        self.redis_url = redis_url
        self.fallback = fallback or InMemoryRouteGeometryCache()
        self._client = None
        self._unavailable = False

    async def _redis(self):
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
        return self._client

    async def put(self, points: list[dict[str, float]]) -> str | None:
        reference = await self.fallback.put(points)
        if reference is None:
            return None
        if self._unavailable:
            return reference
        try:
            client = await self._redis()
            await client.setex(
                f"trip-route-geometry:{reference}",
                GEOMETRY_TTL_SECONDS,
                json.dumps(points, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            self._unavailable = True
        return reference

    async def get(self, geometry_ref: str) -> list[dict[str, float]] | None:
        if self._unavailable:
            return await self.fallback.get(geometry_ref)
        try:
            client = await self._redis()
            raw = await client.get(f"trip-route-geometry:{geometry_ref}")
            if raw:
                value = json.loads(raw)
                if isinstance(value, list):
                    return [
                        {"longitude": float(point["longitude"]), "latitude": float(point["latitude"])}
                        for point in value
                        if isinstance(point, dict)
                        and "longitude" in point
                        and "latitude" in point
                    ]
        except Exception:
            self._unavailable = True
        return await self.fallback.get(geometry_ref)
