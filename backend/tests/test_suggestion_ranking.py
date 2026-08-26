from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.constraints.geo_routes import RouteResult
from app.route_priors.loader import RoutePriorLoader
from app.schemas.place import Coordinates, PlaceCategory, RetrievalExecutionMode
from app.suggestions.models import FreshnessStatus, SuggestionClassification, SuggestionIntent
from app.suggestions.providers import (
    AmapCandidateSource,
    AmapRouteSource,
    AnchorRef,
    ControlledCandidateFact,
    ControlledRouteSource,
    ControlledSnapshotCandidateSource,
    ProviderCandidateQuery,
    RouteTimes,
)
from app.suggestions.ranking import AnchorCandidateRanker, RankingContext, RankingPolicy


NOW = datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc)


CITY_CASES = {
    "北京": {
        "anchor": "Forbidden City",
        "coords": Coordinates(lng=116.397, lat=39.916),
        "places": (
            ("bj-drum", "Drum and Bell Towers", PlaceCategory.ATTRACTION, ("历史", "胡同")),
            ("bj-tian", "Tiananmen Square", PlaceCategory.ATTRACTION, ("城市地标",)),
            ("bj-jing", "景山公园", PlaceCategory.ATTRACTION, ("登高", "日落")),
            ("bj-museum", "中国国家博物馆", PlaceCategory.ATTRACTION, ("博物馆", "室内")),
            ("bj-food", "四季民福故宫店", PlaceCategory.FOOD, ("北京菜",)),
            ("bj-temple", "Temple of Heaven", PlaceCategory.ATTRACTION, ("历史", "建筑")),
        ),
    },
    "上海": {
        "anchor": "The Bund",
        "coords": Coordinates(lng=121.490, lat=31.241),
        "places": (
            ("sh-road", "Nanjing Road East", PlaceCategory.ATTRACTION, ("城市漫步",)),
            ("sh-square", "People's Square", PlaceCategory.ATTRACTION, ("城市地标",)),
            ("sh-yu", "Yuyuan Gardens", PlaceCategory.ATTRACTION, ("园林", "历史")),
            ("sh-museum", "上海博物馆", PlaceCategory.ATTRACTION, ("博物馆", "室内")),
            ("sh-food", "云南南路小吃街", PlaceCategory.FOOD, ("本帮菜",)),
            ("sh-art", "M50 art district", PlaceCategory.ATTRACTION, ("艺术", "街区")),
        ),
    },
    "杭州": {
        "anchor": "West Lake",
        "coords": Coordinates(lng=120.145, lat=30.253),
        "places": (
            ("hz-sudi", "Spring Dawn at Su Causeway", PlaceCategory.ATTRACTION, ("湖景", "步行")),
            ("hz-bridge", "Lingering Snow on Broken Bridge", PlaceCategory.ATTRACTION, ("湖景", "古迹")),
            ("hz-leifeng", "Leifeng Pagoda in Evening Glow", PlaceCategory.ATTRACTION, ("登高", "日落")),
            ("hz-museum", "浙江省博物馆", PlaceCategory.ATTRACTION, ("博物馆", "室内")),
            ("hz-food", "知味观湖滨店", PlaceCategory.FOOD, ("杭帮菜",)),
            ("hz-moon", "Three Ponds Mirroring the Moon", PlaceCategory.ATTRACTION, ("游船", "湖景")),
        ),
    },
}


def _fact(
    city: str,
    center: Coordinates,
    place_id: str,
    name: str,
    category: PlaceCategory,
    tags: tuple[str, ...] = (),
    *,
    lng_offset: float = 0.002,
    popularity: float = 0.6,
    hard: tuple[str, ...] = (),
) -> ControlledCandidateFact:
    return ControlledCandidateFact(
        place_id=place_id,
        name=name,
        city=city,
        category=category,
        coords=Coordinates(lng=center.lng + lng_offset, lat=center.lat),
        popularity=popularity,
        content_relevance=0.7,
        member_suitability=0.8,
        budget_fit=0.75,
        soft_preference=0.7,
        diversity_tags=tags,
        official_prior_refs=(f"official:{city}:route-1",) if "museum" not in place_id else (),
        official_route_prior=0.7 if "museum" not in place_id else 0.0,
        hard_block_codes=hard,
    )


