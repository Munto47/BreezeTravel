from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, model_validator

from app.trip_understanding.errors import RouteProviderUnavailableError
from app.trip_understanding.models import MapReadinessView, StrictModel
from app.trip_understanding.pipeline import canonical_sha256


_ROUTE_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "trip_understanding_route_fixture_v1.json"
)
_ROUTE_FIXTURE_BYTES = _ROUTE_FIXTURE_PATH.read_bytes()
ROUTE_FIXTURE_SHA256 = hashlib.sha256(_ROUTE_FIXTURE_BYTES).hexdigest()
_ROUTE_FIXTURE = json.loads(_ROUTE_FIXTURE_BYTES.decode("utf-8"))
ROUTE_CONFIG_SHA256 = canonical_sha256(
    {
        "selection_policy": _ROUTE_FIXTURE["selection_policy"],
        "modes": ["walking", "transit"],
        "walking_endpoint": "https://restapi.amap.com/v3/direction/walking",
        "transit_endpoint": "https://restapi.amap.com/v3/direction/transit/integrated",
        "route_deadline_ms": 6000,
        "geometry_persistence": "REDIS_TTL_24H_REFERENCE_ONLY",
    }
)


class PlanRevisionRef(StrictModel):
    kind: Literal["UNDERSTANDING", "ITINERARY"]
    aggregate_id: str
    revision: int = Field(gt=0)
    stop_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class MapStop(StrictModel):
    day_index: int = Field(ge=1, le=14)
    day_label: str
    sequence_index: int = Field(ge=0)
    name: str
    canonical_place_id: str | None = None
    resolution_status: Literal["AUTO_MATCHED", "NEEDS_CONFIRMATION", "UNRESOLVED"]
    city: str | None = None
    longitude: float | None = Field(default=None, ge=-180, le=180)
    latitude: float | None = Field(default=None, ge=-90, le=90)


class MapRenderPlan(StrictModel):
    understanding_id: str
    plan_ref: PlanRevisionRef
    route_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    stops: list[MapStop]


class MapRenderJobRecord(StrictModel):
    map_job_id: str
    understanding_id: str
    plan_ref_id: str
    plan_ref: PlanRevisionRef
    route_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["BUILDING"]
    lease_owner: str
    lease_until: datetime
    attempt: int = Field(gt=0)
    max_attempts: int = Field(gt=0)
    started_at: datetime


class RouteGeometryPoint(StrictModel):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)


class InternalRouteModeFact(StrictModel):
    mode: Literal["walking", "transit"]
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    duration_minutes: int | None = Field(default=None, gt=0)
    distance_meters: int | None = Field(default=None, gt=0)
    transfer_count: int | None = Field(default=None, ge=0)
    response_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    geometry_ref: str | None = None
    geometry: list[RouteGeometryPoint] = Field(default_factory=list)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_binding: dict[str, object]
    external_call_count: int = Field(ge=0)
    observed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def available_fields_are_consistent(self) -> "InternalRouteModeFact":
        has_values = self.duration_minutes is not None and self.distance_meters is not None
        if (self.status == "AVAILABLE") != has_values:
            raise ValueError("available route facts require duration and distance")
        return self


class InternalMapEdge(StrictModel):
    day_index: int = Field(ge=1, le=14)
    day_label: str
    sequence_index: int = Field(ge=0)
    origin_name: str
    destination_name: str
    selected_mode: Literal["walking", "transit"] | None = None
    unavailable_reason: str | None = None
    walking: InternalRouteModeFact
    transit: InternalRouteModeFact

    @property
    def available(self) -> bool:
        return self.selected_mode is not None


class MapRenderOutput(StrictModel):
    plan_ref: PlanRevisionRef
    route_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["READY", "PARTIAL", "UNAVAILABLE"]
    stop_count: int = Field(ge=0)
    edges: list[InternalMapEdge]
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_binding: dict[str, object]
    failure: dict[str, object] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime
    observed_at: datetime
    expires_at: datetime


class PublicRouteModeView(StrictModel):
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    duration_minutes: int | None = None
    distance_meters: int | None = None
    transfer_count: int | None = None
    geometry: list[RouteGeometryPoint] = Field(default_factory=list)


class PublicMapEdgeView(StrictModel):
    from_name: str
    to_name: str
    selected_mode: Literal["walking", "transit"] | None = None
    message: str
    walking: PublicRouteModeView
    transit: PublicRouteModeView


class PublicMapDayView(StrictModel):
    label: str
    routes: list[PublicMapEdgeView]


class MapRenderView(StrictModel):
    status: Literal["PREPARING", "AVAILABLE", "NEEDS_UPDATE", "LIMITED", "UNAVAILABLE"]
    message: str
    days: list[PublicMapDayView] = Field(default_factory=list)
    available_actions: list[Literal["VIEW_MAP", "RENDER_MAP"]] = Field(default_factory=list)

    def readiness(self) -> MapReadinessView:
        return MapReadinessView(
            status=self.status,
            message=self.message,
            available_actions=self.available_actions,
        )


