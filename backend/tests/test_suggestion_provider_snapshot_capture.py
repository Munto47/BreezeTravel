from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.importing.models import ResolvedPlaceReceipt
from app.schemas.place import Coordinates, PlaceCategory, RetrievalExecutionMode
from app.suggestions.models import FrozenCanonicalPlace, RouteReceipt, RouteReceiptLeg, SuggestionIntent
from app.suggestions.providers import (
    ProviderCandidate,
    ProviderCandidateBatch,
    ProviderCandidateQuery,
    RouteTimes,
)
from scripts.capture_suggestion_provider_snapshot import (
    ANCHORS,
    collect,
    validate_artifact,
)


OBSERVED_AT = datetime(2026, 8, 21, 1, 2, 3, tzinfo=timezone.utc)
CHECKED_IN_ARTIFACT = (
    Path(__file__).resolve().parents[1]
    / "evidence"
    / "real_provider_local_authorized"
    / "suggestion_snapshot_2026-08-21.json"
)


def _hash(char: str) -> str:
    return char * 64


def _candidate(
    query: ProviderCandidateQuery,
    suffix: str,
    category: PlaceCategory,
    *,
    city: str | None = None,
    place_id: str | None = None,
    name: str | None = None,
) -> ProviderCandidate:
    canonical_id = place_id or f"amap-{query.city}-{suffix}"
    canonical_name = name or f"{query.city}{suffix}"
    canonical_city = city or query.city
    place = FrozenCanonicalPlace(
        place_id=canonical_id,
        name=canonical_name,
        city=canonical_city,
        district="中心区",
        address=f"{suffix}路1号",
        category=category.value,
        coords=Coordinates(
            lng=query.search_center.lng + 0.001 + len(suffix) * 0.00001,
            lat=query.search_center.lat + 0.001,
        ),
    )
    receipt = ResolvedPlaceReceipt(
        canonical_place_id=place.place_id,
        provider="amap",
        provider_place_id=place.place_id,
        name=place.name,
        city=place.city,
        district=place.district,
        address=place.address,
        category=place.category,
        longitude=place.coords.lng,
        latitude=place.coords.lat,
        request_hash=_hash("a"),
        response_hash=_hash("b"),
        observed_at=OBSERVED_AT,
        execution_mode=RetrievalExecutionMode.LIVE,
        source_url="https://restapi.amap.com/v5/place/around",
    )
    return ProviderCandidate(
        canonical_place=place,
        provider_receipt=receipt,
        popularity=0.8,
        diversity_tags=(category.value,),
    )


class FakeCandidateSource:
    def __init__(self):
        self.queries: list[ProviderCandidateQuery] = []

    async def search(self, query: ProviderCandidateQuery) -> ProviderCandidateBatch:
        self.queries.append(query)
        intent = query.intents[0]
        if intent is SuggestionIntent.FOOD:
            candidates = [
                _candidate(query, "food-1", PlaceCategory.FOOD),
                _candidate(query, "food-2", PlaceCategory.FOOD),
            ]
        elif intent is SuggestionIntent.FUN:
            candidates = [
                _candidate(query, "fun-1", PlaceCategory.ATTRACTION),
                _candidate(query, "fun-2", PlaceCategory.ATTRACTION),
            ]
        else:
            candidates = [
                _candidate(query, f"{intent.value.lower()}-1", PlaceCategory.ATTRACTION),
                _candidate(query, f"{intent.value.lower()}-2", PlaceCategory.FOOD),
            ]
        if intent is SuggestionIntent.NEARBY:
            anchor = next(item for item in ANCHORS if item["city"] == query.city)
            candidates.extend([
                _candidate(
                    query,
                    "anchor",
                    PlaceCategory.ATTRACTION,
                    place_id=anchor["place_id"],
                    name=anchor["name"],
                ),
                _candidate(query, "hotel", PlaceCategory.HOTEL),
                _candidate(query, "wrong-city", PlaceCategory.ATTRACTION, city="错误城市"),
            ])
        return ProviderCandidateBatch(
            provider_snapshot_id=f"amap-live-{query.city}-{intent.value}",
            candidates=tuple(candidates),
            retrieved_at=OBSERVED_AT,
        )