def _query(city: str, anchor: str, coords: Coordinates) -> ProviderCandidateQuery:
    return ProviderCandidateQuery(
        city=city,
        intents=(SuggestionIntent.NEARBY, SuggestionIntent.POPULAR, SuggestionIntent.FUN, SuggestionIntent.FOOD),
        typecodes=("050000", "110000", "140100"),
        radius_m=50_000,
        anchor_name=anchor,
        anchor_place_id=f"anchor-{city}",
        anchor_coords=coords,
        keywords=("附近", "热门", "好玩", "好吃"),
    )


def _ranker(
    facts: list[ControlledCandidateFact],
    routes: dict[str, RouteTimes] | None,
    *,
    loader: RoutePriorLoader | None = None,
    observed_at: datetime = NOW,
    policy: RankingPolicy | None = None,
) -> AnchorCandidateRanker:
    return AnchorCandidateRanker(
        ControlledSnapshotCandidateSource(
            facts,
            snapshot_id="controlled-three-city-20260821",
            observed_at=observed_at,
        ),
        ControlledRouteSource(routes) if routes is not None else None,
        policy=policy,
        route_prior_loader=loader,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("city", ["北京", "上海", "杭州"])
async def test_three_city_major_routes_return_stable_materializable_four_to_six(city: str):
    case = CITY_CASES[city]
    facts = [
        _fact(city, case["coords"], place_id, name, category, tags, lng_offset=0.001 * (index + 1))
        for index, (place_id, name, category, tags) in enumerate(case["places"])
    ]
    routes = {
        fact.place_id: RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=5 + index * 5)
        for index, fact in enumerate(facts)
    }
    context = RankingContext(
        query=_query(city, case["anchor"], case["coords"]),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION, PlaceCategory.FOOD}),
        as_of=NOW,
    )
    result = await _ranker(facts, routes, loader=RoutePriorLoader()).rank(context)

    assert result.provider_status == "OK"
    assert 4 <= len(result.candidates) <= 6
    assert result.acceptable_top3
    assert all(candidate.provider_receipt.request_hash for candidate in result.candidates)
    assert all(candidate.provider_receipt.response_hash for candidate in result.candidates)
    assert all(candidate.provider_receipt.execution_mode is RetrievalExecutionMode.FIXTURE for candidate in result.candidates)
    assert any(candidate.canonical_place.category == "food" for candidate in result.candidates)
    assert any("QUOTA_EXPERIENCE_DIVERSITY" in candidate.explanation_codes for candidate in result.candidates)
    assert any("QUOTA_NEAREST" in candidate.explanation_codes for candidate in result.candidates)
    assert any("QUOTA_POPULAR" in candidate.explanation_codes for candidate in result.candidates)


@pytest.mark.asyncio
async def test_exact_15_and_30_minute_thresholds_and_far_candidate_remains_visible():
    city, case = "北京", CITY_CASES["北京"]
    facts = [
        _fact(city, case["coords"], "at-15", "十五分钟点", PlaceCategory.ATTRACTION),
        _fact(city, case["coords"], "at-16", "十六分钟点", PlaceCategory.ATTRACTION),
        _fact(city, case["coords"], "at-30", "三十分钟点", PlaceCategory.ATTRACTION),
        _fact(city, case["coords"], "at-31", "三十一分钟远点", PlaceCategory.ATTRACTION),
    ]
    routes = {
        "at-15": RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=15),
        "at-16": RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=16),
        "at-30": RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=30),
        "at-31": RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=31),
    }
    result = await _ranker(facts, routes).rank(RankingContext(
        query=_query(city, case["anchor"], case["coords"]),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    ))
    classes = {candidate.canonical_place.place_id: candidate.classification for candidate in result.candidates}
    assert classes == {
        "at-15": SuggestionClassification.ON_ROUTE,
        "at-16": SuggestionClassification.ACCEPTABLE_DETOUR,
        "at-30": SuggestionClassification.ACCEPTABLE_DETOUR,
        "at-31": SuggestionClassification.DEFER_TO_OTHER_DAY,
    }


