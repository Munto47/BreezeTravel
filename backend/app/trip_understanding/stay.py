from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import Field

from app.constraints.amap_types import classify_amap_type, typecodes_for_category
from app.schemas.place import PlaceCategory
from app.trip_understanding.errors import PlaceProviderUnavailableError, RouteProviderUnavailableError
from app.trip_understanding.map_render import (
    InternalRouteModeFact,
    MapRenderPlan,
    MapStop,
    PlanRevisionRef,
    RouteGeometryPoint,
    RouteProvider,
    choose_route_mode,
)
from app.trip_understanding.models import StrictModel
from app.trip_understanding.pipeline import canonical_sha256


_ROOT = Path(__file__).resolve().parent
_BRAND_PATH = _ROOT / "hotel_brand_registry_v1.json"
_FIXTURE_PATH = _ROOT.parents[0] / "data" / "amap_mock_places.json"
_BRAND_BYTES = _BRAND_PATH.read_bytes()
_BRAND_PAYLOAD = json.loads(_BRAND_BYTES.decode("utf-8"))
HOTEL_BRAND_REGISTRY_SHA256 = hashlib.sha256(_BRAND_BYTES).hexdigest()

STAY_POLICY_VERSION = "stay-scoring-v1"
STAY_POLICY_SHA256 = canonical_sha256(
    {
        "version": STAY_POLICY_VERSION,
        "search_radii_m": [2000, 4000, 8000, None],
        "candidate_cap": 12,
        "public_cap": 3,
        "single_mode_penalty": 8,
        "double_mode_minutes": 120,
        "double_mode_penalty": 90,
        "evidence_penalty_cap": 240,
        "score": "sum_best+0.5*max_single+8*transfers+evidence_penalty",
        "tie_break": [
            "total_score",
            "max_single_leg_minutes",
            "transfer_count",
            "brand",
            "canonical_place_id",
        ],
        "brand_registry_sha256": HOTEL_BRAND_REGISTRY_SHA256,
    }
)

AMAP_AROUND_ENDPOINT = "https://restapi.amap.com/v5/place/around"
AMAP_TEXT_ENDPOINT = "https://restapi.amap.com/v5/place/text"


