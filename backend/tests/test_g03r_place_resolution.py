from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.constraints.amap_types import classify_amap_type_signals
from app.schemas.place import PlaceCategory
from app.trip_understanding import amap_place as amap_place_module
from app.trip_understanding._three_city_place_lexicon import (
    PlaceLexiconEntry,
    ThreeCityPlaceLexicon,
)
from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.errors import PlaceProviderUnavailableError
from app.trip_understanding.full_text import DeterministicTextInferenceProvider
from app.trip_understanding.pipeline import TripUnderstandingPipeline


def _entry(
    entry_id: str,
    canonical_name: str,
    *,
    city: str = "北京",
    aliases: tuple[str, ...] = (),
    category: str = "attraction",
    district: str | None = "东城区",
) -> PlaceLexiconEntry:
    return PlaceLexiconEntry(
        entry_id=entry_id,
        city=city,
        canonical_name=canonical_name,
        aliases=aliases,
        category=category,
        district=district,
        sources=(
            {
                "kind": "official_verification",
                "publisher": "test authority",
                "url": "https://example.test/place",
            },
        ),
        verified_at="2026-08-30",
    )


def _use_lexicon(monkeypatch: pytest.MonkeyPatch, *entries: PlaceLexiconEntry) -> None:
    lexicon = ThreeCityPlaceLexicon(entries=entries)
    monkeypatch.setattr(amap_place_module, "get_three_city_place_lexicon", lambda: lexicon)


def _poi(
    *,
    provider_id: str = "poi-1",
    name: str = "故宫博物院",
    city: str = "北京",
    category: PlaceCategory = PlaceCategory.ATTRACTION,
    **overrides: object,
) -> dict[str, object]:
    province, district, adcode = {
        "北京": ("北京市", "东城区", "110101"),
        "上海": ("上海市", "黄浦区", "310101"),
        "杭州": ("浙江省", "上城区", "330102"),
    }[city]
    typecode, type_label = {
        PlaceCategory.ATTRACTION: ("110202", "风景名胜;风景名胜相关;旅游景点"),
        PlaceCategory.FOOD: ("050100", "餐饮服务;中餐厅;中餐厅"),
        PlaceCategory.HOTEL: ("100100", "住宿服务;宾馆酒店;宾馆酒店"),
        PlaceCategory.TRANSPORT: ("150500", "交通设施服务;地铁站;地铁站"),
    }[category]
    return {
        "id": provider_id,
        "name": name,
        "location": "116.397026,39.918058",
        "type": type_label,
        "typecode": typecode,
        "pname": province,
        "cityname": f"{city}市",
        "adname": district,
        "address": district,
        "adcode": adcode,
        **overrides,
    }