@pytest.mark.asyncio
async def test_explicit_insert_edge_uses_three_leg_delta_not_anchor_distance():
    center = CITY_CASES["上海"]["coords"]
    fact = _fact("上海", center, "insert", "插入点", PlaceCategory.ATTRACTION)
    query = ProviderCandidateQuery(
        city="上海",
        intents=(SuggestionIntent.NEARBY,),
        typecodes=("110000",),
        radius_m=5000,
        anchor_name="外滩到人民广场",
        previous_anchor=AnchorRef(stop_id="s1", place_id="bund", name="外滩", coords=center),
        next_anchor=AnchorRef(
            stop_id="s2",
            place_id="square",
            name="人民广场",
            coords=Coordinates(lng=center.lng + 0.01, lat=center.lat),
        ),
    )
    route = RouteTimes(
        status="AVAILABLE",
        previous_to_candidate_minutes=10,
        candidate_to_next_minutes=11,
        previous_to_next_minutes=9,
    )
    result = await _ranker([fact], {"insert": route}).rank(RankingContext(
        query=query,
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    ))
    assert result.candidates[0].route_delta.delta_route_minutes == 12
    assert result.candidates[0].route_delta.previous_to_next_minutes == 9


def test_city_category_only_query_is_unrepresentable_and_partial_edge_is_rejected():
    with pytest.raises(ValueError, match="anchor coordinates"):
        ProviderCandidateQuery(
            city="杭州",
            intents=(SuggestionIntent.FUN,),
            typecodes=("110000",),
            radius_m=3000,
            anchor_name="西湖",
        )
    with pytest.raises(ValueError, match="both previous_anchor and next_anchor"):
        ProviderCandidateQuery(
            city="杭州",
            intents=(SuggestionIntent.FUN,),
            typecodes=("110000",),
            radius_m=3000,
            anchor_name="西湖",
            previous_anchor=AnchorRef(
                stop_id="s1",
                place_id="west-lake",
                name="西湖",
                coords=Coordinates(lng=120.145, lat=30.253),
            ),
        )


@pytest.mark.asyncio
async def test_wrong_city_category_selected_and_canonical_duplicates_are_removed_before_scoring():
    center = CITY_CASES["上海"]["coords"]
    facts = [
        _fact("杭州", center, "wrong-city", "错误城市", PlaceCategory.ATTRACTION),
        _fact("上海", center, "hotel", "错误酒店", PlaceCategory.HOTEL),
        _fact("上海", center, "selected", "已选地点", PlaceCategory.ATTRACTION),
        _fact("上海", center, "dup-a", "同一个标准地点", PlaceCategory.ATTRACTION),
        _fact("上海", center, "dup-b", "同一个标准地点", PlaceCategory.ATTRACTION),
        _fact("上海", center, "valid", "合法地点", PlaceCategory.ATTRACTION),
    ]
    routes = {
        place_id: RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=10)
        for place_id in ("wrong-city", "hotel", "selected", "dup-a", "dup-b", "valid")
    }
    result = await _ranker(facts, routes).rank(RankingContext(
        query=_query("上海", "The Bund", center),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        selected_place_ids=frozenset({"selected"}),
        as_of=NOW,
    ))
    ids = {candidate.canonical_place.place_id for candidate in result.candidates}
    assert ids == {"dup-a", "valid"}
    assert result.excluded_counts == {
        "WRONG_CITY": 1,
        "WRONG_CATEGORY": 1,
        "ALREADY_SELECTED": 1,
        "CANONICAL_DUPLICATE": 1,
    }


