from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.trip_understandings_v3 import get_trip_understanding_repository
from app.main import app
from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.errors import RouteProviderUnavailableError
from app.trip_understanding.map_render import MapRenderPlan, MapStop, PlanRevisionRef
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.route_geometry import InMemoryRouteGeometryCache
from app.trip_understanding.stay import (
    ControlledStayRouteProvider,
    StayCandidate,
    StayRecommendationEngine,
    stay_plan_from_map,
)
from app.trip_understanding.worker import TripUnderstandingWorker


class ManyHotelsProvider:
    def __init__(self) -> None:
        self.scopes: list[int | None] = []

    async def search(self, *, city, longitude, latitude, radius_m):
        self.scopes.append(radius_m)
        return [
            StayCandidate(
                canonical_place_id=f"hotel-{index:02d}",
                name=f"汉庭酒店（测试{index:02d}店）",
                category="住宿",
                area_or_address=f"测试路{index}号",
                city=city,
                longitude=longitude,
                latitude=latitude,
                provider_binding={"external_calls": 0},
            )
            for index in range(15)
        ] + [
            StayCandidate(
                canonical_place_id="unregistered",
                name="没有登记的旅店",
                category="住宿",
                area_or_address="测试路",
                city=city,
                longitude=longitude,
                latitude=latitude,
            ),
            StayCandidate(
                canonical_place_id="wrong-city",
                name="汉庭酒店（外地店）",
                category="住宿",
                area_or_address="外地",
                city="上海",
                longitude=longitude,
                latitude=latitude,
            ),
        ]


class OneHotelProvider:
    def __init__(self) -> None:
        self.scopes = []

    async def search(self, *, city, longitude, latitude, radius_m):
        self.scopes.append(radius_m)
        return [
            StayCandidate(
                canonical_place_id="hotel-one",
                name="汉庭酒店（单模式店）",
                category="住宿",
                area_or_address="测试路1号",
                city=city,
                longitude=longitude,
                latitude=latitude,
            )
        ]


class TransitUnavailableProvider:
    def __init__(self, *, fail_walking: bool = False) -> None:
        self.controlled = ControlledStayRouteProvider()
        self.fail_walking = fail_walking

    async def route(self, origin, destination, mode, *, observed_at):
        if mode == "transit" or self.fail_walking:
            raise RouteProviderUnavailableError(
                "CONTROLLED_MODE_UNAVAILABLE",
                provider_binding={"external_calls": 0},
                external_call_count=0,
            )
        return await self.controlled.route(
            origin,
            destination,
            mode,
            observed_at=observed_at,
        )


def _map_plan() -> MapRenderPlan:
    stops = [
        MapStop(
            day_index=1,
            day_label="Day 1",
            sequence_index=0,
            name="故宫博物院",
            canonical_place_id="p1",
            resolution_status="AUTO_MATCHED",
            city="北京",
            longitude=116.3913,
            latitude=39.9163,
        ),
        MapStop(
            day_index=1,
            day_label="Day 1",
            sequence_index=1,
            name="景山公园",
            canonical_place_id="p2",
            resolution_status="AUTO_MATCHED",
            city="北京",
            longitude=116.3974,
            latitude=39.9254,
        ),
        MapStop(
            day_index=2,
            day_label="Day 2",
            sequence_index=0,
            name="天坛公园",
            canonical_place_id="p3",
            resolution_status="AUTO_MATCHED",
            city="北京",
            longitude=116.4071,
            latitude=39.8822,
        ),
        MapStop(
            day_index=3,
            day_label="Day 3",
            sequence_index=0,
            name="颐和园",
            canonical_place_id="p4",
            resolution_status="AUTO_MATCHED",
            city="北京",
            longitude=116.2755,
            latitude=39.9999,
        ),
    ]
    return MapRenderPlan(
        understanding_id="g02-domain",
        plan_ref=PlanRevisionRef(
            kind="UNDERSTANDING",
            aggregate_id="g02-domain",
            revision=2,
            stop_set_hash=canonical_sha256([item.model_dump(mode="json") for item in stops]),
        ),
        route_config_hash="a" * 64,
        stops=stops,
    )


