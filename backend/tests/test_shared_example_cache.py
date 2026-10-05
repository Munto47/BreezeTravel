import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from redis.exceptions import ConnectionError

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.models import PlaceResolutionOutcome, ResolvedPlace
from app.trip_understanding.shared_cache import SharedFactCache, prewarm_examples


class RedisMemory:
    def __init__(self):
        self.data = {}
        self.now = 0
        self.down = False
    async def get(self, key):
        if self.down:
            raise ConnectionError()
        value = self.data.get(key)
        return value[0] if value and value[1] > self.now else None
    async def set(self, key, value, *, ex, nx=False):
        if self.down:
            raise ConnectionError()
        if nx and await self.get(key):
            return False
        self.data[key] = (value, self.now + ex)
        return True
    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_success_cache_crosses_workers_expires_and_fails_open():
    redis = RedisMemory()
    calls = []
    def resolver():
        instance = AmapPlaceResolver(api_key="controlled", shared_cache=SharedFactCache("", client=redis), success_cache_size=0)
        async def query(**options):
            calls.append(options)
            return PlaceResolutionOutcome(place=ResolvedPlace(canonical_place_id="poi", name=options["atomic_place_name"],
                category="景点", area_or_address=options["city"], provider_binding={"external_calls": 1}),
                receipt={"external_calls": 1})
        instance._resolve_uncached = query
        return instance
    first, second = resolver(), resolver()
    query = dict(city="北京", atomic_place_name="景山公园", category_hint="景点")
    await first.resolve(**query)
    cached = await second.resolve(**query)
    assert len(calls) == 1 and cached.receipt["external_calls"] == 0
    cached.place.name = "用户编辑"
    assert (await second.resolve(**query)).place.name == "景山公园"
    await second.resolve(**{**query, "city": "深圳"})
    await second.resolve(**{**query, "category_hint": "餐饮"})
    assert len(calls) == 3
    redis.now = 901
    await second.resolve(**query)
    assert len(calls) == 4
    redis.down = True
    await second.resolve(**query)
    assert len(calls) == 5
    await first.aclose()
    await second.aclose()


@pytest.mark.asyncio
async def test_ambiguity_and_corrupt_entries_never_become_success_or_block_queries():
    redis = RedisMemory()
    cache = SharedFactCache("", client=redis)
    resolver = AmapPlaceResolver(api_key="controlled", shared_cache=cache)
    calls = []
    async def ambiguous(**query):
        calls.append(query)
        return PlaceResolutionOutcome(receipt={"status": "AMBIGUOUS", "external_calls": 1})
    resolver._resolve_uncached = ambiguous
    query = dict(city="深圳", atomic_place_name="大芬油画村", category_hint="景点")
    await asyncio.gather(resolver.resolve(**query), resolver.resolve(**query))
    assert len(calls) == 1 and not redis.data
    key = cache.key("amap-poi-v2-identity-v1", ("深圳", "大芬油画村", "景点", True))
    redis.data[key] = ('{"place":{}}', 900)
    assert (await resolver.resolve(**query)).place is None
    assert len(calls) == 2
    await resolver.aclose()


@pytest.mark.asyncio
async def test_startup_warming_is_deduplicated_and_does_not_repeat_when_redis_is_down():
    redis = RedisMemory()
    class Resolver:
        shared_cache = SharedFactCache("", client=redis)
        calls = []
        async def resolve(self, **query):
            self.calls.append(query)
    resolver = Resolver()
    await asyncio.gather(prewarm_examples(resolver, 4), prewarm_examples(resolver, 4))
    initial = len(resolver.calls)
    assert initial == 26
    await prewarm_examples(resolver, 4)
    redis.down = True
    await prewarm_examples(resolver, 4)
    assert len(resolver.calls) == initial


@pytest.mark.asyncio
async def test_routes_reuse_only_same_direction_mode_and_unexpired_fact():
    from app.trip_understanding.map_render import InternalRouteModeFact, MapStop
    now = datetime.now(timezone.utc)
    cache = SharedFactCache("", client=RedisMemory())
    provider = AmapRouteProvider(api_key="controlled", shared_cache=cache)
    calls = []
    async def route(origin, destination, mode, *, observed_at):
        calls.append((origin, destination, mode))
        return InternalRouteModeFact(mode=mode, status="AVAILABLE", duration_minutes=5, distance_meters=400,
            request_hash="a" * 64, response_hash="b" * 64, external_call_count=1,
            provider_binding={"external_calls": 1}, observed_at=observed_at, expires_at=observed_at + timedelta(minutes=5))
    provider._route_uncached = route
    a = MapStop(activity_token="a" * 24, day_label="Day 1", resolution_status="AUTO_MATCHED", name="甲", canonical_place_id="a",
                city="北京", longitude=116.4, latitude=39.9, day_index=1, sequence_index=0)
    b = a.model_copy(update={"canonical_place_id": "b", "longitude": 116.41})
    await provider.route(a, b, "walking", observed_at=now)
    assert (await provider.route(a, b, "walking", observed_at=now)).external_call_count == 0
    await provider.route(b, a, "walking", observed_at=now)
    await provider.route(a, b, "transit", observed_at=now)
    await provider.route(a, b, "walking", observed_at=now + timedelta(minutes=6))
    assert len(calls) == 4
