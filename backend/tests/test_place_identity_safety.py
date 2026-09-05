"""Synthetic venue identities: no user guide or live provider fixture."""
from __future__ import annotations

import httpx
import pytest

from app.schemas.place import PlaceCategory
from app.trip_understanding import amap_place
from app.trip_understanding._three_city_place_lexicon import (
    PlaceLexiconEntry,
    ThreeCityPlaceLexicon,
    venue_suffix_equivalent,
)


@pytest.mark.parametrize("left,right", [
    ("星河博物馆", "星河图书馆"),
    ("星河博物院", "星河美术馆"),
    ("星河体育场", "星河体育馆"),
    ("星河纪念馆", "星河展览馆"),
    ("星河科技馆", "星河艺术馆"),
])
def test_distinct_venue_types_are_not_equal_after_removing_the_suffix(left, right):
    assert not venue_suffix_equivalent(left, right)
    assert not venue_suffix_equivalent(right, left)


@pytest.mark.parametrize("alias", ["星河", "星河博物馆", "星河图书馆"])
def test_lexicon_alias_cannot_rewrite_explicit_museum_into_a_library(alias):
    entry = PlaceLexiconEntry(
        entry_id="synthetic-library", city="上海", canonical_name="星河图书馆",
        aliases=(alias,), category="attraction", district=None, sources=(),
        verified_at="2026-09-01",
    )
    assert not ThreeCityPlaceLexicon(entries=(entry,)).lookup(
        city="上海", name="星河博物馆",
    ).matches


def _poi(name: str, *, alias: str = "", library: bool = True):
    return {
        "id": "synthetic-library" if library else "synthetic-museum",
        "name": name, "alias": alias, "location": "121.48,31.23",
        "typecode": "140500" if library else "140100",
        "type": "科教文化服务;图书馆;图书馆" if library else "科教文化服务;博物馆;博物馆",
        "pname": "上海市", "cityname": "上海市", "adname": "黄浦区",
        "adcode": "310101", "address": "黄浦区",
    }


@pytest.mark.parametrize("alias", ["", "星河", "星河博物馆", "星河图书馆"])
def test_provider_alias_cannot_override_conflicting_explicit_venue_identity(alias):
    decision = amap_place._evaluate_candidates(
        [_poi("城市星河图书馆", alias=alias) if alias else _poi("星河图书馆")],
        city="上海", canonical_name="星河博物馆", safe_aliases=(),
        expected_category=PlaceCategory.ATTRACTION, expected_district=None,
        atomic="星河博物馆",
    )
    assert decision.selected is None
    assert decision.metrics["category_compatible_candidate_count"] == 0


@pytest.mark.parametrize("name,alias", [
    ("星河博物馆", ""),
    ("星河", "星河博物馆"),
])
def test_provider_type_label_cannot_claim_library_as_explicit_museum(name, alias):
    decision = amap_place._evaluate_candidates(
        [_poi(name, alias=alias, library=True)],
        city="上海", canonical_name="星河博物馆", safe_aliases=(),
        expected_category=PlaceCategory.ATTRACTION, expected_district=None,
        atomic="星河博物馆",
    )
    assert decision.selected is None
    assert decision.metrics["provider_type_conflict_candidate_count"] == 1
    assert decision.metrics["category_compatible_candidate_count"] == 0


@pytest.mark.parametrize("left,right", [
    ("星河博物馆", "星河博物院"),
    ("星河景区", "星河风景区"),
    ("星河", "星河纪念馆"),
])
def test_existing_same_type_and_untyped_abbreviation_matches_remain(left, right):
    assert venue_suffix_equivalent(left, right)


@pytest.mark.asyncio
async def test_resolver_keeps_correct_museum_when_library_has_matching_short_alias(monkeypatch):
    monkeypatch.setattr(amap_place, "get_three_city_place_lexicon", lambda: ThreeCityPlaceLexicon(entries=()))
    requests = []

    async def reply(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "1", "infocode": "10000", "pois": [
            _poi("城市星河图书馆", alias="星河"),
            _poi("星河博物院", library=False),
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        outcome = await amap_place.AmapPlaceResolver(api_key="synthetic", client=client).resolve(
            city="上海", atomic_place_name="星河博物馆", category_hint="景点",
        )
    assert outcome.place is not None
    assert outcome.place.name == "星河博物院"
    assert len(requests) == 1
