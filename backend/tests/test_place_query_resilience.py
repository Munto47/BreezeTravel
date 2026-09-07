"""Controlled provider responses verify cost, isolation and identity boundaries."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.trip_understanding import amap_place, candidates
from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.city_knowledge import CityEntity, CityKnowledge
from app.trip_understanding.errors import PlaceProviderUnavailableError
from tests.test_amap_trip_understanding import _poi


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["transport", 502, 503, 504])
async def test_temporary_failure_retries_once_and_counts_actual_requests(first):
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            if first == "transport":
                raise httpx.ConnectError("private-test-key must not be retained", request=request)
            return httpx.Response(first)
        return httpx.Response(200, json={"status": "1", "pois": [_poi()]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await AmapPlaceResolver(api_key="private-test-key", client=client).resolve(city="北京", atomic_place_name="故宫博物院")
    assert outcome.place is not None
    assert outcome.receipt["external_calls"] == len(requests) == 2
    assert outcome.receipt["retry_count"] == 1
    assert "private-test-key" not in str(outcome.receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    (429, {}), (401, {}), (200, {"status": "0", "infocode": "10003"}),
    (200, {"status": "1", "pois": "invalid"}),
])
async def test_quota_auth_and_invalid_payload_do_not_retry_or_poison_later_request(response):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(response[0], json=response[1]) if len(calls) == 1 else httpx.Response(200, json={"status": "1", "pois": [_poi()]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = AmapPlaceResolver(api_key="test", client=client)
        with pytest.raises(PlaceProviderUnavailableError) as caught:
            await resolver.resolve(city="北京", atomic_place_name="故宫博物院")
        assert caught.value.external_call_count == len(calls) == 1
        assert (await resolver.resolve(city="北京", atomic_place_name="故宫博物院")).place
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_shared_failure_has_one_owner_for_http_cost_and_next_request_recovers():
    release, started = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request)
        started.set()
        await release.wait()
        return httpx.Response(429)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = AmapPlaceResolver(api_key="test", client=client)
        first = asyncio.create_task(resolver.resolve(city="北京", atomic_place_name="故宫博物院"))
        await started.wait()
        second = asyncio.create_task(resolver.resolve(city="北京", atomic_place_name="故宫博物院"))
        await asyncio.sleep(0)
        release.set()
        failures = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(f, PlaceProviderUnavailableError) for f in failures)
        assert sum(f.external_call_count for f in failures) == len(calls) == 1
        assert failures[1].provider_binding["cache_reuse"] == "INFLIGHT_COALESCED"
        with pytest.raises(PlaceProviderUnavailableError):
            await resolver.resolve(city="北京", atomic_place_name="故宫博物院")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_deadline_cancels_slow_request_without_starting_fake_retry():
    cancelled, calls = asyncio.Event(), []
    async def handler(request):
        calls.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = AmapPlaceResolver(api_key="test", client=client, deadline_seconds=0.025)
        with pytest.raises(PlaceProviderUnavailableError) as caught:
            await resolver.resolve(city="北京", atomic_place_name="故宫博物院")
    assert caught.value.category == "DEADLINE_EXCEEDED"
    assert caught.value.external_call_count == len(calls) == 1
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_success_cache_expires_is_bounded_and_returns_independent_copies(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(amap_place, "time", SimpleNamespace(monotonic=lambda: clock[0], perf_counter=amap_place.time.perf_counter))
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "1", "pois": [_poi(name=request.url.params["keywords"])]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = AmapPlaceResolver(api_key="test", client=client, success_cache_seconds=10, success_cache_size=1)
        first = await resolver.resolve(city="北京", atomic_place_name="故宫博物院")
        first.place.name = "caller changed its copy"
        reused = await resolver.resolve(city="北京", atomic_place_name="故宫博物院")
        assert reused.place.name == "故宫博物院" and reused.receipt["external_calls"] == 0
        assert len(calls) == 1
        clock[0] += 11
        await resolver.resolve(city="北京", atomic_place_name="故宫博物院")
        assert len(calls) == 2
        await resolver.resolve(city="北京", atomic_place_name="星河公园", category_hint="景点")
        await resolver.resolve(city="北京", atomic_place_name="故宫博物院")
        assert len(calls) == 4


@pytest.mark.asyncio
async def test_category_hint_has_separate_cache_and_cancellation_keeps_other_waiter_alive():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request)
        started.set()
        await release.wait()
        return httpx.Response(200, json={"status": "1", "pois": [_poi()]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = AmapPlaceResolver(api_key="test", client=client)
        first = asyncio.create_task(resolver.resolve(city="北京", atomic_place_name="故宫博物院", category_hint="景点"))
        await started.wait()
        second = asyncio.create_task(resolver.resolve(city="北京", atomic_place_name="故宫博物院", category_hint="景点"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await second).place is not None
        conflict = await resolver.resolve(city="北京", atomic_place_name="故宫博物院", category_hint="酒店")
        assert conflict.place is None
    assert len(calls) == 1


def chengdu_admin(keyword):
    if keyword == "510000":
        return {"name": "四川省", "adcode": "510000", "level": "province"}
    return {"name": "成都市", "adcode": "510100", "level": "city", "polyline": "103.5,30.2;104.5,30.2;104.5,31.2;103.5,30.2",
            "districts": [{"name": "武侯区", "adcode": "510107", "level": "district"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_manual_other_city_verified_alias_rewrite_preserves_ambiguity_and_wrong_city_filter(monkeypatch, ambiguous):
    entities = [CityEntity("a", "成都", "星河公园", "poi", "attraction", ("星河绿地",), review_status="name_verified")]
    if ambiguous:
        entities.append(CityEntity("b", "成都", "星河花园", "poi", "attraction", ("星河绿地",), review_status="name_verified"))
    monkeypatch.setattr(candidates, "get_city_knowledge", lambda: CityKnowledge(tuple(entities)))
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("district"):
            return httpx.Response(200, json={"status": "1", "districts": [chengdu_admin(request.url.params["keywords"])]})
        poi = _poi(name="星河公园", pname="四川省", cityname="成都市", adname="武侯区", adcode="510107", location="104.04,30.64")
        return httpx.Response(200, json={"status": "1", "pois": [
            {**poi, "id": "wrong-city", "cityname": "德阳市"}, poi,
            {**poi, "id": "branch", "name": "星河公园东门"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(AmapPlaceResolver, "_http_client", lambda _: client)
        monkeypatch.setattr(candidates, "get_settings", lambda: SimpleNamespace(amap_api_key="test", trip_understanding_provider_mode="live"))
        found = await candidates.search_candidates(city="成都", query="星河绿地", category_hint="景点")
    text_calls = [r for r in calls if r.url.path.endswith("text")]
    assert len(text_calls) == 1
    assert text_calls[0].url.params["keywords"] == ("星河绿地" if ambiguous else "星河公园")
    assert text_calls[0].url.params["city_limit"] == "true"
    assert all(place.city == "成都" and place.canonical_place_id != "amap:wrong-city" for place in found)
    assert found[0].name == "星河公园"
