from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.importing.models import ResolvedPlaceReceipt
from app.schemas.place import Coordinates, PlaceCategory, RetrievalExecutionMode
from app.suggestions.models import (
    CandidateCurrentFact,
    FrozenCanonicalPlace,
    RouteReceipt,
    RouteReceiptLeg,
)
from app.suggestions.providers import (
    ProviderCandidate,
    ProviderCandidateBatch,
    ProviderCandidateQuery,
    RouteTimes,
)
from app.suggestions.frozen_snapshot import (
    FrozenSnapshotCandidateSource,
    FrozenSnapshotError,
    FrozenSnapshotRouteSource,
    FrozenSnapshotSpec,
    FrozenSuggestionSnapshot,
)
from scripts.capture_chained_suggestion_provider_snapshot import (
    DEFAULT_OUTPUT,
    MAX_PROVIDER_REQUESTS,
    _combined_query,
    collect,
    seal_report,
    validate_artifact,
)


OBSERVED_AT = datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc)


def _hash(char: str) -> str:
    return char * 64


class ChainedCandidateSource:
    def __init__(self):
        self.calls: list[ProviderCandidateQuery] = []

    async def search(self, query: ProviderCandidateQuery) -> ProviderCandidateBatch:
        self.calls.append(query)
        intent = query.intents[0].value.lower()
        candidates = []
        for index in range(1, 7):
            category = PlaceCategory.FOOD if index in {3, 6} else PlaceCategory.ATTRACTION
            place = FrozenCanonicalPlace(
                place_id=f"{query.anchor_place_id}-{intent}-{index}",
                name=f"{query.anchor_name}-{intent}-{index}",
                city=query.city,
                district="中心区",
                address=f"链式路{index}号",
                category=category.value,
                coords=Coordinates(
                    lng=query.search_center.lng + index * 0.0001,
                    lat=query.search_center.lat + index * 0.0001,
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
            candidates.append(
                ProviderCandidate(
                    canonical_place=place,
                    provider_receipt=receipt,
                    popularity=0.7,
                    diversity_tags=(category.value,),
                    current_facts=(CandidateCurrentFact(
                        fact_type="OPENING_HOURS",
                        value="08:00-20:00",
                        provider="amap_v5_place_around",
                        observed_at=OBSERVED_AT,
                        request_hash=_hash("a"),
                        response_hash=_hash("b"),
                        execution_mode=RetrievalExecutionMode.LIVE,
                        source_url="https://restapi.amap.com/v5/place/around",
                    ),),
                )
            )
        return ProviderCandidateBatch(
            provider_snapshot_id=f"amap-{query.city}-{query.anchor_place_id}-{intent}",
            candidates=tuple(candidates),
            retrieved_at=OBSERVED_AT,
        )


class ChainedRouteSource:
    def __init__(self, *, available: bool = True):
        self.available = available
        self.calls: list[tuple[ProviderCandidateQuery, ProviderCandidate]] = []

    async def route_times(
        self, query: ProviderCandidateQuery, candidate: ProviderCandidate
    ) -> RouteTimes:
        self.calls.append((query, candidate))
        if not self.available:
            return RouteTimes(status="UNKNOWN", reason_code="CONTROLLED_UNAVAILABLE")
        duration = 8 + len(self.calls) % 8
        receipt = RouteReceipt(
            leg=RouteReceiptLeg.PREVIOUS_TO_CANDIDATE,
            transport_mode="walking",
            origin_place_id=str(query.anchor_place_id),
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


def _runtime() -> dict[str, object]:
    return {
        "runtime_profile": "local_real",
        "amap_mock": False,
        "demo_mode": False,
        "amap_api_key_configured": True,
    }


@pytest.mark.asyncio
async def test_capture_builds_three_independent_four_stop_chains_with_bounded_requests():
    candidates = ChainedCandidateSource()
    routes = ChainedRouteSource()
    report = await collect(
        candidate_source_factory=lambda: candidates,
        route_source_factory=lambda: routes,
        runtime=_runtime(),
        request_pause_seconds=0,
    )

    assert report["capture_status"] == "COMPLETE"
    assert report["overall_status"] == "PASSED"
    assert report["authoritative_public_asgi_replay"]["status"] == "NOT_RUN"
    assert report["authoritative_public_asgi_replay"]["transport"] == (
        "SEPARATE_RUNSPEC_PUBLIC_HTTP"
    )
    assert report["claim_boundary"]["proves_three_city_four_stop_snapshot_replay"] is False
    assert report["failure_receipt"] is None
    assert report["integrity"]["passed"] is True
    assert validate_artifact(report) == []
    assert report["request_accounting"] == {
        "max_requests": MAX_PROVIDER_REQUESTS,
        "actual_requests": 90,
        "candidate_query_requests": 36,
        "walking_route_requests": 54,
    }
    assert len(candidates.calls) == 36
    assert len(routes.calls) == 54
    for city in report["cities"]:
        assert city["chain_status"] == "COMPLETE"
        assert len(city["selected_chain_place_ids"]) == 4
        assert len(set(city["selected_chain_place_ids"])) == 4
        for previous, current in zip(city["rounds"], city["rounds"][1:], strict=False):
            assert current["anchor"]["place_id"] == previous["selection"]["selected_candidate_place_id"]


@pytest.mark.asyncio
async def test_unavailable_routes_are_partial_and_never_promoted_to_fill_chain():
    report = await collect(
        candidate_source_factory=ChainedCandidateSource,
        route_source_factory=lambda: ChainedRouteSource(available=False),
        runtime=_runtime(),
        request_pause_seconds=0,
    )

    assert report["overall_status"] == "PARTIAL"
    assert report["failure_receipt"]["reason_code"] == "THREE_CITY_CHAIN_INCOMPLETE"
    assert all(city["completed_rounds"] == 0 for city in report["cities"])
    assert all(len(city["selected_chain_place_ids"]) == 1 for city in report["cities"])


@pytest.mark.asyncio
async def test_validator_rejects_broken_anchor_continuity_and_payload_hash():
    report = await collect(
        candidate_source_factory=ChainedCandidateSource,
        route_source_factory=ChainedRouteSource,
        runtime=_runtime(),
        request_pause_seconds=0,
    )
    tampered = copy.deepcopy(report)
    tampered["cities"][0]["rounds"][1]["anchor"]["place_id"] = "forged-anchor"

    errors = validate_artifact(tampered)

    assert "北京:round_2:anchor_continuity_invalid" in errors
    assert "artifact_payload_hash_mismatch" in errors


@pytest.mark.asyncio
async def test_provider_capture_cannot_self_attest_product_replay():
    report = await collect(
        candidate_source_factory=ChainedCandidateSource,
        route_source_factory=ChainedRouteSource,
        runtime=_runtime(),
        request_pause_seconds=0,
    )
    report["claim_boundary"]["proves_three_city_four_stop_snapshot_replay"] = True
    report["authoritative_public_asgi_replay"] = {
        "status": "PASS",
        "transport": "PUBLIC_ASGI_HTTP",
        "gate_decision": "PASS",
        "reason_code": "SELF_ATTESTED",
    }

    sealed = seal_report(report)

    assert sealed["overall_status"] == "FAILED"
    assert "authoritative_replay_boundary_invalid" in sealed["integrity"]["validation_errors"]
    assert "authoritative_replay_claim_must_remain_false" in sealed["integrity"]["validation_errors"]


@pytest.mark.asyncio
async def test_checked_in_chain_is_complete_provider_capture_and_can_be_served_for_g2():
    raw = DEFAULT_OUTPUT.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    spec = FrozenSnapshotSpec(
        path=str(DEFAULT_OUTPUT.relative_to(Path(__file__).resolve().parents[2])).replace("\\", "/"),
        file_sha256=hashlib.sha256(raw).hexdigest(),
        snapshot_id=payload["integrity"]["artifact_payload_sha256"],
    )
    assert payload["overall_status"] == "PASSED"
    assert payload["capture_status"] == "COMPLETE"
    assert payload["authoritative_public_asgi_replay"]["gate_decision"] == "UNAVAILABLE"
    assert payload["claim_boundary"]["proves_three_city_four_stop_snapshot_replay"] is False
    assert validate_artifact(payload) == []
    batch = await FrozenSnapshotCandidateSource(spec).search(
        _combined_query({
            "city": "北京",
            "place_id": "B000A7BD6T",
            "name": "故宫博物院",
            "coords": Coordinates(lng=116.3913, lat=39.9163),
        })
    )
    assert 4 <= len(batch.candidates) <= 6


@pytest.mark.asyncio
async def test_checked_in_chain_replays_all_nine_exact_anchor_rounds_with_original_receipts():
    raw = DEFAULT_OUTPUT.read_bytes()
    payload = json.loads(raw)
    spec = FrozenSnapshotSpec(
        path=str(DEFAULT_OUTPUT.relative_to(Path(__file__).resolve().parents[2])).replace("\\", "/"),
        file_sha256=hashlib.sha256(raw).hexdigest(),
        snapshot_id=payload["integrity"]["artifact_payload_sha256"],
    )
    candidates = FrozenSnapshotCandidateSource(spec)
    routes = FrozenSnapshotRouteSource(spec)

    for city in payload["cities"]:
        selected_chain = city["selected_chain_place_ids"]
        assert len(selected_chain) == 4
        for round_index, round_row in enumerate(city["rounds"], 1):
            anchor = round_row["anchor"]
            query = _combined_query({
                **anchor,
                "city": city["city"],
                "coords": Coordinates.model_validate(anchor["coords"]),
            })
            batch = await candidates.search(query)
            assert 4 <= len(batch.candidates) <= 6
            assert selected_chain[round_index] in {
                item.canonical_place.place_id for item in batch.candidates
            }
            for candidate in batch.candidates:
                entity = candidate.provider_receipt
                assert entity.execution_mode is RetrievalExecutionMode.LIVE
                assert entity.source_url == "https://restapi.amap.com/v5/place/around"
                for fact in candidate.current_facts:
                    assert fact.request_hash == entity.request_hash
                    assert fact.response_hash == entity.response_hash
                    assert fact.execution_mode is RetrievalExecutionMode.LIVE
                route = await routes.route_times(query, candidate)
                assert route.status == "AVAILABLE"
                assert len(route.route_receipts) == 1
                receipt = route.route_receipts[0]
                assert receipt.origin_place_id == anchor["place_id"]
                assert receipt.destination_place_id == candidate.canonical_place.place_id
                assert receipt.execution_mode is RetrievalExecutionMode.LIVE


@pytest.mark.asyncio
async def test_partial_chain_remains_unservable_even_when_integrity_is_valid(tmp_path):
    report = await collect(
        candidate_source_factory=ChainedCandidateSource,
        route_source_factory=lambda: ChainedRouteSource(available=False),
        runtime=_runtime(),
        request_pause_seconds=0,
    )
    path = tmp_path / "partial-chain.json"
    raw = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    path.write_bytes(raw)
    spec = FrozenSnapshotSpec(
        path=path.name,
        file_sha256=hashlib.sha256(raw).hexdigest(),
        snapshot_id=report["integrity"]["artifact_payload_sha256"],
    )

    with pytest.raises(FrozenSnapshotError, match="STATUS_NOT_PASSED"):
        FrozenSuggestionSnapshot.load(spec, repo_root=tmp_path)