@pytest.mark.asyncio
async def test_hard_blocked_is_visible_as_infeasible_but_cannot_leak_into_acceptable_top3():
    center = CITY_CASES["北京"]["coords"]
    facts = [
        _fact("北京", center, f"ok-{index}", f"可用{index}", PlaceCategory.ATTRACTION, popularity=0.4 + index / 10)
        for index in range(4)
    ] + [
        _fact(
            "北京",
            center,
            "closed",
            "闭馆地点",
            PlaceCategory.ATTRACTION,
            popularity=1.0,
            hard=("OPENING_HOURS_CONFLICT",),
        )
    ]
    routes = {
        fact.place_id: RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=8)
        for fact in facts
    }
    result = await _ranker(facts, routes).rank(RankingContext(
        query=_query("北京", "Forbidden City", center),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    ))
    assert all(candidate.hard_gate.passed for candidate in result.candidates[:3])
    assert [candidate.canonical_place.place_id for candidate in result.infeasible_candidates] == ["closed"]
    assert result.infeasible_candidates[0].classification is SuggestionClassification.INFEASIBLE
    assert result.infeasible_candidates[0].total_score == 0


@pytest.mark.asyncio
async def test_route_unknown_is_retained_honestly_and_never_marked_acceptable():
    center = CITY_CASES["杭州"]["coords"]
    facts = [_fact("杭州", center, "unknown-route", "路线未知点", PlaceCategory.ATTRACTION)]
    result = await _ranker(facts, None).rank(RankingContext(
        query=_query("杭州", "West Lake", center),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    ))
    candidate = result.candidates[0]
    assert candidate.route_delta.status == "UNKNOWN"
    assert candidate.route_delta.reason_code == "ROUTE_PROVIDER_NOT_CONFIGURED"
    assert candidate.classification is SuggestionClassification.DEFER_TO_OTHER_DAY
    assert "ROUTE_DELTA_UNKNOWN" in candidate.explanation_codes
    assert result.acceptable_top3 == ()
    assert "TOP3_HAS_NO_ACCEPTABLE_CANDIDATE" in result.shortage_reason_codes


@pytest.mark.asyncio
async def test_stale_provider_receipt_remains_visible_but_not_acceptable():
    center = CITY_CASES["杭州"]["coords"]
    facts = [_fact("杭州", center, "stale", "旧快照点", PlaceCategory.ATTRACTION)]
    result = await _ranker(
        facts,
        {"stale": RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=5)},
        observed_at=NOW - timedelta(days=2),
    ).rank(RankingContext(
        query=_query("杭州", "West Lake", center),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    ))
    assert result.candidates[0].evidence_freshness.status is FreshnessStatus.STALE
    assert result.acceptable_top3 == ()


@pytest.mark.asyncio
async def test_provider_empty_has_explicit_shortage_without_fabrication():
    center = CITY_CASES["上海"]["coords"]
    result = await _ranker([], {}).rank(RankingContext(
        query=_query("上海", "The Bund", center),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    ))
    assert result.provider_status == "EMPTY"
    assert result.candidates == ()
    assert result.shortage_reason_codes == ("PROVIDER_EMPTY", "RESULTS_BELOW_MINIMUM")


@pytest.mark.asyncio
async def test_provider_timeout_has_stable_reason_without_fixture_fallback():
    class SlowSource:
        async def search(self, query):
            del query
            await asyncio.sleep(0.05)

    center = CITY_CASES["北京"]["coords"]
    ranker = AnchorCandidateRanker(
        SlowSource(),
        None,
        policy=RankingPolicy(provider_timeout_seconds=0.01),
    )
    result = await ranker.rank(RankingContext(
        query=_query("北京", "Forbidden City", center),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    ))
    assert result.provider_status == "TIMEOUT"
    assert result.candidates == ()
    assert result.shortage_reason_codes == ("PROVIDER_TIMEOUT", "RESULTS_BELOW_MINIMUM")