class MapRenderAcceptedView(StrictModel):
    status: Literal["PREPARING", "AVAILABLE", "LIMITED", "UNAVAILABLE"]
    message: str


class MapRenderRequestOutcome(StrictModel):
    accepted: MapRenderAcceptedView
    replayed: bool = False


class RouteProvider(Protocol):
    async def route(
        self,
        origin: MapStop,
        destination: MapStop,
        mode: Literal["walking", "transit"],
        *,
        observed_at: datetime,
    ) -> InternalRouteModeFact: ...


def choose_route_mode(
    walking: InternalRouteModeFact,
    transit: InternalRouteModeFact,
) -> Literal["walking", "transit"] | None:
    if walking.status == "AVAILABLE" and transit.status == "AVAILABLE":
        assert walking.duration_minutes is not None
        assert transit.duration_minutes is not None
        if walking.duration_minutes <= transit.duration_minutes + 10:
            return "walking"
        return "transit"
    if walking.status == "AVAILABLE":
        return "walking"
    if transit.status == "AVAILABLE":
        return "transit"
    return None


def _unavailable_fact(
    origin: MapStop,
    destination: MapStop,
    mode: Literal["walking", "transit"],
    *,
    reason: str,
    observed_at: datetime,
    provider_binding: dict[str, object] | None = None,
    external_call_count: int = 0,
) -> InternalRouteModeFact:
    request = {
        "origin": origin.name,
        "destination": destination.name,
        "mode": mode,
        "route_config_hash": ROUTE_CONFIG_SHA256,
    }
    response = {"status": "UNAVAILABLE", "reason": reason}
    return InternalRouteModeFact(
        mode=mode,
        status="UNAVAILABLE",
        response_hash=canonical_sha256(response),
        request_hash=canonical_sha256(request),
        provider_binding={
            "execution_mode": "controlled_fixture",
            "fixture_sha256": ROUTE_FIXTURE_SHA256,
            "reason": reason,
            **(provider_binding or {}),
        },
        external_call_count=external_call_count,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(hours=24),
    )


class ControlledFixtureRouteProvider:
    def __init__(self) -> None:
        self._routes = {
            (item["origin"], item["destination"]): item
            for item in _ROUTE_FIXTURE["routes"]
        }

    async def route(
        self,
        origin: MapStop,
        destination: MapStop,
        mode: Literal["walking", "transit"],
        *,
        observed_at: datetime,
    ) -> InternalRouteModeFact:
        request = {
            "origin": origin.name,
            "destination": destination.name,
            "mode": mode,
            "route_config_hash": ROUTE_CONFIG_SHA256,
        }
        record = self._routes.get((origin.name, destination.name))
        if record is None:
            is_stay_edge = any(
                marker in stop.name
                for stop in (origin, destination)
                for marker in ("酒店", "饭店", "宾馆", "旅馆")
            )
            if (
                not is_stay_edge
                or
                origin.longitude is None
                or origin.latitude is None
                or destination.longitude is None
                or destination.latitude is None
            ):
                return _unavailable_fact(
                    origin,
                    destination,
                    mode,
                    reason="FIXTURE_ROUTE_NOT_AVAILABLE",
                    observed_at=observed_at,
                )
            latitude_1 = math.radians(origin.latitude)
            latitude_2 = math.radians(destination.latitude)
            delta_latitude = latitude_2 - latitude_1
            delta_longitude = math.radians(destination.longitude - origin.longitude)
            haversine = (
                math.sin(delta_latitude / 2) ** 2
                + math.cos(latitude_1)
                * math.cos(latitude_2)
                * math.sin(delta_longitude / 2) ** 2
            )
            straight_line_meters = int(
                2 * 6_371_000 * math.asin(min(1.0, math.sqrt(haversine)))
            )
            if mode == "walking":
                distance_meters = max(100, int(straight_line_meters * 1.2))
                duration_minutes = max(2, math.ceil(distance_meters / 75))
                transfer_count = 0
            else:
                distance_meters = max(200, int(straight_line_meters * 1.35))
                duration_minutes = max(8, math.ceil(distance_meters / 300) + 6)
                transfer_count = 0 if distance_meters < 2_500 else 1
            record = {
                mode: {
                    "duration_minutes": duration_minutes,
                    "distance_meters": distance_meters,
                    "transfer_count": transfer_count,
                }
            }
        response = {"status": "AVAILABLE", **record[mode]}
        geometry = []
        if (
            origin.longitude is not None
            and origin.latitude is not None
            and destination.longitude is not None
            and destination.latitude is not None
        ):
            geometry = [
                RouteGeometryPoint(longitude=origin.longitude, latitude=origin.latitude),
                RouteGeometryPoint(longitude=destination.longitude, latitude=destination.latitude),
            ]
        return InternalRouteModeFact(
            mode=mode,
            status="AVAILABLE",
            duration_minutes=int(record[mode]["duration_minutes"]),
            distance_meters=int(record[mode]["distance_meters"]),
            transfer_count=int(record[mode]["transfer_count"]),
            geometry=geometry,
            response_hash=canonical_sha256(response),
            request_hash=canonical_sha256(request),
            provider_binding={
                "execution_mode": _ROUTE_FIXTURE["execution_mode"],
                "snapshot_id": _ROUTE_FIXTURE["snapshot_id"],
                "fixture_sha256": ROUTE_FIXTURE_SHA256,
            },
            external_call_count=int(_ROUTE_FIXTURE["external_calls"]),
            observed_at=observed_at,
            expires_at=observed_at + timedelta(hours=24),
        )