def _client(
    responder: dict[str, object] | Callable[[httpx.Request], dict[str, object]],
    observed: list[httpx.Request],
) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        payload = responder(request) if callable(responder) else responder
        return httpx.Response(200, json=payload, headers={"x-request-id": "test-request"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_explicit_dining_context_prevents_same_name_hotel_auto_match() -> None:
    source = "北京。Day 1 晚餐去悦庭。"
    observed: list[httpx.Request] = []
    pois = [
        _poi(
            provider_id="hotel-yueting",
            name="悦庭",
            category=PlaceCategory.HOTEL,
        ),
        _poi(
            provider_id="food-yueting",
            name="悦庭餐厅",
            category=PlaceCategory.FOOD,
            business={"alias": "悦庭"},
        ),
    ]
    async with _client(
        {"status": "1", "infocode": "10000", "pois": pois},
        observed,
    ) as client:
        output = await TripUnderstandingPipeline(
            DeterministicTextInferenceProvider(),
            AmapPlaceResolver(api_key="test-only", client=client),
        ).run(source)

    mention = output.proposal.mentions[0]
    card = output.public_result.days[0].activities[0]
    assert mention.atomic_place_name == "悦庭"
    assert mention.category_hint == "餐饮"
    assert card.name == "悦庭餐厅"
    assert card.category == "餐饮"
    assert card.status == "READY"
    assert all("050000" in request.url.params["types"] for request in observed)


@pytest.mark.asyncio
async def test_lexicon_alias_rewrites_query_but_never_resolves_without_provider_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_lexicon(monkeypatch, _entry("palace", "故宫博物院", aliases=("故宫",)))
    observed: list[httpx.Request] = []
    payload = {"status": "1", "infocode": "10000", "pois": []}
    async with _client(payload, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="故宫",
        )

    assert outcome.place is None
    assert len(observed) == 2
    assert all(request.url.params["keywords"] == "故宫博物院" for request in observed)
    assert all("district" not in request.url.params for request in observed)
    assert outcome.receipt["lexicon_status"] == "MATCHED"
    assert outcome.receipt["lexicon_rewrite_applied"] is True
    assert outcome.receipt["status"] == "NO_UNIQUE_MATCH"


@pytest.mark.asyncio
async def test_lexicon_and_semantic_category_conflict_makes_no_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_lexicon(monkeypatch, _entry("palace", "故宫博物院", aliases=("故宫",)))
    observed: list[httpx.Request] = []
    async with _client({}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="故宫",
            category_hint="酒店",
        )

    assert outcome.place is None
    assert outcome.receipt["status"] == "LEXICON_CATEGORY_CONFLICT"
    assert outcome.receipt["external_calls"] == 0
    assert observed == []


@pytest.mark.asyncio
async def test_ambiguous_lexicon_match_makes_no_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_lexicon(
        monkeypatch,
        _entry("museum-east", "东城博物馆", aliases=("城市博物馆",)),
        _entry("museum-west", "西城博物馆", aliases=("城市博物馆",)),
    )
    observed: list[httpx.Request] = []
    async with _client({}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="城市博物馆",
        )

    assert outcome.place is None
    assert outcome.receipt["status"] == "LEXICON_AMBIGUOUS"
    assert outcome.receipt["lexicon_status"] == "AMBIGUOUS"
    assert outcome.receipt["external_calls"] == 0
    assert observed == []


@pytest.mark.asyncio
async def test_same_tier_conflict_stays_pending_and_lower_tier_cannot_break_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_lexicon(monkeypatch, _entry("museum", "首都博物馆", aliases=("首博",)))
    pois = [
        _poi(provider_id="canonical-1", name="首都博物馆"),
        _poi(provider_id="canonical-2", name="首都博物馆"),
        _poi(provider_id="alias-1", name="首博"),
    ]
    observed: list[httpx.Request] = []
    async with _client({"status": "1", "infocode": "10000", "pois": pois}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="首博",
        )

    assert outcome.place is None
    assert outcome.receipt["selection_tier"] == "AMBIGUOUS_CANONICAL_EXACT"
    assert outcome.receipt["category_compatible_candidate_count"] == 3
    assert outcome.receipt["external_calls"] == 1
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_duplicate_provider_id_is_deduplicated_within_highest_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_lexicon(monkeypatch, _entry("museum", "首都博物馆"))
    duplicate = _poi(provider_id="same-poi", name="首都博物馆")
    observed: list[httpx.Request] = []
    payload = {"status": "1", "infocode": "10000", "pois": [duplicate, dict(duplicate)]}
    async with _client(payload, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="首都博物馆",
        )

    assert outcome.place is not None
    assert outcome.place.canonical_place_id == "same-poi"
    assert outcome.receipt["selection_tier"] == "CANONICAL_EXACT"
    assert outcome.receipt["category_compatible_candidate_count"] == 1


@pytest.mark.asyncio
async def test_cross_city_candidate_is_removed_before_unique_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_lexicon(monkeypatch, _entry("museum", "城市博物馆", district=None))
    pois = [
        _poi(provider_id="shanghai", name="城市博物馆", city="上海"),
        _poi(provider_id="beijing", name="城市博物馆", city="北京"),
    ]
    observed: list[httpx.Request] = []
    async with _client({"status": "1", "infocode": "10000", "pois": pois}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="城市博物馆",
        )

    assert outcome.place is not None
    assert outcome.place.canonical_place_id == "beijing"
    assert outcome.receipt["city_consistent_candidate_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contradiction",
    [
        {"pname": "上海市"},
        {"cityname": "上海市"},
        {"adcode": "310101"},
        {"adname": "西城区"},
    ],
)
async def test_province_city_adcode_or_lexicon_district_contradiction_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
    contradiction: dict[str, object],
) -> None:
    _use_lexicon(monkeypatch, _entry("palace", "故宫博物院", district="东城区"))
    observed: list[httpx.Request] = []
    poi = _poi(name="故宫博物院", **contradiction)
    async with _client({"status": "1", "infocode": "10000", "pois": [poi]}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="故宫博物院",
        )

    assert outcome.place is None
    assert outcome.receipt["city_consistent_candidate_count"] == 0
    assert outcome.receipt["status"] == "NO_UNIQUE_MATCH"
    assert len(observed) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("city", "district", "wrong_adcode"),
    [
        ("北京", "东城区", "110102"),
        ("上海", "黄浦区", "310104"),
        ("杭州", "上城区", "330106"),
    ],
)
async def test_district_and_same_city_adcode_contradiction_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
    city: str,
    district: str,
    wrong_adcode: str,
) -> None:
    _use_lexicon(
        monkeypatch,
        _entry("museum", "城市博物馆", city=city, district=district),
    )
    observed: list[httpx.Request] = []
    poi = _poi(
        name="城市博物馆",
        city=city,
        adname=district,
        adcode=wrong_adcode,
    )
    async with _client({"status": "1", "infocode": "10000", "pois": [poi]}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city=city,
            atomic_place_name="城市博物馆",
        )

    assert outcome.place is None
    assert outcome.receipt["city_consistent_candidate_count"] == 0
    assert outcome.receipt["status"] == "NO_UNIQUE_MATCH"