@pytest.mark.asyncio
async def test_fixed_snapshot_order_is_stable_and_provider_order_is_not_preserved():
    center = CITY_CASES["北京"]["coords"]
    facts = [
        _fact("北京", center, place_id, name, PlaceCategory.ATTRACTION, popularity=popularity)
        for place_id, name, popularity in (
            ("z-provider-first", "Z点", 0.1),
            ("a-provider-last", "A点", 1.0),
            ("m-middle", "M点", 0.5),
            ("b-middle", "B点", 0.8),
        )
    ]
    routes = {
        fact.place_id: RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=10)
        for fact in facts
    }
    context = RankingContext(
        query=_query("北京", "Forbidden City", center),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    )
    first = await _ranker(facts, routes).rank(context)
    second = await _ranker(list(reversed(facts)), routes).rank(context)
    first_ids = [candidate.canonical_place.place_id for candidate in first.candidates]
    second_ids = [candidate.canonical_place.place_id for candidate in second.candidates]
    assert first_ids == second_ids
    assert first_ids != [fact.place_id for fact in facts]


@pytest.mark.asyncio
async def test_wikivoyage_prior_only_changes_allowed_prior_components_not_current_facts():
    center = CITY_CASES["北京"]["coords"]
    fact = _fact("北京", center, "drum", "Drum and Bell Towers", PlaceCategory.ATTRACTION)
    result = await _ranker(
        [fact],
        {"drum": RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=12)},
        loader=RoutePriorLoader(),
    ).rank(RankingContext(
        query=_query("北京", "Forbidden City", center),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    ))
    candidate = result.candidates[0]
    assert candidate.score_components["community_route_prior"] == 1
    assert candidate.score_components["community_content_prior"] == 0
    assert candidate.score_components["evidence"] == 1
    assert candidate.route_delta.delta_route_minutes == 12
    assert candidate.provider_receipt.provider == "controlled_snapshot"
    assert any(ref.startswith("wikivoyage:") for ref in candidate.source_prior_refs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("city", "anchor", "candidate_name"),
    [
        ("北京", "故宫博物院", "景山公园"),
        ("上海", "遇见·南昌路", "思南书局诗歌店"),
    ],
)
async def test_hash_bound_official_neighbor_is_applied_only_after_provider_resolution(
    city: str,
    anchor: str,
    candidate_name: str,
):
    center = CITY_CASES[city]["coords"]
    fact = ControlledCandidateFact(
        place_id=f"resolved-{city}",
        name=candidate_name,
        city=city,
        category=PlaceCategory.ATTRACTION,
        coords=Coordinates(lng=center.lng + 0.001, lat=center.lat),
        popularity=0.31,
        content_relevance=0.42,
        member_suitability=0.53,
        budget_fit=0.64,
        soft_preference=0.75,
    )
    query = _query(city, anchor, center)
    context = RankingContext(
        query=query,
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    )
    routes = {fact.place_id: RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=12)}
    without_prior = await _ranker([fact], routes).rank(context)
    with_prior = await _ranker([fact], routes, loader=RoutePriorLoader()).rank(context)

    candidate = with_prior.candidates[0]
    baseline = without_prior.candidates[0]
    assert with_prior.official_prior_status == "AVAILABLE"
    assert with_prior.official_prior_reason_code == "VERIFIED_ARCHIVE_AVAILABLE"
    assert candidate.score_components["official_route_prior"] == 1
    assert candidate.total_score == pytest.approx(baseline.total_score + 0.08)
    assert "OFFICIAL_ROUTE_PRIOR_HASH_BOUND" in candidate.explanation_codes
    official_ref = next(ref for ref in candidate.source_prior_refs if ref.startswith("official-route:"))
    assert "#raw-sha256=" in official_ref
    assert "&extract-sha256=" in official_ref
    assert "&body-sha256=" in official_ref
    assert candidate.canonical_place == baseline.canonical_place
    assert candidate.provider_receipt == baseline.provider_receipt
    assert candidate.route_delta == baseline.route_delta
    assert candidate.evidence_freshness == baseline.evidence_freshness
    assert candidate.hard_gate == baseline.hard_gate
    assert candidate.score_components["popularity"] == baseline.score_components["popularity"]