@pytest.mark.asyncio
async def test_stay_domain_caps_twelve_and_uses_frozen_deterministic_order() -> None:
    provider = ManyHotelsProvider()
    plan = stay_plan_from_map(_map_plan())
    assert plan is not None
    output = await StayRecommendationEngine(
        provider,
        ControlledStayRouteProvider(),
    ).recommend(
        plan,
        observed_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )

    assert output.status == "READY"
    assert provider.scopes == [2000]
    assert len(output.candidates) == 12
    assert all(item.candidate.brand == "汉庭" for item in output.candidates)
    assert [item.total_score for item in output.candidates] == sorted(
        item.total_score for item in output.candidates
    )
    assert [item.candidate.canonical_place_id for item in output.candidates] == [
        f"hotel-{index:02d}" for index in range(12)
    ]
    assert len(output.candidates[0].legs) == 4
    assert output.provider_binding["route_external_calls"] == 0


@pytest.mark.asyncio
async def test_route_geometry_cache_is_short_lived_and_reconstructable() -> None:
    cache = InMemoryRouteGeometryCache()
    points = [
        {"longitude": 116.3913, "latitude": 39.9163},
        {"longitude": 116.3974, "latitude": 39.9254},
    ]
    reference = await cache.put(points)
    assert reference and reference.startswith("rg3_")
    assert await cache.get(reference) == points
    cache.expire(reference)
    assert await cache.get(reference) is None


@pytest.mark.asyncio
async def test_stay_modes_fail_independently_and_all_missing_candidates_are_hidden() -> None:
    plan = stay_plan_from_map(_map_plan())
    assert plan is not None
    observed_at = datetime(2026, 8, 30, tzinfo=timezone.utc)
    candidate_provider = OneHotelProvider()
    one_mode = await StayRecommendationEngine(
        candidate_provider,
        TransitUnavailableProvider(),
    ).recommend(plan, observed_at=observed_at)
    assert candidate_provider.scopes == [2000, 4000, 8000, None]
    assert len(one_mode.candidates) == 1
    assert one_mode.candidates[0].missing_leg_count == 0
    assert one_mode.candidates[0].evidence_penalty == 8 * len(plan.anchors)
    assert all(
        leg.walking.status == "AVAILABLE" and leg.transit.status == "UNAVAILABLE"
        for leg in one_mode.candidates[0].legs
    )

    no_modes = await StayRecommendationEngine(
        OneHotelProvider(),
        TransitUnavailableProvider(fail_walking=True),
    ).recommend(plan, observed_at=observed_at)
    assert no_modes.status == "UNAVAILABLE"
    assert no_modes.candidates == []