@pytest.mark.asyncio
async def test_direct_municipality_accepts_absent_city_field_only_with_consistent_province_and_adcode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_lexicon(monkeypatch, _entry("palace", "故宫博物院"))
    observed: list[httpx.Request] = []
    poi = _poi(name="故宫博物院", cityname=[])
    async with _client({"status": "1", "infocode": "10000", "pois": [poi]}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="故宫博物院",
        )

    assert outcome.place is not None
    assert outcome.place.canonical_place_id == "poi-1"


@pytest.mark.asyncio
async def test_hangzhou_county_level_city_admin_name_and_adcode_are_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_lexicon(
        monkeypatch,
        _entry("museum", "建德博物馆", city="杭州", district="建德市"),
    )
    observed: list[httpx.Request] = []
    poi = _poi(
        name="建德博物馆",
        city="杭州",
        adname="建德市",
        adcode="330182",
    )
    async with _client({"status": "1", "infocode": "10000", "pois": [poi]}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="杭州",
            atomic_place_name="建德博物馆",
        )

    assert outcome.place is not None
    assert outcome.place.canonical_place_id == "poi-1"


@pytest.mark.asyncio
async def test_typecode_and_text_category_conflict_is_unknown_and_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_lexicon(monkeypatch, _entry("palace", "故宫博物院"))
    signals = classify_amap_type_signals("100100", "风景名胜;旅游景点")
    assert signals.conflict is True
    assert signals.category is PlaceCategory.UNKNOWN

    observed: list[httpx.Request] = []
    poi = _poi(name="故宫博物院", typecode="100100")
    async with _client({"status": "1", "infocode": "10000", "pois": [poi]}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="故宫博物院",
        )

    assert outcome.place is None
    assert outcome.receipt["provider_type_conflict_candidate_count"] == 1
    assert outcome.receipt["status"] == "NO_UNIQUE_MATCH"


@pytest.mark.asyncio
async def test_provider_category_conflicting_with_semantic_and_lexicon_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_lexicon(monkeypatch, _entry("palace", "故宫博物院"))
    observed: list[httpx.Request] = []
    poi = _poi(name="故宫博物院", category=PlaceCategory.HOTEL)
    async with _client({"status": "1", "infocode": "10000", "pois": [poi]}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="故宫博物院",
            category_hint="景点",
        )

    assert outcome.place is None
    assert outcome.receipt["provider_type_conflict_candidate_count"] == 0
    assert outcome.receipt["category_compatible_candidate_count"] == 0
    assert outcome.receipt["status"] == "NO_UNIQUE_MATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_field",
    ["id", "name", "location", "typecode", "type", "pname", "adname", "adcode"],
)
async def test_identity_or_provider_evidence_field_missing_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
) -> None:
    _use_lexicon(monkeypatch, _entry("palace", "故宫博物院"))
    observed: list[httpx.Request] = []
    poi = _poi(name="故宫博物院")
    poi.pop(missing_field)
    async with _client({"status": "1", "infocode": "10000", "pois": [poi]}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="故宫博物院",
        )

    assert outcome.place is None
    assert outcome.receipt["status"] == "NO_UNIQUE_MATCH"
    assert len(observed) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_field", "invalid_value"),
    [("id", 123), ("typecode", 110202), ("type", ["风景名胜;旅游景点"])],
)
async def test_non_string_identity_or_category_evidence_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
    invalid_field: str,
    invalid_value: object,
) -> None:
    _use_lexicon(monkeypatch, _entry("palace", "故宫博物院"))
    observed: list[httpx.Request] = []
    poi = _poi(name="故宫博物院", **{invalid_field: invalid_value})
    async with _client({"status": "1", "infocode": "10000", "pois": [poi]}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="故宫博物院",
        )

    assert outcome.place is None
    assert outcome.receipt["status"] == "NO_UNIQUE_MATCH"


