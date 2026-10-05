"""Best-effort shared provider facts; never authoritative itinerary storage."""
import asyncio
import hashlib
import json

from redis.asyncio import Redis
from redis.exceptions import RedisError


class SharedFactCache:
    def __init__(self, url, *, client=None):
        self.client = client or Redis.from_url(url, socket_timeout=.25, socket_connect_timeout=.25)

    @staticmethod
    def key(namespace, query):
        digest = hashlib.sha256(json.dumps(query, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return f"trip-facts:{namespace}:{digest}"

    async def get(self, namespace, query):
        try:
            async with asyncio.timeout(.3):
                raw = await self.client.get(self.key(namespace, query))
            return json.loads(raw) if raw else None
        except (RedisError, OSError, TimeoutError, ValueError, TypeError):
            return None

    async def put(self, namespace, query, value, *, ttl=900):
        if ttl <= 0:
            return
        try:
            async with asyncio.timeout(.3):
                await self.client.set(self.key(namespace, query), json.dumps(value), ex=max(1, int(ttl)))
        except (RedisError, OSError, TimeoutError):
            pass

    async def claim(self, namespace, query, *, ttl=900):
        try:
            async with asyncio.timeout(.3):
                return bool(await self.client.set(self.key(namespace, query), "1", nx=True, ex=ttl))
        except (RedisError, OSError, TimeoutError):
            # Without a shared claim, skip prewarming; ordinary queries still work.
            return False

    async def aclose(self):
        await self.client.aclose()


async def prewarm_examples(resolver, concurrency):
    from app.trip_understanding.example_preprocessing import catalog, seed_plan
    cache = getattr(resolver, "shared_cache", None)
    if cache is None or not await cache.claim("example-warm-v1", [(e["id"], e["version"]) for e in catalog()]):
        return
    queries = set()
    for example in catalog():
        _, plan = seed_plan(example["id"], example["version"])
        for mention in plan.mentions:
            if mention.role in {"PLANNED", "OPTIONAL"} and not mention.parent_mention_id and mention.city_hint:
                queries.add((mention.city_hint, mention.atomic_place_name, mention.category_hint))
    slots = asyncio.Semaphore(concurrency)
    async def warm(query):
        async with slots:
            await resolver.resolve(city=query[0], atomic_place_name=query[1], category_hint=query[2])
    await asyncio.gather(*(warm(query) for query in sorted(queries)), return_exceptions=True)