@pytest.mark.asyncio
async def test_hangzhou_official_unavailable_is_observable_without_promoting_community_or_blocking_candidate():
    center = CITY_CASES["杭州"]["coords"]
    fact = ControlledCandidateFact(
        place_id="resolved-hangzhou-candidate",
        name="浙江省博物馆",
        city="杭州",
        category=PlaceCategory.ATTRACTION,
        coords=Coordinates(lng=center.lng + 0.001, lat=center.lat),
    )
    result = await _ranker(
        [fact],
        {fact.place_id: RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=8)},
        loader=RoutePriorLoader(),
    ).rank(RankingContext(
        query=_query("杭州", "西湖风景名胜区", center),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    ))

    candidate = result.candidates[0]
    assert result.official_prior_status == "UNAVAILABLE"
    assert result.official_prior_reason_code == "OFFICIAL_ARCHIVE_UNAVAILABLE"
    assert candidate.hard_gate.passed is True
    assert candidate in result.acceptable_top3
    assert candidate.score_components["official_route_prior"] == 0
    assert "OFFICIAL_ROUTE_PRIOR_UNAVAILABLE" in candidate.explanation_codes
    assert not any(ref.startswith("official-route:") for ref in candidate.source_prior_refs)


@pytest.mark.asyncio
async def test_prior_integrity_failure_fails_closed_without_changing_provider_facts_or_hard_gate():
    class BrokenPriorLoader:
        def signals_for_city(self, *_args, **_kwargs):
            raise ValueError("archive hash mismatch")

    center = CITY_CASES["北京"]["coords"]
    fact = ControlledCandidateFact(
        place_id="provider-resolved-only",
        name="景山公园",
        city="北京",
        category=PlaceCategory.ATTRACTION,
        coords=Coordinates(lng=center.lng + 0.001, lat=center.lat),
        popularity=0.7,
    )
    result = await _ranker(
        [fact],
        {fact.place_id: RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=8)},
        loader=BrokenPriorLoader(),
    ).rank(RankingContext(
        query=_query("北京", "故宫博物院", center),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    ))

    candidate = result.candidates[0]
    assert result.official_prior_status == "INTEGRITY_ERROR"
    assert result.official_prior_reason_code == "ROUTE_PRIOR_INTEGRITY_UNAVAILABLE"
    assert candidate.hard_gate.passed is True
    assert candidate.score_components["official_route_prior"] == 0
    assert candidate.source_prior_refs == []
    assert "ROUTE_PRIOR_INTEGRITY_UNAVAILABLE" in candidate.explanation_codes


@pytest.mark.asyncio
async def test_hash_bound_official_prior_cannot_promote_unknown_route_to_acceptable():
    center = CITY_CASES["北京"]["coords"]
    fact = ControlledCandidateFact(
        place_id="jingshan-route-unknown",
        name="景山公园",
        city="北京",
        category=PlaceCategory.ATTRACTION,
        coords=Coordinates(lng=center.lng + 0.001, lat=center.lat),
    )
    result = await _ranker([fact], None, loader=RoutePriorLoader()).rank(RankingContext(
        query=_query("北京", "故宫博物院", center),
        allowed_categories=frozenset({PlaceCategory.ATTRACTION}),
        as_of=NOW,
    ))

    candidate = result.candidates[0]
    assert candidate.score_components["official_route_prior"] == 1
    assert candidate.route_delta.status == "UNKNOWN"
    assert candidate.classification is SuggestionClassification.DEFER_TO_OTHER_DAY
    assert candidate not in result.acceptable_top3