@pytest.mark.asyncio
async def test_place_outside_lexicon_still_uses_strict_live_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_lexicon(monkeypatch)
    observed: list[httpx.Request] = []
    poi = _poi(name="未收录纪念馆")
    async with _client({"status": "1", "infocode": "10000", "pois": [poi]}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="未收录纪念馆",
        )

    assert outcome.place is not None
    assert observed[0].url.params["keywords"] == "未收录纪念馆"
    assert outcome.receipt["lexicon_status"] == "MISS"


@pytest.mark.asyncio
async def test_unavailable_lexicon_falls_back_to_strict_live_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lexicon = ThreeCityPlaceLexicon.unavailable("LEXICON_INVALID")
    monkeypatch.setattr(amap_place_module, "get_three_city_place_lexicon", lambda: lexicon)
    observed: list[httpx.Request] = []
    poi = _poi(name="未收录纪念馆")
    async with _client({"status": "1", "infocode": "10000", "pois": [poi]}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="未收录纪念馆",
        )

    assert outcome.place is not None
    assert outcome.place.canonical_place_id == "poi-1"
    assert observed[0].url.params["keywords"] == "未收录纪念馆"
    assert outcome.receipt["lexicon_status"] == "UNAVAILABLE"
    assert outcome.receipt["selection_tier"] == "CANONICAL_EXACT"


@pytest.mark.asyncio
async def test_lexicon_hit_does_not_hide_provider_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_lexicon(monkeypatch, _entry("palace", "故宫博物院", aliases=("故宫",)))
    observed: list[httpx.Request] = []
    payload = {"status": "0", "infocode": "10001", "info": "INVALID_USER_KEY"}
    async with _client(payload, observed) as client:
        with pytest.raises(PlaceProviderUnavailableError) as captured:
            await AmapPlaceResolver(api_key="test-only", client=client).resolve(
                city="北京",
                atomic_place_name="故宫",
            )

    assert captured.value.category == "PROVIDER_STATUS_ERROR"
    assert captured.value.provider_binding["lexicon_status"] == "MATCHED"
    assert captured.value.external_call_count == 1


@pytest.mark.asyncio
async def test_complete_whitelisted_venue_suffix_is_an_equivalent_lowest_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_lexicon(monkeypatch)
    observed: list[httpx.Request] = []
    poi = _poi(name="鲁迅纪念馆")
    async with _client({"status": "1", "infocode": "10000", "pois": [poi]}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name="鲁迅",
            category_hint="景点",
        )

    assert outcome.place is not None
    assert outcome.place.canonical_place_id == "poi-1"
    assert outcome.receipt["selection_tier"] == "VENUE_SUFFIX_EQUIVALENT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("atomic", "provider_name"),
    [
        ("午门", "故宫博物院-午门"),
        ("T2航站楼", "T3航站楼"),
        ("西溪店", "湖滨店"),
        ("东校区", "西校区"),
        ("杭州站", "杭州"),
    ],
)
async def test_hierarchy_numbers_branches_campuses_and_single_suffixes_are_not_over_normalized(
    monkeypatch: pytest.MonkeyPatch,
    atomic: str,
    provider_name: str,
) -> None:
    _use_lexicon(monkeypatch)
    observed: list[httpx.Request] = []
    poi = _poi(name=provider_name)
    async with _client({"status": "1", "infocode": "10000", "pois": [poi]}, observed) as client:
        outcome = await AmapPlaceResolver(api_key="test-only", client=client).resolve(
            city="北京",
            atomic_place_name=atomic,
            category_hint="景点",
        )

    assert outcome.place is None
    assert outcome.receipt["status"] == "NO_UNIQUE_MATCH"
