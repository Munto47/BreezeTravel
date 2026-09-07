"""Unspecified place names must not acquire a venue identity by suffix deletion."""
from __future__ import annotations

import httpx
import pytest

from app.trip_understanding import amap_place
from app.trip_understanding._three_city_place_lexicon import PlaceLexiconEntry, ThreeCityPlaceLexicon


def _tier(query: str, name: str, *, aliases: tuple[str, ...] = (), provider_alias: str = ""):
    return amap_place._name_match_tier(
        {"name": name, "alias": provider_alias}, canonical_name=query,
        safe_aliases=aliases, city="北京",
    )


@pytest.mark.parametrize("query", ["前门", "星河", "青石里", "北城文化街"])
@pytest.mark.parametrize("suffix", ["博物馆", "图书馆", "纪念馆", "科技馆"])
def test_untyped_name_cannot_gain_a_venue_identity_by_suffix_deletion(query, suffix):
    assert _tier(query, query + suffix) is None


@pytest.mark.parametrize("name", ["前门", "星河", "青石里"])
def test_short_name_can_still_match_the_same_exact_provider_name(name):
    assert _tier(name, name) == "CANONICAL_EXACT"


@pytest.mark.parametrize("query,name", [
    ("星河博物馆", "星河博物院"),
    ("星河博物院", "星河博物馆"),
    ("星河景区", "星河风景区"),
])
def test_explicit_same_kind_venue_suffix_variants_remain_eligible(query, name):
    assert _tier(query, name) == "VENUE_SUFFIX_EQUIVALENT"


def test_reviewed_alias_is_not_replaced_with_unproven_suffix_matching():
    assert _tier("星河博物馆", "星河", aliases=("星河",)) == "SAFE_ALIAS_EXACT"


def test_existing_explicit_provider_alias_still_matches():
    assert _tier("紫禁城", "故宫博物院", provider_alias="紫禁城") == "SAFE_ALIAS_EXACT"


def _entry(*, aliases: tuple[str, ...] = ()) -> PlaceLexiconEntry:
    return PlaceLexiconEntry(
        entry_id="synthetic-star-museum", city="北京", canonical_name="星河博物馆",
        aliases=aliases, category="attraction", district="东城区", sources=(), verified_at="2026-09-06",
    )


async def _resolve(monkeypatch, *, query: str, name: str, entries=()):
    monkeypatch.setattr(amap_place, "get_three_city_place_lexicon", lambda: ThreeCityPlaceLexicon(entries=entries))
    requests = []

    async def reply(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "1", "infocode": "10000", "pois": [{
            "id": "synthetic-museum", "name": name,
            "typecode": "140100", "type": "科教文化服务;博物馆;博物馆",
            "location": "116.397,39.90", "pname": "北京市", "cityname": "北京市",
            "adname": "东城区", "adcode": "110101", "address": "合成地址",
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        outcome = await amap_place.AmapPlaceResolver(api_key="synthetic-only", client=client).resolve(
            city="北京", atomic_place_name=query, category_hint="景点",
        )
    return outcome, requests


@pytest.mark.asyncio
async def test_front_gate_cannot_be_auto_matched_to_a_museum(monkeypatch):
    outcome, requests = await _resolve(monkeypatch, query="前门", name="前门博物馆")
    assert outcome.place is None
    assert outcome.receipt["status"] != "AUTO_MATCHED"
    assert requests and all(request.url.params["keywords"] == "前门" for request in requests)


@pytest.mark.asyncio
async def test_lexicon_suffix_suggestion_cannot_turn_unknown_name_into_museum(monkeypatch):
    outcome, requests = await _resolve(monkeypatch, query="星河", name="星河博物馆", entries=(_entry(),))
    assert outcome.place is None
    assert requests and all(request.url.params["keywords"] == "星河" for request in requests)


@pytest.mark.asyncio
async def test_explicit_reviewed_lexicon_alias_still_resolves_a_short_name(monkeypatch):
    outcome, requests = await _resolve(
        monkeypatch, query="星河", name="星河博物馆", entries=(_entry(aliases=("星河",)),),
    )
    assert outcome.place is not None and outcome.place.name == "星河博物馆"
    assert len(requests) == 1 and requests[0].url.params["keywords"] == "星河博物馆"


@pytest.mark.asyncio
async def test_lexicon_same_kind_variant_still_resolves(monkeypatch):
    outcome, requests = await _resolve(
        monkeypatch, query="星河博物院", name="星河博物馆", entries=(_entry(),),
    )
    assert outcome.place is not None and outcome.place.name == "星河博物馆"
    assert len(requests) == 1