class FakeRouteSource:
    def __init__(self):
        self.calls: list[tuple[ProviderCandidateQuery, ProviderCandidate]] = []

    async def route_times(
        self,
        query: ProviderCandidateQuery,
        candidate: ProviderCandidate,
    ) -> RouteTimes:
        self.calls.append((query, candidate))
        duration = 12
        receipt = RouteReceipt(
            leg=RouteReceiptLeg.PREVIOUS_TO_CANDIDATE,
            transport_mode="walking",
            origin_place_id=query.anchor_place_id or "missing",
            origin_coords=query.search_center,
            destination_place_id=candidate.canonical_place.place_id,
            destination_coords=candidate.canonical_place.coords,
            duration_minutes=duration,
            provider="amap",
            request_hash=_hash("c"),
            response_hash=_hash("d"),
            observed_at=OBSERVED_AT,
            snapshot_id=f"amap-route-{candidate.canonical_place.place_id}",
            execution_mode=RetrievalExecutionMode.LIVE,
            max_age_seconds=900,
            source_url="https://restapi.amap.com/v3/direction/walking",
        )
        return RouteTimes(
            status="AVAILABLE",
            previous_to_candidate_minutes=duration,
            route_receipts=(receipt,),
        )


@pytest.mark.asyncio
async def test_fake_live_adapters_capture_three_cities_and_complete_receipts():
    candidates = FakeCandidateSource()
    routes = FakeRouteSource()
    report = await collect(
        candidate_source_factory=lambda: candidates,
        route_source_factory=lambda: routes,
        runtime={
            "runtime_profile": "local_real",
            "amap_mock": False,
            "demo_mode": False,
            "amap_api_key_configured": True,
        },
        request_pause_seconds=0,
    )

    assert report["overall_status"] == "passed"
    assert report["integrity"]["passed"] is True
    assert validate_artifact(report) == []
    assert len(candidates.queries) == 12
    assert len(routes.calls) == 18
    assert [row["city"] for row in report["cities"]] == ["北京", "上海", "杭州"]
    for city in report["cities"]:
        assert city["selection"]["frozen_candidate_count"] == 6
        assert city["selection"]["excluded_counts"] == {
            "wrong_city": 1,
            "wrong_category": 1,
            "anchor_duplicate": 1,
            "canonical_duplicate": 0,
        }
        assert [row["intent"] for row in city["query_receipts"]] == [
            "NEARBY",
            "POPULAR",
            "FUN",
            "FOOD",
        ]
        for item in city["candidates"]:
            assert item["provider_receipt"]["execution_mode"] == "live"
            assert item["operational_evidence"]["status"] == "UNKNOWN"
            assert item["route_times"]["route_receipts"][0]["provider"] == "amap"


@pytest.mark.asyncio
async def test_preflight_failure_is_a_sanitized_durable_failure_receipt():
    report = await collect(
        runtime={
            "runtime_profile": "demo",
            "amap_mock": True,
            "demo_mode": True,
            "amap_api_key_configured": False,
        },
        request_pause_seconds=0,
    )

    assert report["overall_status"] == "failed"
    assert report["failure_receipt"] == {
        "stage": "preflight",
        "reason_codes": [
            "runtime_profile_not_local_real",
            "amap_mock_not_false",
            "demo_mode_not_false",
            "amap_credentials_missing",
        ],
    }
    assert "api_key" not in str(report["failure_receipt"]).lower()
    assert report["integrity"]["passed"] is False


@pytest.mark.asyncio
async def test_artifact_validator_detects_hash_and_route_tampering():
    report = await collect(
        candidate_source_factory=FakeCandidateSource,
        route_source_factory=FakeRouteSource,
        runtime={
            "runtime_profile": "local_real",
            "amap_mock": False,
            "demo_mode": False,
            "amap_api_key_configured": True,
        },
        request_pause_seconds=0,
    )
    tampered = copy.deepcopy(report)
    tampered["cities"][0]["candidates"][0]["route_times"]["route_receipts"][0][
        "destination_place_id"
    ] = "forged-place"

    errors = validate_artifact(tampered)
    assert "北京:amap-北京-nearby-1:route_receipt_invalid" in errors
    assert "artifact_payload_hash_mismatch" in errors


def test_checked_in_live_snapshot_is_hash_bound_and_structurally_valid():
    report = json.loads(CHECKED_IN_ARTIFACT.read_text(encoding="utf-8"))

    assert validate_artifact(report) == []
    assert report["overall_status"] == "passed"
    assert report["integrity"]["passed"] is True
    assert {row["city"]: len(row["candidates"]) for row in report["cities"]} == {
        "北京": 6,
        "上海": 6,
        "杭州": 6,
    }
    assert sum(
        len(candidate["route_times"]["route_receipts"])
        for city in report["cities"]
        for candidate in city["candidates"]
    ) == 18