class MapRenderer:
    def __init__(self, provider: RouteProvider | None = None) -> None:
        self.provider = provider or ControlledFixtureRouteProvider()

    async def render(
        self,
        plan: MapRenderPlan,
        *,
        observed_at: datetime | None = None,
    ) -> MapRenderOutput:
        started_at = observed_at or datetime.now(timezone.utc)
        by_day: dict[int, list[MapStop]] = defaultdict(list)
        for stop in sorted(plan.stops, key=lambda item: (item.day_index, item.sequence_index)):
            by_day[stop.day_index].append(stop)
        edges: list[InternalMapEdge] = []
        for day_index in sorted(by_day):
            stops = by_day[day_index]
            for sequence_index, (origin, destination) in enumerate(zip(stops, stops[1:])):
                can_route = (
                    origin.resolution_status == "AUTO_MATCHED"
                    and destination.resolution_status == "AUTO_MATCHED"
                    and origin.canonical_place_id is not None
                    and destination.canonical_place_id is not None
                )
                if can_route:
                    async def resolve_mode(
                        mode: Literal["walking", "transit"],
                    ) -> InternalRouteModeFact:
                        try:
                            return await self.provider.route(
                                origin,
                                destination,
                                mode,
                                observed_at=started_at,
                            )
                        except RouteProviderUnavailableError as exc:
                            return _unavailable_fact(
                                origin,
                                destination,
                                mode,
                                reason=exc.category,
                                observed_at=started_at,
                                provider_binding=exc.provider_binding,
                                external_call_count=exc.external_call_count,
                            )

                    walking, transit = await asyncio.gather(
                        resolve_mode("walking"),
                        resolve_mode("transit"),
                    )
                else:
                    walking = _unavailable_fact(
                        origin,
                        destination,
                        "walking",
                        reason="PLACE_NEEDS_CONFIRMATION",
                        observed_at=started_at,
                    )
                    transit = _unavailable_fact(
                        origin,
                        destination,
                        "transit",
                        reason="PLACE_NEEDS_CONFIRMATION",
                        observed_at=started_at,
                    )
                selected = choose_route_mode(walking, transit)
                edges.append(
                    InternalMapEdge(
                        day_index=day_index,
                        day_label=origin.day_label,
                        sequence_index=sequence_index,
                        origin_name=origin.name,
                        destination_name=destination.name,
                        selected_mode=selected,
                        unavailable_reason=None if selected else "ROUTE_NOT_AVAILABLE",
                        walking=walking,
                        transit=transit,
                    )
                )
        available_count = sum(edge.available for edge in edges)
        external_call_count = sum(
            edge.walking.external_call_count + edge.transit.external_call_count
            for edge in edges
        )
        if edges and available_count == len(edges):
            status: Literal["READY", "PARTIAL", "UNAVAILABLE"] = "READY"
        elif available_count:
            status = "PARTIAL"
        else:
            status = "UNAVAILABLE"
        snapshot_payload = {
            "plan_ref": plan.plan_ref.model_dump(mode="json"),
            "route_config_hash": plan.route_config_hash,
            "status": status,
            "stops": [stop.model_dump(mode="json") for stop in plan.stops],
            "edges": [edge.model_dump(mode="json") for edge in edges],
        }
        finished_at = datetime.now(timezone.utc) if observed_at is None else observed_at
        return MapRenderOutput(
            plan_ref=plan.plan_ref,
            route_config_hash=plan.route_config_hash,
            status=status,
            stop_count=len(plan.stops),
            edges=edges,
            snapshot_sha256=canonical_sha256(snapshot_payload),
            provider_binding={
                "execution_mode": "route-provider-bound",
                "route_config_hash": plan.route_config_hash,
                "mode_fact_bindings_sha256": canonical_sha256(
                    [
                        {
                            "walking": edge.walking.provider_binding,
                            "transit": edge.transit.provider_binding,
                        }
                        for edge in edges
                    ]
                ),
                "external_calls": external_call_count,
                "modes": ["walking", "transit"],
            },
            failure={} if status == "READY" else {"unavailable_edge_count": len(edges) - available_count},
            started_at=started_at,
            finished_at=finished_at,
            observed_at=started_at,
            expires_at=started_at + timedelta(hours=24),
        )


def mode_public_view(fact: InternalRouteModeFact) -> PublicRouteModeView:
    return PublicRouteModeView(
        status=fact.status,
        duration_minutes=fact.duration_minutes,
        distance_meters=fact.distance_meters,
        transfer_count=fact.transfer_count,
        geometry=fact.geometry,
    )
