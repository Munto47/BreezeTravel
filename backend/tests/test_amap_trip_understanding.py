from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.trip_understanding.amap_place import (
    AMAP_POI_V2_ENDPOINT,
    AmapPlaceResolver,
)
from app.trip_understanding.errors import PlaceProviderUnavailableError
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.pipeline import ResilientStructuredInferenceProvider
from app.trip_understanding.qwen_provider import QwenStructuredInferenceProvider
from app.trip_understanding.worker import build_configured_full_pipeline


def _poi(
    *,
    provider_id: str = "B000A83M61",
    name: str = "故宫博物院",
    city: str = "北京市",
    typecode: str = "110202",
    **extra: object,
) -> dict[str, object]:
    return {
        "id": provider_id,
        "name": name,
        "location": "116.397026,39.918058",
        "type": "风景名胜;风景名胜相关;旅游景点",
        "typecode": typecode,
        "pname": "北京市",
        "cityname": city,
        "adname": "东城区",
        "address": "景山前街4号",
        "adcode": "110101",
        **extra,
    }


def _client(payload: dict[str, object], observed: list[httpx.Request]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json=payload,
            headers={"x-request-id": "provider-request-id"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_amap_exact_city_category_match_is_adopted_with_redacted_receipt() -> None:
    observed: list[httpx.Request] = []
    payload = {"status": "1", "infocode": "10000", "count": "1", "pois": [_poi()]}
    async with _client(payload, observed) as client:
        resolver = AmapPlaceResolver(api_key="test-only", client=client)
        outcome = await resolver.resolve(
            city="北京",
            atomic_place_name="故宫博物院",
            category_hint="景点",
        )

    assert outcome.place is not None
    assert outcome.place.canonical_place_id == "B000A83M61"
    assert outcome.place.category == "景点"
    assert outcome.place.provider_binding["adcode"] == "110101"
    assert outcome.place.provider_binding["coordinates"] == {
        "longitude": 116.397026,
        "latitude": 39.918058,
    }
    assert outcome.receipt["raw_provider_response_retained"] is False
    assert outcome.receipt["external_calls"] == 1
    assert len(observed) == 1
    request = observed[0]
    assert str(request.url).startswith(AMAP_POI_V2_ENDPOINT)
    assert request.url.params["region"] == "北京"
    assert request.url.params["city_limit"] == "true"
    assert request.url.params["keywords"] == "故宫博物院"
    assert request.url.params["types"].split("|") == [
        "061000",
        "110000",
        "140100",
        "140200",
        "140400",
        "140500",
    ]
    assert "key=test-only" in str(request.url)
    assert "test-only" not in str(outcome.receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pois", "category_hint", "expected_calls"),
    [
        ([_poi(city="上海市")], "景点", 2),
        ([_poi(typecode="100100")], "景点", 2),
        ([_poi(), _poi(provider_id="B000A83M62")], "景点", 1),
    ],
)
async def test_amap_wrong_city_category_or_ambiguous_name_stays_pending(
    pois: list[dict[str, object]],
    category_hint: str,
    expected_calls: int,
) -> None:
    observed: list[httpx.Request] = []
    payload = {"status": "1", "infocode": "10000", "count": str(len(pois)), "pois": pois}
    async with _client(payload, observed) as client:
        resolver = AmapPlaceResolver(api_key="test-only", client=client)
        outcome = await resolver.resolve(
            city="北京",
            atomic_place_name="故宫博物院",
            category_hint=category_hint,
        )

    assert outcome.place is None
    assert outcome.receipt["status"] == "NO_UNIQUE_MATCH"
    assert outcome.receipt["external_calls"] == expected_calls
    assert len(observed) == expected_calls


@pytest.mark.asyncio
async def test_amap_rewrites_once_without_types_and_keeps_local_category_check() -> None:
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        pois = [] if "types" in request.url.params else [_poi()]
        return httpx.Response(
            200,
            json={"status": "1", "infocode": "10000", "pois": pois},
            headers={"x-request-id": f"provider-request-{len(observed)}"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await AmapPlaceResolver(
            api_key="test-only",
            client=client,
        ).resolve(
            city="北京",
            atomic_place_name="故宫博物院",
            category_hint="景点",
        )

    assert outcome.place is not None
    assert outcome.place.category == "景点"
    assert len(observed) == 2
    assert "types" in observed[0].url.params
    assert "types" not in observed[1].url.params
    assert outcome.receipt["external_calls"] == 2
    assert outcome.receipt["rewrite_count"] == 1
    assert outcome.receipt["query_strategy"] == (
        "CATEGORY_FILTERED_THEN_UNTYPED_LOCAL_CATEGORY_CHECK"
    )
    assert len(outcome.receipt["calls"]) == 2
    assert len(str(outcome.receipt["request_sha256"])) == 64
    assert len(str(outcome.receipt["response_sha256"])) == 64
    assert "test-only" not in str(outcome.receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("atomic", "poi", "alias_count"),
    [
        (
            "故宫博物院",
            _poi(name="北京市故宫博物院（暂停开放）"),
            0,
        ),
        (
            "故宫",
            _poi(business={"alias": "紫禁城|故宫"}),
            1,
        ),
        (
            "午门",
            _poi(name="故宫博物院-午门"),
            0,
        ),
        (
            "西湖",
            _poi(name="西湖风景区", city="杭州市", provider_id="B0WESTLAKE"),
            0,
        ),
    ],
)
async def test_amap_provider_name_variants_remain_unique_city_category_matches(
    atomic: str,
    poi: dict[str, object],
    alias_count: int,
) -> None:
    city = "杭州" if atomic == "西湖" else "北京"
    observed: list[httpx.Request] = []
    payload = {"status": "1", "infocode": "10000", "count": "1", "pois": [poi]}
    async with _client(payload, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city=city,
            atomic_place_name=atomic,
            category_hint="景点",
        )

    assert outcome.place is not None
    assert outcome.receipt["provider_alias_candidate_count"] == alias_count
    assert outcome.receipt["category_compatible_candidate_count"] == 1
    assert outcome.receipt["name_match_policy"] == (
        "PRIMARY_ALIAS_CITY_STATUS_HIERARCHY_V2"
    )
    assert len(observed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("city", "atomic"),
    [
        ("成都", "武侯祠"),
        ("北京", "https://example.invalid/place"),
        ("北京", "预约说明"),
        ("北京", "去故宫。然后吃饭"),
    ],
)
async def test_amap_non_deep_city_or_non_atomic_text_makes_zero_calls(
    city: str,
    atomic: str,
) -> None:
    observed: list[httpx.Request] = []
    async with _client({}, observed) as client:
        resolver = AmapPlaceResolver(api_key="test-only", client=client)
        outcome = await resolver.resolve(city=city, atomic_place_name=atomic)

    assert outcome.place is None
    assert outcome.receipt["external_calls"] == 0
    assert observed == []


@pytest.mark.asyncio
async def test_amap_provider_failure_is_typed_and_redacted() -> None:
    observed: list[httpx.Request] = []
    payload = {"status": "0", "infocode": "10001", "info": "INVALID_USER_KEY"}
    async with _client(payload, observed) as client:
        resolver = AmapPlaceResolver(api_key="test-only", client=client)
        with pytest.raises(PlaceProviderUnavailableError) as captured:
            await resolver.resolve(city="北京", atomic_place_name="故宫博物院")

    assert captured.value.category == "PROVIDER_STATUS_ERROR"
    assert captured.value.external_call_count == 1
    assert captured.value.provider_binding["infocode"] == "10001"
    assert captured.value.provider_binding["city"] == "北京"
    assert captured.value.provider_binding["city_limit"] is True
    assert len(str(captured.value.provider_binding["query_sha256"])) == 64
    assert "INVALID_USER_KEY" not in str(captured.value.provider_binding)
    assert "test-only" not in str(captured.value.provider_binding)


@pytest.mark.asyncio
async def test_amap_timeout_preserves_redacted_city_and_atomic_query_binding() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("controlled timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = AmapPlaceResolver(api_key="test-only", client=client)
        with pytest.raises(PlaceProviderUnavailableError) as captured:
            await resolver.resolve(city="杭州", atomic_place_name="西湖", category_hint="景点")

    binding = captured.value.provider_binding
    assert captured.value.category == "DEADLINE_EXCEEDED"
    assert captured.value.external_call_count == 1
    assert binding["city"] == "杭州"
    assert binding["city_limit"] is True
    assert len(str(binding["query_sha256"])) == 64
    assert "西湖" not in str(binding)
    assert "test-only" not in str(binding)


def test_full_worker_profile_injects_live_qwen_and_amap_only_when_enabled() -> None:
    fixture = build_configured_full_pipeline(
        Settings(_env_file=None, trip_understanding_provider_mode="fixture")
    )
    assert isinstance(fixture.place_resolver, ControlledSnapshotPlaceResolver)

    live = build_configured_full_pipeline(
        Settings(
            _env_file=None,
            trip_understanding_provider_mode="live",
            qwen_api_key="test-qwen-key",
            trip_understanding_qwen_model="qwen-exact-test-snapshot",
            amap_api_key="test-amap-key",
        )
    )
    assert isinstance(live.inference_provider, ResilientStructuredInferenceProvider)
    assert isinstance(live.inference_provider.primary, QwenStructuredInferenceProvider)
    assert isinstance(live.place_resolver, AmapPlaceResolver)