@pytest.mark.asyncio
async def test_amap_route_geometry_is_parsed_for_walking_and_transit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "walking" in request.url.path:
            payload = {
                "status": "1",
                "route": {
                    "paths": [
                        {
                            "duration": "600",
                            "distance": "1000",
                            "steps": [{"polyline": "116.10,39.10;116.20,39.20"}],
                        }
                    ]
                },
            }
        else:
            payload = {
                "status": "1",
                "route": {
                    "transits": [
                        {
                            "duration": "900",
                            "distance": "3000",
                            "segments": [
                                {
                                    "walking": {
                                        "steps": [{"polyline": "116.10,39.10;116.11,39.11"}]
                                    }
                                },
                                {
                                    "bus": {
                                        "buslines": [
                                            {"polyline": "116.11,39.11;116.20,39.20"}
                                        ]
                                    }
                                },
                            ],
                        }
                    ]
                },
            }
        return httpx.Response(200, json=payload, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = AmapRouteProvider(api_key="fixture-key", client=http_client)
        origin, destination = _map_plan().stops[:2]
        walking, transit = await asyncio.gather(
            provider.route(origin, destination, "walking", observed_at=datetime.now(timezone.utc)),
            provider.route(origin, destination, "transit", observed_at=datetime.now(timezone.utc)),
        )
    assert [(point.longitude, point.latitude) for point in walking.geometry] == [
        (116.1, 39.1),
        (116.2, 39.2),
    ]
    assert [(point.longitude, point.latitude) for point in transit.geometry] == [
        (116.1, 39.1),
        (116.11, 39.11),
        (116.2, 39.2),
    ]


def test_g02_public_map_stay_selection_and_stale_journey() -> None:
    repository = InMemoryTripUnderstandingRepository()
    app.dependency_overrides[get_trip_understanding_repository] = lambda: repository
    client = TestClient(app)
    try:
        created = client.post(
            "/api/v3/trip-understandings",
            headers={"Idempotency-Key": "g02-create"},
            json={"mode": "DEMO"},
        )
        assert created.status_code == 202
        resource_id = created.json()["public_resource_id"]
        asyncio.run(TripUnderstandingWorker(repository).run_once("g02-understanding"))
        initial = client.get(f"/api/v3/trip-understandings/{resource_id}/result")
        assert initial.status_code == 200
        initial_etag = initial.headers["etag"]

        worker = MapRenderWorker(repository)
        assert asyncio.run(worker.run_once("g02-map")) is True
        map_view = client.get(
            f"/api/v3/trip-understandings/{resource_id}/map-renders/latest"
        ).json()
        assert map_view["status"] == "AVAILABLE"
        assert all(
            route[route["selected_mode"]]["geometry"]
            for day in map_view["days"]
            for route in day["routes"]
        )

        assert asyncio.run(worker.run_once("g02-stay")) is True
        suggestions = client.get(
            f"/api/v3/trip-understandings/{resource_id}/stay-suggestions"
        )
        assert suggestions.status_code == 200
        payload = suggestions.json()
        assert payload["status"] in {"AVAILABLE", "LIMITED"}
        assert 1 <= len(payload["candidates"]) <= 3
        assert all(candidate["brand"] for candidate in payload["candidates"])

        provider_effects = repository.map_provider_effect_count
        selected = client.post(
            f"/api/v3/trip-understandings/{resource_id}/stay-selection",
            headers={"Idempotency-Key": "g02-select", "If-Match": initial_etag},
            json={"candidate_token": payload["candidates"][0]["candidate_token"]},
        )
        assert selected.status_code == 200
        assert selected.json()["overnight_days"] == ["Day 1", "Day 2"]
        assert selected.json()["map_readiness"] == "NEEDS_UPDATE"
        assert selected.headers["etag"] != initial_etag
        assert repository.map_provider_effect_count == provider_effects

        replay = client.post(
            f"/api/v3/trip-understandings/{resource_id}/stay-selection",
            headers={"Idempotency-Key": "g02-select", "If-Match": initial_etag},
            json={"candidate_token": payload["candidates"][0]["candidate_token"]},
        )
        assert replay.status_code == 200
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.headers["etag"] == selected.headers["etag"]

        updated = client.get(f"/api/v3/trip-understandings/{resource_id}/result")
        assert updated.json()["map"]["status"] == "NEEDS_UPDATE"
        assert updated.json()["stay"]["candidates"][0]["selected"] is True
        selected_name = updated.json()["stay"]["candidates"][0]["name"]
        rerender = client.post(
            f"/api/v3/trip-understandings/{resource_id}/map-renders",
            headers={
                "Idempotency-Key": "g02-map-after-stay",
                "If-Match": selected.headers["etag"],
            },
        )
        assert rerender.status_code == 202
        assert repository.map_provider_effect_count == provider_effects
        assert asyncio.run(worker.run_once("g02-map-after-stay")) is True
        refreshed_map = client.get(
            f"/api/v3/trip-understandings/{resource_id}/map-renders/latest"
        ).json()
        assert refreshed_map["status"] == "AVAILABLE"
        overnight_routes = [
            route
            for day in refreshed_map["days"][:2]
            for route in day["routes"]
        ]
        assert sum(route["from_name"] == selected_name for route in overnight_routes) == 2
        assert sum(route["to_name"] == selected_name for route in overnight_routes) == 2
        assert repository.map_provider_effect_count > provider_effects
        assert "price" not in json_text(updated.json()).lower()
        assert all(
            forbidden not in json_text(updated.json())
            for forbidden in ("revision", "receipt", "Evidence", "Audit", "Repair", "Postcheck")
        )
    finally:
        app.dependency_overrides.pop(get_trip_understanding_repository, None)


def json_text(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
