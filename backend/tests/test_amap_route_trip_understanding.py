from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app.config import Settings
from app.trip_understanding.amap_route import (
    AMAP_TRANSIT_ENDPOINT,
    AMAP_WALKING_ENDPOINT,
    AmapRouteProvider,
)
from app.trip_understanding.map_render import (
    ROUTE_CONFIG_SHA256,
    ControlledFixtureRouteProvider,
    MapRenderPlan,
    MapRenderer,
    MapStop,
    PlanRevisionRef,
)
from app.trip_understanding.map_worker import build_configured_renderer
from app.trip_understanding.pipeline import canonical_sha256


def _stop(name: str, provider_id: str, longitude: float, latitude: float) -> MapStop:
    return MapStop(
        day_index=1,
        day_label="Day 1",
        sequence_index=0 if name == "故宫博物院" else 1,
        name=name,
        canonical_place_id=provider_id,
        resolution_status="AUTO_MATCHED",
        city="北京",
        longitude=longitude,
        latitude=latitude,
    )


@pytest.mark.asyncio
async def test_live_route_provider_runs_walking_and_transit_concurrently() -> None:
    observed: list[httpx.Request] = []
    active = 0
    maximum_active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        observed.append(request)
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        if request.url.path.endswith("/walking"):
            payload = {
                "status": "1",
                "route": {"paths": [{"duration": "720", "distance": "900"}]},
            }
        else:
            payload = {
                "status": "1",
                "route": {
                    "transits": [
                        {
                            "duration": "600",
                            "distance": "1200",
                            "segments": [
                                {"bus": {"buslines": [{"name": "公交"}]}},
                                {"railway": {"name": "地铁"}},
                            ],
                        }
                    ]
                },
            }
        return httpx.Response(200, json=payload, headers={"x-request-id": "route-id"})

    origin = _stop("故宫博物院", "poi-origin", 116.397029, 39.917839)
    destination = _stop("景山公园", "poi-destination", 116.396155, 39.925015)
    plan = MapRenderPlan(
        understanding_id="live-route-test",
        plan_ref=PlanRevisionRef(
            kind="UNDERSTANDING",
            aggregate_id="live-route-test",
            revision=1,
            stop_set_hash=canonical_sha256([origin.model_dump(), destination.model_dump()]),
        ),
        route_config_hash=ROUTE_CONFIG_SHA256,
        stops=[origin, destination],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        output = await MapRenderer(
            AmapRouteProvider(api_key="test-only", client=client)
        ).render(
            plan,
            observed_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )

    assert output.status == "READY"
    assert len(output.edges) == 1
    edge = output.edges[0]
    assert edge.walking.duration_minutes == 12
    assert edge.walking.distance_meters == 900
    assert edge.transit.duration_minutes == 10
    assert edge.transit.transfer_count == 1
    assert edge.selected_mode == "walking"
    assert output.provider_binding["external_calls"] == 2
    assert maximum_active == 2
    assert {request.url.path for request in observed} == {
        httpx.URL(AMAP_WALKING_ENDPOINT).path,
        httpx.URL(AMAP_TRANSIT_ENDPOINT).path,
    }
    assert all(request.url.params["origin"] == "116.397029,39.917839" for request in observed)
    assert all("test-only" not in str(fact.provider_binding) for fact in (edge.walking, edge.transit))
    assert all(
        fact.provider_binding["raw_provider_response_retained"] is False
        for fact in (edge.walking, edge.transit)
    )


@pytest.mark.asyncio
async def test_live_route_missing_coordinates_is_unavailable_without_calls() -> None:
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(500)

    origin = MapStop(
        day_index=1,
        day_label="Day 1",
        sequence_index=0,
        name="地点A",
        canonical_place_id="poi-a",
        resolution_status="AUTO_MATCHED",
        city="北京",
    )
    destination = origin.model_copy(
        update={"sequence_index": 1, "name": "地点B", "canonical_place_id": "poi-b"}
    )
    plan = MapRenderPlan(
        understanding_id="missing-coordinate-test",
        plan_ref=PlanRevisionRef(
            kind="UNDERSTANDING",
            aggregate_id="missing-coordinate-test",
            revision=1,
            stop_set_hash="a" * 64,
        ),
        route_config_hash=ROUTE_CONFIG_SHA256,
        stops=[origin, destination],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        output = await MapRenderer(
            AmapRouteProvider(api_key="test-only", client=client)
        ).render(plan)

    assert output.status == "UNAVAILABLE"
    assert output.provider_binding["external_calls"] == 0
    assert observed == []
    assert output.edges[0].walking.status == "UNAVAILABLE"
    assert output.edges[0].transit.status == "UNAVAILABLE"


def test_map_worker_uses_live_route_provider_only_for_live_profile() -> None:
    fixture = build_configured_renderer(
        Settings(_env_file=None, trip_understanding_provider_mode="fixture")
    )
    assert isinstance(fixture.provider, ControlledFixtureRouteProvider)

    live = build_configured_renderer(
        Settings(
            _env_file=None,
            trip_understanding_provider_mode="live",
            amap_api_key="test-amap-key",
        )
    )
    assert isinstance(live.provider, AmapRouteProvider)