def _normalized(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _city(value: str) -> str:
    normalized = _normalized(value)
    return normalized[:-1] if normalized.endswith("市") else normalized


def haversine_meters(
    first_longitude: float,
    first_latitude: float,
    second_longitude: float,
    second_latitude: float,
) -> float:
    radius = 6_371_000.0
    first_latitude_radians = math.radians(first_latitude)
    second_latitude_radians = math.radians(second_latitude)
    latitude_delta = math.radians(second_latitude - first_latitude)
    longitude_delta = math.radians(second_longitude - first_longitude)
    value = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_latitude_radians)
        * math.cos(second_latitude_radians)
        * math.sin(longitude_delta / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def geometric_median(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not points:
        return None
    latitude_origin = sum(latitude for _longitude, latitude in points) / len(points)
    longitude_origin = sum(longitude for longitude, _latitude in points) / len(points)
    cosine = max(0.2, math.cos(math.radians(latitude_origin)))
    projected = [
        (
            (longitude - longitude_origin) * 111_320 * cosine,
            (latitude - latitude_origin) * 110_540,
        )
        for longitude, latitude in points
    ]
    x = sum(point[0] for point in projected) / len(projected)
    y = sum(point[1] for point in projected) / len(projected)
    for _ in range(32):
        distances = [math.hypot(x - px, y - py) for px, py in projected]
        if any(distance < 1e-6 for distance in distances):
            index = distances.index(min(distances))
            x, y = projected[index]
            break
        denominator = sum(1 / distance for distance in distances)
        next_x = sum(px / distance for (px, _py), distance in zip(projected, distances, strict=True)) / denominator
        next_y = sum(py / distance for (_px, py), distance in zip(projected, distances, strict=True)) / denominator
        if math.hypot(next_x - x, next_y - y) < 0.05:
            x, y = next_x, next_y
            break
        x, y = next_x, next_y
    return (
        longitude_origin + x / (111_320 * cosine),
        latitude_origin + y / 110_540,
    )


class HotelBrandRegistry:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        source = payload or _BRAND_PAYLOAD
        self.entries = [
            (
                str(item["brand"]),
                tuple(_normalized(alias) for alias in item.get("aliases", [])),
            )
            for item in source.get("brands", [])
            if isinstance(item, dict) and item.get("brand")
        ]

    def match(self, name: str) -> str | None:
        candidate = _normalized(name)
        matches = [
            (brand, alias)
            for brand, aliases in self.entries
            for alias in aliases
            if alias and alias in candidate
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda item: (-len(item[1]), item[0]))[0][0]


class StayAnchor(StrictModel):
    day_index: int = Field(ge=1, le=14)
    direction: Literal["STAY_TO_FIRST", "LAST_TO_STAY"]
    stop: MapStop


class StayRecommendationPlan(StrictModel):
    understanding_id: str
    plan_ref: PlanRevisionRef
    city: str
    center_longitude: float = Field(ge=-180, le=180)
    center_latitude: float = Field(ge=-90, le=90)
    overnight_days: list[int]
    anchors: list[StayAnchor]


class StayCandidate(StrictModel):
    canonical_place_id: str
    name: str
    category: str
    area_or_address: str
    city: str
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    brand: str | None = None
    search_radius_m: int | None = None
    provider_binding: dict[str, object] = Field(default_factory=dict)


class StayCommuteLeg(StrictModel):
    day_index: int = Field(ge=1, le=14)
    direction: Literal["STAY_TO_FIRST", "LAST_TO_STAY"]
    endpoint_name: str
    selected_mode: Literal["walking", "transit"] | None
    walking: InternalRouteModeFact
    transit: InternalRouteModeFact


class ScoredStayCandidate(StrictModel):
    candidate: StayCandidate
    total_score: float = Field(ge=0)
    max_single_leg_minutes: int = Field(ge=0)
    transfer_count: int = Field(ge=0)
    missing_leg_count: int = Field(ge=0)
    evidence_penalty: int = Field(ge=0)
    legs: list[StayCommuteLeg]


class StayRecommendationOutput(StrictModel):
    plan_ref: PlanRevisionRef
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["READY", "PARTIAL", "UNAVAILABLE"]
    area_summary: str
    searched_scopes: list[str]
    candidates: list[ScoredStayCandidate]
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_binding: dict[str, object]
    failure: dict[str, object] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime
    observed_at: datetime


class StayRecommendationJobRecord(StrictModel):
    stay_job_id: str
    understanding_id: str
    plan_ref_id: str
    plan_ref: PlanRevisionRef
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["BUILDING"]
    lease_owner: str
    lease_until: datetime
    attempt: int = Field(gt=0)
    max_attempts: int = Field(gt=0)
    started_at: datetime


class StayCandidateProvider(Protocol):
    async def search(
        self,
        *,
        city: str,
        longitude: float,
        latitude: float,
        radius_m: int | None,
    ) -> list[StayCandidate]: ...


class ControlledStayCandidateProvider:
    def __init__(self, path: Path = _FIXTURE_PATH) -> None:
        raw = path.read_bytes()
        self.snapshot_sha256 = hashlib.sha256(raw).hexdigest()
        self.payload = json.loads(raw.decode("utf-8"))

    async def search(
        self,
        *,
        city: str,
        longitude: float,
        latitude: float,
        radius_m: int | None,
    ) -> list[StayCandidate]:
        entries = self.payload.get(city, [])
        result: list[StayCandidate] = []
        for item in entries if isinstance(entries, list) else []:
            if not isinstance(item, dict) or item.get("category") != "hotel":
                continue
            coords = item.get("coords")
            if not isinstance(coords, dict):
                continue
            candidate_longitude = float(coords["lng"])
            candidate_latitude = float(coords["lat"])
            distance = haversine_meters(
                longitude,
                latitude,
                candidate_longitude,
                candidate_latitude,
            )
            if radius_m is not None and distance > radius_m:
                continue
            result.append(
                StayCandidate(
                    canonical_place_id=str(item["place_id"]),
                    name=str(item["name"]),
                    category="住宿",
                    area_or_address=str(item["address"]),
                    city=city,
                    longitude=candidate_longitude,
                    latitude=candidate_latitude,
                    search_radius_m=radius_m,
                    provider_binding={
                        "provider": "controlled_fixture_snapshot",
                        "snapshot_sha256": self.snapshot_sha256,
                        "distance_from_center_m": round(distance),
                        "external_calls": 0,
                        "raw_provider_response_retained": False,
                    },
                )
            )
        return sorted(result, key=lambda item: (item.name, item.canonical_place_id))


def _coordinates(value: object) -> tuple[float, float] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        longitude, latitude = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    return longitude, latitude


class AmapStayCandidateProvider:
    def __init__(
        self,
        *,
        api_key: str,
        deadline_seconds: float = 4.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Amap API key is required")
        self.api_key = api_key
        self.deadline_seconds = deadline_seconds
        self.client = client

    async def search(
        self,
        *,
        city: str,
        longitude: float,
        latitude: float,
        radius_m: int | None,
    ) -> list[StayCandidate]:
        endpoint = AMAP_AROUND_ENDPOINT if radius_m is not None else AMAP_TEXT_ENDPOINT
        typecodes = typecodes_for_category(PlaceCategory.HOTEL)
        params: dict[str, object] = {
            "key": self.api_key,
            "types": "|".join(typecodes),
            "region": city,
            "city_limit": "true",
            "page_size": 25,
            "page_num": 1,
            "output": "json",
        }
        if radius_m is None:
            params["keywords"] = "酒店"
        else:
            params.update(
                {
                    "location": f"{longitude:.6f},{latitude:.6f}",
                    "radius": radius_m,
                    "sortrule": "distance",
                }
            )
        safe_params = {key: value for key, value in params.items() if key != "key"}
        request_hash = canonical_sha256({"endpoint": endpoint, "params": safe_params})
        started = time.perf_counter()
        try:
            if self.client is not None:
                response = await self.client.get(endpoint, params=params, timeout=self.deadline_seconds)
            else:
                async with httpx.AsyncClient(timeout=self.deadline_seconds) as client:
                    response = await client.get(endpoint, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise PlaceProviderUnavailableError(
                "DEADLINE_EXCEEDED",
                provider_binding={"provider": "AMAP_STAY_V5", "request_sha256": request_hash},
                external_call_count=1,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise PlaceProviderUnavailableError(
                "PROVIDER_UNAVAILABLE",
                provider_binding={"provider": "AMAP_STAY_V5", "request_sha256": request_hash},
                external_call_count=1,
            ) from exc
        if not isinstance(payload, dict) or payload.get("status") != "1":
            raise PlaceProviderUnavailableError(
                "PROVIDER_STATUS_ERROR",
                provider_binding={"provider": "AMAP_STAY_V5", "request_sha256": request_hash},
                external_call_count=1,
            )
        response_hash = canonical_sha256(payload)
        result: list[StayCandidate] = []
        pois = payload.get("pois")
        for item in pois if isinstance(pois, list) else []:
            if not isinstance(item, dict):
                continue
            category = classify_amap_type(str(item.get("typecode") or ""), str(item.get("type") or ""))
            coordinates = _coordinates(item.get("location"))
            provider_id = str(item.get("id") or "").strip()
            provider_city = str(item.get("cityname") or item.get("pname") or "")
            if category != PlaceCategory.HOTEL or coordinates is None or not provider_id or _city(provider_city) != _city(city):
                continue
            address = item.get("address")
            if isinstance(address, list):
                address = "".join(str(value) for value in address)
            result.append(
                StayCandidate(
                    canonical_place_id=provider_id,
                    name=str(item.get("name") or "酒店"),
                    category="住宿",
                    area_or_address=str(address or item.get("adname") or "区域待确认"),
                    city=city,
                    longitude=coordinates[0],
                    latitude=coordinates[1],
                    search_radius_m=radius_m,
                    provider_binding={
                        "provider": "AMAP_STAY_V5",
                        "request_sha256": request_hash,
                        "response_sha256": response_hash,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "external_calls": 1,
                        "raw_provider_response_retained": False,
                    },
                )
            )
        return result


class ControlledStayRouteProvider:
    async def route(
        self,
        origin: MapStop,
        destination: MapStop,
        mode: Literal["walking", "transit"],
        *,
        observed_at: datetime,
    ) -> InternalRouteModeFact:
        if (
            origin.longitude is None
            or origin.latitude is None
            or destination.longitude is None
            or destination.latitude is None
        ):
            raise RouteProviderUnavailableError(
                "ROUTE_ENDPOINT_COORDINATES_UNAVAILABLE",
                provider_binding={"provider": "controlled_stay_route", "external_calls": 0},
                external_call_count=0,
            )
        distance = max(
            1,
            round(
                haversine_meters(
                    origin.longitude,
                    origin.latitude,
                    destination.longitude,
                    destination.latitude,
                )
            ),
        )
        if mode == "walking":
            duration = max(1, math.ceil(distance / 75))
            transfers = 0
        else:
            duration = max(6, 8 + math.ceil(distance / 400))
            transfers = 1 if distance > 5_000 else 0
        request = {
            "origin": [origin.longitude, origin.latitude],
            "destination": [destination.longitude, destination.latitude],
            "mode": mode,
            "policy": STAY_POLICY_VERSION,
        }
        response = {"duration_minutes": duration, "distance_meters": distance, "transfers": transfers}
        return InternalRouteModeFact(
            mode=mode,
            status="AVAILABLE",
            duration_minutes=duration,
            distance_meters=distance,
            transfer_count=transfers,
            response_hash=canonical_sha256(response),
            request_hash=canonical_sha256(request),
            geometry=[
                RouteGeometryPoint(longitude=origin.longitude, latitude=origin.latitude),
                RouteGeometryPoint(longitude=destination.longitude, latitude=destination.latitude),
            ],
            provider_binding={
                "provider": "controlled_stay_route",
                "execution_mode": "controlled_fixture",
                "external_calls": 0,
            },
            external_call_count=0,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(hours=24),
        )


def _unavailable_route_fact(
    mode: Literal["walking", "transit"],
    *,
    category: str,
    observed_at: datetime,
    provider_binding: dict[str, object] | None = None,
    external_calls: int = 0,
) -> InternalRouteModeFact:
    return InternalRouteModeFact(
        mode=mode,
        status="UNAVAILABLE",
        response_hash=canonical_sha256({"status": "UNAVAILABLE", "category": category}),
        request_hash=canonical_sha256({"mode": mode, "category": category}),
        provider_binding={"category": category, **(provider_binding or {})},
        external_call_count=external_calls,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(hours=24),
    )


class StayRecommendationEngine:
    def __init__(
        self,
        candidate_provider: StayCandidateProvider | None = None,
        route_provider: RouteProvider | None = None,
        brand_registry: HotelBrandRegistry | None = None,
    ) -> None:
        self.candidate_provider = candidate_provider or ControlledStayCandidateProvider()
        self.route_provider = route_provider or ControlledStayRouteProvider()
        self.brand_registry = brand_registry or HotelBrandRegistry()

    async def _mode(
        self,
        origin: MapStop,
        destination: MapStop,
        mode: Literal["walking", "transit"],
        observed_at: datetime,
    ) -> InternalRouteModeFact:
        try:
            return await self.route_provider.route(origin, destination, mode, observed_at=observed_at)
        except RouteProviderUnavailableError as exc:
            return _unavailable_route_fact(
                mode,
                category=exc.category,
                observed_at=observed_at,
                provider_binding=exc.provider_binding,
                external_calls=exc.external_call_count,
            )

    async def _score_candidate(
        self,
        plan: StayRecommendationPlan,
        candidate: StayCandidate,
        observed_at: datetime,
    ) -> ScoredStayCandidate | None:
        hotel = MapStop(
            day_index=1,
            day_label="住宿",
            sequence_index=0,
            name=candidate.name,
            canonical_place_id=candidate.canonical_place_id,
            resolution_status="AUTO_MATCHED",
            city=candidate.city,
            longitude=candidate.longitude,
            latitude=candidate.latitude,
        )
        legs: list[StayCommuteLeg] = []
        selected_minutes: list[int] = []
        transfer_count = 0
        missing_legs = 0
        evidence_penalty = 0
        for anchor in plan.anchors:
            origin, destination = (
                (hotel, anchor.stop)
                if anchor.direction == "STAY_TO_FIRST"
                else (anchor.stop, hotel)
            )
            walking, transit = await asyncio.gather(
                self._mode(origin, destination, "walking", observed_at),
                self._mode(origin, destination, "transit", observed_at),
            )
            selected_mode = choose_route_mode(walking, transit)
            if selected_mode is None:
                missing_legs += 1
                selected_minutes.append(120)
                evidence_penalty += 90
            else:
                selected = walking if selected_mode == "walking" else transit
                assert selected.duration_minutes is not None
                selected_minutes.append(selected.duration_minutes)
                transfer_count += selected.transfer_count or 0
                if walking.status == "UNAVAILABLE" or transit.status == "UNAVAILABLE":
                    evidence_penalty += 8
            legs.append(
                StayCommuteLeg(
                    day_index=anchor.day_index,
                    direction=anchor.direction,
                    endpoint_name=anchor.stop.name,
                    selected_mode=selected_mode,
                    walking=walking,
                    transit=transit,
                )
            )
        if not selected_minutes or missing_legs == len(selected_minutes):
            return None
        evidence_penalty = min(240, evidence_penalty)
        maximum = max(selected_minutes)
        total_score = sum(selected_minutes) + 0.5 * maximum + 8 * transfer_count + evidence_penalty
        return ScoredStayCandidate(
            candidate=candidate,
            total_score=round(total_score, 3),
            max_single_leg_minutes=maximum,
            transfer_count=transfer_count,
            missing_leg_count=missing_legs,
            evidence_penalty=evidence_penalty,
            legs=legs,
        )

    async def recommend(
        self,
        plan: StayRecommendationPlan,
        *,
        observed_at: datetime | None = None,
    ) -> StayRecommendationOutput:
        started = observed_at or datetime.now(UTC)
        searched_scopes: list[str] = []
        candidates_by_id: dict[str, StayCandidate] = {}
        search_failures: list[str] = []
        external_calls = 0
        for radius in (2000, 4000, 8000, None):
            searched_scopes.append("同城" if radius is None else f"{radius // 1000}公里")
            try:
                found = await self.candidate_provider.search(
                    city=plan.city,
                    longitude=plan.center_longitude,
                    latitude=plan.center_latitude,
                    radius_m=radius,
                )
            except PlaceProviderUnavailableError as exc:
                search_failures.append(exc.category)
                external_calls += exc.external_call_count
                continue
            for candidate in found:
                external_calls += int(candidate.provider_binding.get("external_calls", 0))
                brand = self.brand_registry.match(candidate.name)
                if (
                    brand is None
                    or candidate.category != "住宿"
                    or _city(candidate.city) != _city(plan.city)
                ):
                    continue
                candidates_by_id.setdefault(
                    candidate.canonical_place_id,
                    candidate.model_copy(update={"brand": brand, "search_radius_m": radius}),
                )
            if len(candidates_by_id) >= 12:
                break
        evaluation_set = sorted(
            candidates_by_id.values(),
            key=lambda item: (
                haversine_meters(
                    plan.center_longitude,
                    plan.center_latitude,
                    item.longitude,
                    item.latitude,
                ),
                item.canonical_place_id,
            ),
        )[:12]
        scored_raw = await asyncio.gather(
            *(self._score_candidate(plan, candidate, started) for candidate in evaluation_set)
        )
        scored = sorted(
            (item for item in scored_raw if item is not None),
            key=lambda item: (
                item.total_score,
                item.max_single_leg_minutes,
                item.transfer_count,
                item.candidate.brand or "",
                item.candidate.canonical_place_id,
            ),
        )
        if not scored:
            status: Literal["READY", "PARTIAL", "UNAVAILABLE"] = "UNAVAILABLE"
        elif len(scored) >= 3 and not search_failures and not any(item.missing_leg_count for item in scored[:3]):
            status = "READY"
        else:
            status = "PARTIAL"
        snapshot_payload = {
            "plan_ref": plan.plan_ref.model_dump(mode="json"),
            "policy_hash": STAY_POLICY_SHA256,
            "searched_scopes": searched_scopes,
            "candidates": [item.model_dump(mode="json") for item in scored],
        }
        finished = started if observed_at is not None else datetime.now(UTC)
        return StayRecommendationOutput(
            plan_ref=plan.plan_ref,
            policy_hash=STAY_POLICY_SHA256,
            status=status,
            area_summary=f"{plan.city}行程首末站附近",
            searched_scopes=searched_scopes,
            candidates=scored,
            snapshot_sha256=canonical_sha256(snapshot_payload),
            provider_binding={
                "candidate_provider_calls": external_calls,
                "route_external_calls": sum(
                    fact.external_call_count
                    for item in scored
                    for leg in item.legs
                    for fact in (leg.walking, leg.transit)
                ),
                "policy_version": STAY_POLICY_VERSION,
                "brand_registry_sha256": HOTEL_BRAND_REGISTRY_SHA256,
                "raw_provider_response_retained": False,
            },
            failure={"search_failures": search_failures} if status != "READY" else {},
            started_at=started,
            finished_at=finished,
            observed_at=started,
        )


def stay_plan_from_map(plan: MapRenderPlan) -> StayRecommendationPlan | None:
    by_day: dict[int, list[MapStop]] = {}
    for stop in sorted(plan.stops, key=lambda item: (item.day_index, item.sequence_index)):
        by_day.setdefault(stop.day_index, []).append(stop)
    if len(by_day) < 2:
        return None
    overnight_days = sorted(by_day)[:-1]
    anchors: list[StayAnchor] = []
    points: list[tuple[float, float]] = []
    for day_index in overnight_days:
        day_stops = by_day[day_index]
        if not day_stops:
            continue
        first, last = day_stops[0], day_stops[-1]
        for direction, stop in (("STAY_TO_FIRST", first), ("LAST_TO_STAY", last)):
            if stop.longitude is None or stop.latitude is None:
                continue
            anchors.append(StayAnchor(day_index=day_index, direction=direction, stop=stop))
            points.append((stop.longitude, stop.latitude))
    center = geometric_median(points)
    city = next((stop.city for stop in plan.stops if stop.city), None)
    if center is None or city is None or not anchors:
        return None
    return StayRecommendationPlan(
        understanding_id=plan.understanding_id,
        plan_ref=plan.plan_ref,
        city=city,
        center_longitude=center[0],
        center_latitude=center[1],
        overnight_days=overnight_days,
        anchors=anchors,
    )