@pytest.mark.asyncio
async def test_live_amap_adapter_emits_complete_receipts_and_preserves_returned_city(monkeypatch):
    payload = {
        "status": "1",
        "pois": [{
            "id": "amap-live-1",
            "name": "外滩观景点",
            "location": "121.4901,31.2411",
            "cityname": "上海市",
            "adname": "黄浦区",
            "address": "中山东一路",
            "typecode": "110000",
            "type": "风景名胜",
            "business": {"rating": "4.8", "keytag": "江景,建筑"},
        }],
    }

    class FakeResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def raise_for_status(self):
            return None

        async def json(self):
            return payload

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def get(self, *args, **kwargs):
            assert kwargs["params"]["location"] == "121.49,31.241"
            assert kwargs["params"]["region"] == "上海"
            assert kwargs["params"]["types"] == "110000"
            assert kwargs["params"]["radius"] == 3000
            return FakeResponse()

    monkeypatch.setattr("app.suggestions.providers.aiohttp.ClientSession", FakeSession)
    query = ProviderCandidateQuery(
        city="上海",
        intents=(SuggestionIntent.NEARBY,),
        typecodes=("110000",),
        radius_m=3000,
        anchor_name="外滩",
        anchor_place_id="bund",
        anchor_coords=Coordinates(lng=121.49, lat=31.241),
        keywords=("景点",),
    )
    batch = await AmapCandidateSource(api_key="secret-key").search(query)
    candidate = batch.candidates[0]
    receipt = candidate.provider_receipt
    assert candidate.canonical_place.city == "上海"
    assert receipt.execution_mode is RetrievalExecutionMode.LIVE
    assert len(receipt.request_hash) == len(receipt.response_hash) == 64
    assert receipt.provider_place_id == "amap-live-1"
    assert receipt.longitude == 121.4901
    assert "secret-key" not in receipt.request_hash
    assert batch.provider_snapshot_id == f"amap-{receipt.response_hash}"


@pytest.mark.asyncio
async def test_amap_route_receipt_uses_real_response_metadata_and_missing_hash_is_unknown(monkeypatch):
    center = CITY_CASES["北京"]["coords"]
    fact = _fact("北京", center, "route-live", "线上路线点", PlaceCategory.ATTRACTION)
    query = _query("北京", "故宫", center)
    candidate = (
        await ControlledSnapshotCandidateSource(
            [fact],
            snapshot_id="poi-controlled-for-live-route",
            observed_at=NOW,
        ).search(query)
    ).candidates[0]
    calls = 0

    async def complete_route(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return RouteResult(
            status="ok",
            duration_minutes=12,
            source="amap_walking_route",
            response_hash="a" * 64,
            observed_at=NOW,
        )

    monkeypatch.setattr("app.suggestions.providers.fetch_amap_route", complete_route)
    route = await AmapRouteSource().route_times(query, candidate)
    assert calls == 1
    assert route.status == "AVAILABLE"
    assert len(route.route_receipts) == 1
    route_receipt = route.route_receipts[0]
    assert route_receipt.response_hash == "a" * 64
    assert route_receipt.observed_at == NOW
    assert route_receipt.execution_mode is RetrievalExecutionMode.LIVE
    assert route_receipt.origin_place_id == "anchor-北京"
    assert route_receipt.destination_place_id == "route-live"
    assert route_receipt.source_url == "https://restapi.amap.com/v3/direction/walking"

    async def incomplete_route(*_args, **_kwargs):
        return RouteResult(
            status="ok",
            duration_minutes=12,
            source="amap_walking_route",
            response_hash=None,
            observed_at=NOW,
        )

    monkeypatch.setattr("app.suggestions.providers.fetch_amap_route", incomplete_route)
    unavailable = await AmapRouteSource().route_times(query, candidate)
    assert unavailable.status == "UNKNOWN"
    assert unavailable.reason_code == "ROUTE_PROVIDER_RECEIPT_INCOMPLETE"
    assert unavailable.route_receipts == ()
