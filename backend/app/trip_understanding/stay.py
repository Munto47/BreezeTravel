from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

import httpx
from pydantic import Field

from app.constraints.amap_types import classify_amap_type_signals, typecodes_for_category
from app.schemas.place import PlaceCategory
from app.trip_understanding.candidates import _CITY_BOUNDS
from app.trip_understanding.city_scope import CityScopeLookup
from app.trip_understanding.amap_place import _admin_matches
from app.trip_understanding.overnight_context import lodging_role_message, overnight_segments, stay_context_hash
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
from app.trip_understanding.pipeline import atomic_place_rejection_reason, canonical_sha256


_ROOT = Path(__file__).resolve().parent
_BRAND_PATH = _ROOT / "hotel_brand_registry_v1.json"
_FIXTURE_PATH = _ROOT.parents[0] / "data" / "amap_mock_places.json"
_BRAND_BYTES = _BRAND_PATH.read_bytes()
_BRAND_PAYLOAD = json.loads(_BRAND_BYTES.decode("utf-8"))
HOTEL_BRAND_REGISTRY_SHA256 = hashlib.sha256(_BRAND_BYTES).hexdigest()

STAY_POLICY_VERSION = "stay-scoring-v5-verified-branches-only"
STAY_RECALL_CAP = 12
STAY_EVALUATION_CAP = 6
STAY_PUBLIC_CAP = 3
STAY_SEGMENT_ROUTE_BUDGET = 96
STAY_TASK_ROUTE_BUDGET = 192
STAY_POLICY_SHA256 = canonical_sha256(
    {
        "version": STAY_POLICY_VERSION,
        "candidate_query_keyword": "连锁酒店",
        "search_radii_m": [2000, 4000, 8000, None],
        "candidate_cap": 12,
        "evaluation_cap": 6,
        "segment_route_budget": 96,
        "task_route_budget": 192,
        "public_cap": 3,
        "area_search_cap": 2,
        "official_property_search_cap": 2,
        "preferred_brand_average_minutes_tolerance": 10,
        "single_mode_penalty": 8,
        "missing_leg_ranking_penalty": 120,
        "public_maximum": "verified_unexpired_legs_only",
        "city_scope": "continuous_overnight_segments",
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


def stay_plan_spans_cities(plan: MapRenderPlan) -> bool:
    return len({_city(stop.city) for stop in plan.stops if stop.city and _city(stop.city)}) > 1


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
        self.metadata = {item["brand"]: item for item in source.get("brands", [])}
        self.properties = source.get("properties", [])
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
        if re.search(r"(?:旁|对面|附近|隔壁|原址)|民宿|公寓|质量整改|暂停营业|停业", candidate):
            return None
        # Some verified map names omit 酒店. This is only a name hint; identity()
        # still requires this specific POI ID and complete recorded address.
        for prop in self.properties:
            if any(_normalized(str(row.get("name", ""))) == candidate for row in prop.get("provider_matches", [])):
                return prop["brand"]
        matches = [
            (brand, alias)
            for brand, aliases in self.entries
            for alias in aliases
            if alias and alias in candidate and ("酒店" in candidate or "饭店" in candidate or "宾馆" in candidate)
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda item: (-len(item[1]), item[0]))[0][0]

    def identity(self, candidate: "StayCandidate", brand: str) -> dict[str, object]:
        metadata = self.metadata.get(brand, {})
        # Both the branch's name and street number must match the official page.
        address = _normalized(candidate.area_or_address)
        names = _normalized(candidate.name)
        for item in self.properties:
            name_tokens, address_tokens = item.get("name_tokens", []), item.get("address_tokens", [])
            if (not isinstance(name_tokens, list) or len(name_tokens) < 2
                or not all(isinstance(token, str) and token.strip() for token in name_tokens)
                or not isinstance(address_tokens, list) or not address_tokens
                or not all(isinstance(token, str) and token.strip() for token in address_tokens)):
                continue
            if (item.get("brand") != brand or _city(item.get("city", "")) != _city(candidate.city)
                    or not all(_normalized(token) in address for token in address_tokens)):
                continue
            mappings = item.get("provider_matches", [])
            matched = next((row for row in mappings if row.get("provider") == "AMAP"
                and row.get("poi_id") and candidate.canonical_place_id.removeprefix("amap:") == row["poi_id"]
                and _city(str(row.get("city", ""))) == _city(candidate.city)
                and row.get("name") and names == _normalized(row["name"])
                and row.get("address") and address == _normalized(row["address"])
                and row.get("checked_at")), None)
            # A mapped branch cannot fall back to name-only matching when the
            # POI ID or full address changes. Such a change needs a new audit.
            if matched or (not mappings and all(_normalized(token) in names for token in name_tokens)):
                return {"brand_group": metadata.get("group"), "brand_priority": metadata.get("priority", 1),
                        "property_identity": "OFFICIAL_NAME_ADDRESS", "official_url": item["source_url"],
                        "checked_at": item["checked_at"], "property_status": item.get("status", "LISTED"),
                        **({"identity_method": "EXPLICIT_AMAP_BRANCH", "map_checked_at": matched["checked_at"]} if matched else {})}
        return {"brand_group": None, "brand_priority": 1, "property_identity": "NAME_ONLY"}

    def property_seeds(self, city: str, longitude: float, latitude: float) -> list[dict[str, Any]]:
        properties = [p for p in self.properties if _city(p.get("city", "")) == _city(city)
            and p.get("status") == "LISTED" and p.get("name")]

        def proximity(prop):
            distances = []
            for mapping in prop.get("provider_matches", []):
                try:
                    lng, lat = (float(part) for part in mapping.get("location", "").split(","))
                except (ValueError, AttributeError):
                    continue
                if -180 <= lng <= 180 and -90 <= lat <= 90:
                    distances.append(haversine_meters(longitude, latitude, lng, lat))
            return min(distances, default=math.inf)

        # Recorded coordinates select one search seed, never substitute for a
        # fresh place response or claim a travel time.
        return sorted(properties, key=proximity)


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
    segments: list["StaySegmentPlan"] = Field(default_factory=list)
    context_hash: str = ""


class StaySegmentPlan(StrictModel):
    segment_key: str
    city: str | None = None
    overnight_days: list[int]
    anchors: list[StayAnchor] = Field(default_factory=list)
    preserved_hotels: list[str] = Field(default_factory=list)
    pending_lodging_roles: list[str] = Field(default_factory=list)
    uncertain: bool = False
    expected_boundary_count: int = 0
    missing_boundaries: list[dict[str, object]] = Field(default_factory=list)


StayRecommendationPlan.model_rebuild()


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


def rank_stay_candidates(candidates: list[ScoredStayCandidate]) -> list[ScoredStayCandidate]:
    ranked = sorted(candidates, key=lambda c: (c.missing_leg_count > 0, c.total_score,
        c.max_single_leg_minutes, c.candidate.canonical_place_id))
    preferred = next((c for c in ranked if c.candidate.provider_binding.get("brand_priority") == 0), None)
    if not ranked or preferred is None or preferred is ranked[0]:
        return ranked

    def average(candidate: ScoredStayCandidate) -> float | None:
        if candidate.missing_leg_count or not candidate.legs:
            return None
        facts = [getattr(leg, leg.selected_mode) for leg in candidate.legs if leg.selected_mode]
        if len(facts) != len(candidate.legs) or any(f.duration_minutes is None for f in facts):
            return None
        return sum(f.duration_minutes for f in facts) / len(facts)

    best_average, preferred_average = average(ranked[0]), average(preferred)
    ranked.remove(preferred)
    # Compare real selected-route minutes, without score penalties or invented durations.
    if best_average is not None and preferred_average is not None and preferred_average - best_average <= 10:
        ranked.insert(0, preferred)
    else:
        ranked.insert(min(2, len(ranked)), preferred)
    return ranked


@dataclass(frozen=True)
class StayCommuteAssessment:
    maximum_minutes: int | None
    transfer_count: int
    missing_leg_count: int
    leg_count: int
    observed_at: datetime | None
    valid_until: datetime | None

    @property
    def complete(self) -> bool:
        return self.leg_count > 0 and self.missing_leg_count == 0


def assess_stay_commute(legs: list, *, now: datetime, expected_missing: int = 0) -> StayCommuteAssessment:
    """Rebuild display/check values from legs, including historical scored rows."""
    minutes, observations, expirations = [], [], []
    transfers = 0
    for leg in legs:
        data = leg.model_dump() if hasattr(leg, "model_dump") else dict(leg)
        mode = data.get("selected_mode")
        fact = data.get(mode) if isinstance(mode, str) and mode in {"walking", "transit"} else None
        if not isinstance(fact, dict):
            continue
        duration = fact.get("duration_minutes")
        observed, expires = fact.get("observed_at"), fact.get("expires_at")
        if (
            fact.get("mode") != mode or fact.get("status") != "AVAILABLE"
            or type(duration) is not int or duration <= 0
            or not isinstance(observed, datetime) or not isinstance(expires, datetime)
            or observed.tzinfo is None or expires.tzinfo is None
            or observed > now or expires <= now
        ):
            continue
        minutes.append(duration)
        observations.append(observed)
        expirations.append(expires)
        count = fact.get("transfer_count")
        transfers += count if type(count) is int and count >= 0 else 0
    missing = max(expected_missing, len(legs) - len(minutes), 1 if not legs else 0)
    return StayCommuteAssessment(
        maximum_minutes=max(minutes) if minutes else None,
        transfer_count=transfers, missing_leg_count=missing, leg_count=len(legs),
        observed_at=max(observations) if observations else None,
        valid_until=min(expirations) if expirations else None,
    )


async def load_stay_commute_assessment(conn, candidate_id: str, *, now: datetime, expected_missing: int = 0) -> StayCommuteAssessment:
    rows = await conn.fetch(
        """
        SELECT l.selected_mode, f.mode, f.status, f.duration_minutes,
               f.transfer_count, f.observed_at, f.expires_at
        FROM trip_stay_commute_legs l
        LEFT JOIN trip_stay_commute_mode_facts f
          ON f.leg_id = l.leg_id AND f.mode = l.selected_mode
        WHERE l.candidate_id = $1
        ORDER BY l.day_index, l.direction
        """,
        candidate_id,
    )
    legs = [{"selected_mode": row["selected_mode"], row["selected_mode"] or "missing": dict(row)} for row in rows]
    return assess_stay_commute(legs, now=now, expected_missing=expected_missing)


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


class StaySearchRows(list):
    def __init__(self, rows, *, external_calls: int = 0, administrative_calls: int = 0):
        super().__init__(rows)
        self.external_calls = external_calls
        self.administrative_calls = administrative_calls


class StayRouteBudget:
    def __init__(self, task_limit: int = STAY_TASK_ROUTE_BUDGET):
        self.task_limit = task_limit
        self.attempts = 0
        self.actual_calls = 0
        self.cache_hits = 0
        self.segment_attempts: dict[str, int] = {}
        self.semaphore = asyncio.Semaphore(4)
        self.locks: dict[tuple, asyncio.Lock] = {}


class ControlledStayCandidateProvider:
    def __init__(self, path: Path = _FIXTURE_PATH) -> None:
        raw = path.read_bytes()
        self.snapshot_sha256 = hashlib.sha256(raw).hexdigest()
        self.payload = json.loads(raw.decode("utf-8"))
        if path == _FIXTURE_PATH:
            # Demonstration-only frozen facts; the provider remains offline.
            for prop in _BRAND_PAYLOAD.get("properties", []):
                if prop.get("status") != "LISTED":
                    continue
                for match in prop.get("provider_matches", []):
                    lng, lat = (float(part) for part in match["location"].split(","))
                    self.payload.setdefault(prop["city"], []).append({"category": "hotel",
                        "place_id": f"amap:{match['poi_id']}", "name": match["name"], "address": match["address"],
                        "coords": {"lng": lng, "lat": lat}})
            self.snapshot_sha256 = canonical_sha256(self.payload)

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
        self.city_scopes = CityScopeLookup()

    async def search(
        self,
        *,
        city: str,
        longitude: float,
        latitude: float,
        radius_m: int | None,
        query_keyword: str = "连锁酒店",
    ) -> list[StayCandidate]:
        scope = None
        scope_receipt: dict = {}
        if _city(city) not in _CITY_BOUNDS:
            if self.client is not None:
                scope = await self.city_scopes.get(city, client=self.client, api_key=self.api_key, timeout=self.deadline_seconds, receipt=scope_receipt)
            else:
                async with httpx.AsyncClient(timeout=self.deadline_seconds) as client:
                    scope = await self.city_scopes.get(city, client=client, api_key=self.api_key, timeout=self.deadline_seconds, receipt=scope_receipt)
        bounds = scope.bounds if scope else _CITY_BOUNDS.get(_city(city))
        if bounds is None:
            return StaySearchRows([], administrative_calls=int(scope_receipt.get("external_calls", 0)))
        west, east, south, north = bounds
        if not (west <= longitude <= east and south <= latitude <= north):
            return StaySearchRows([], administrative_calls=int(scope_receipt.get("external_calls", 0)))
        endpoint = AMAP_AROUND_ENDPOINT if radius_m is not None else AMAP_TEXT_ENDPOINT
        typecodes = typecodes_for_category(PlaceCategory.HOTEL)
        params: dict[str, object] = {
            "key": self.api_key,
            "types": "|".join(typecodes),
            "keywords": query_keyword,
            "region": city,
            "city_limit": "true",
            "page_size": 25,
            "page_num": 1,
            "output": "json",
        }
        if radius_m is not None:
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
                provider_binding={"provider": "AMAP_STAY_V5", "request_sha256": request_hash,
                    "administrative_calls": int(scope_receipt.get("external_calls", 0))},
                external_call_count=1,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise PlaceProviderUnavailableError(
                "PROVIDER_UNAVAILABLE",
                provider_binding={"provider": "AMAP_STAY_V5", "request_sha256": request_hash,
                    "administrative_calls": int(scope_receipt.get("external_calls", 0))},
                external_call_count=1,
            ) from exc
        if not isinstance(payload, dict) or payload.get("status") != "1":
            raise PlaceProviderUnavailableError(
                "PROVIDER_STATUS_ERROR",
                provider_binding={"provider": "AMAP_STAY_V5", "request_sha256": request_hash,
                    "administrative_calls": int(scope_receipt.get("external_calls", 0))},
                external_call_count=1,
            )
        response_hash = canonical_sha256(payload)
        result: list[StayCandidate] = []
        pois = payload.get("pois")
        for item in pois if isinstance(pois, list) else []:
            if not isinstance(item, dict):
                continue
            signals = classify_amap_type_signals(str(item.get("typecode") or ""), str(item.get("type") or ""))
            coordinates = _coordinates(item.get("location"))
            provider_id = str(item.get("id") or "").strip()
            provider_city = str(item.get("cityname") or item.get("pname") or "")
            name = str(item.get("name") or "").strip()
            if (not signals.complete or signals.conflict or signals.category != PlaceCategory.HOTEL
                or coordinates is None or not provider_id or _city(provider_city) != _city(city)
                or not (scope.matches(item) if scope else _admin_matches(item, expected_city=_city(city), expected_district=None))
                or atomic_place_rejection_reason("".join(name.split())) is not None):
                continue
            if not (west <= coordinates[0] <= east and south <= coordinates[1] <= north):
                continue
            if radius_m is not None and haversine_meters(longitude, latitude, *coordinates) > radius_m:
                continue
            address = item.get("address")
            if isinstance(address, list):
                address = "".join(str(value) for value in address)
            result.append(
                StayCandidate(
                    canonical_place_id=provider_id,
                    name=name,
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
        return StaySearchRows(result, external_calls=1, administrative_calls=int(scope_receipt.get("external_calls", 0)))

    async def search_area(self, *, city: str, area: str, longitude: float, latitude: float) -> list[StayCandidate]:
        rows = await self.search(city=city, longitude=longitude, latitude=latitude,
            radius_m=None, query_keyword=f"{area} 酒店")
        for candidate in rows:
            candidate.provider_binding["area_search_seed"] = area
        return rows

    async def search_property(self, *, city: str, name: str, longitude: float, latitude: float) -> list[StayCandidate]:
        return await self.search(city=city, longitude=longitude, latitude=latitude,
            radius_m=None, query_keyword=name)


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
        task_route_budget: int = STAY_TASK_ROUTE_BUDGET,
    ) -> None:
        self.candidate_provider = candidate_provider or ControlledStayCandidateProvider()
        self.route_provider = route_provider or ControlledStayRouteProvider()
        self.brand_registry = brand_registry or HotelBrandRegistry()
        self.task_route_budget = max(0, task_route_budget)
        self.route_cache: dict[tuple, InternalRouteModeFact] = {}

    async def _mode(
        self,
        origin: MapStop,
        destination: MapStop,
        mode: Literal["walking", "transit"],
        observed_at: datetime,
        now_provider: Callable[[], datetime],
        budget: StayRouteBudget,
        segment_key: str,
    ) -> InternalRouteModeFact:
        key = (origin.longitude, origin.latitude, destination.longitude, destination.latitude, origin.city, destination.city, mode)
        async with budget.locks.setdefault(key, asyncio.Lock()):
            cached = self.route_cache.get(key)
            current = now_provider()
            if cached and cached.observed_at <= current < cached.expires_at:
                budget.cache_hits += 1
                return cached.model_copy(update={"external_call_count": 0,
                    "provider_binding": {**cached.provider_binding, "cache_hit": True}})
            async with budget.semaphore:
                if budget.attempts >= budget.task_limit or budget.segment_attempts.get(segment_key, 0) >= STAY_SEGMENT_ROUTE_BUDGET:
                    return _unavailable_route_fact(mode, category="STAY_ROUTE_BUDGET_REACHED", observed_at=observed_at)
                budget.attempts += 1
                budget.segment_attempts[segment_key] = budget.segment_attempts.get(segment_key, 0) + 1
                try:
                    fact = await self.route_provider.route(origin, destination, mode, observed_at=observed_at)
                    budget.actual_calls += fact.external_call_count
                    checked_at = now_provider()
                    if (fact.mode != mode or fact.status != "AVAILABLE"
                        or type(fact.duration_minutes) is not int or fact.duration_minutes <= 0
                        or fact.observed_at > checked_at or fact.expires_at <= checked_at):
                        return _unavailable_route_fact(mode, category="ROUTE_FACT_NOT_USABLE", observed_at=observed_at,
                            provider_binding=fact.provider_binding, external_calls=fact.external_call_count)
                    if len(self.route_cache) >= 512:
                        self.route_cache.pop(next(iter(self.route_cache)))
                    self.route_cache[key] = fact
                    return fact
                except RouteProviderUnavailableError as exc:
                    budget.actual_calls += exc.external_call_count
                    return _unavailable_route_fact(mode, category=exc.category, observed_at=observed_at,
                        provider_binding=exc.provider_binding, external_calls=exc.external_call_count)

    async def _score_candidate(
        self,
        plan: StayRecommendationPlan,
        candidate: StayCandidate,
        observed_at: datetime,
        now_provider: Callable[[], datetime],
        budget: StayRouteBudget,
        segment_key: str,
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
        ranking_minutes: list[int] = []
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
                self._mode(origin, destination, "walking", observed_at, now_provider, budget, segment_key),
                self._mode(origin, destination, "transit", observed_at, now_provider, budget, segment_key),
            )
            selected_mode = choose_route_mode(walking, transit)
            if selected_mode is None:
                missing_legs += 1
                ranking_minutes.append(120)
                evidence_penalty += 90
            else:
                selected = walking if selected_mode == "walking" else transit
                assert selected.duration_minutes is not None
                selected_minutes.append(selected.duration_minutes)
                ranking_minutes.append(selected.duration_minutes)
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
        if not selected_minutes:
            return None
        evidence_penalty = min(240, evidence_penalty)
        maximum = max(selected_minutes)
        total_score = sum(ranking_minutes) + 0.5 * max(ranking_minutes) + 8 * transfer_count + evidence_penalty
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
        self, plan: StayRecommendationPlan, *, observed_at: datetime | None = None,
    ) -> StayRecommendationOutput:
        monotonic_started = time.perf_counter()
        started = observed_at or datetime.now(UTC)

        def operation_now() -> datetime:
            return (datetime.now(UTC) if observed_at is None else
                    started + timedelta(seconds=time.perf_counter() - monotonic_started))

        segments = plan.segments or [StaySegmentPlan(segment_key="legacy", city=plan.city,
            overnight_days=plan.overnight_days, anchors=plan.anchors)]
        budget = StayRouteBudget(self.task_route_budget)
        scored_all, segment_results, scopes_all, failures = [], [], [], []
        poi_calls = administrative_calls = 0
        for segment in segments:
            metadata = {"segment_key": segment.segment_key, "city": segment.city,
                        "overnight_days": segment.overnight_days,
                        "preserved_hotels": segment.preserved_hotels, "status": "UNAVAILABLE",
                        "pending_lodging_roles": segment.pending_lodging_roles,
                        "expected_boundary_count": segment.expected_boundary_count,
                        "missing_boundary_count": len(segment.missing_boundaries), "missing_boundaries": segment.missing_boundaries}
            if segment.pending_lodging_roles:
                metadata.update(status="LIMITED", message=lodging_role_message(segment.pending_lodging_roles))
                segment_results.append(metadata)
                continue
            if segment.preserved_hotels:
                metadata.update(status="PRESERVED", message=("已保留原住宿；酒店位置或行程首末站还需确认"
                    if segment.missing_boundaries else "保留原行程中的住宿，不自动更换"))
                segment_results.append(metadata)
                continue
            if segment.uncertain or not segment.city or not segment.anchors:
                metadata.update(status="LIMITED", message=f"这晚有{len(segment.missing_boundaries) or 1}处首末站或过夜城市尚未确认，补全后再比较住宿")
                segment_results.append(metadata)
                continue
            if any(_city(a.stop.city or "") != _city(segment.city) for a in segment.anchors):
                raise ValueError("stay segment anchor city does not match")
            center = geometric_median([(a.stop.longitude, a.stop.latitude) for a in segment.anchors])
            segment_plan = plan.model_copy(update={"city": segment.city, "anchors": segment.anchors,
                "overnight_days": segment.overnight_days, "center_longitude": center[0], "center_latitude": center[1]})
            recalled: dict[str, StayCandidate] = {}
            unverified: set[str] = set()
            searched, search_failures = [], []
            for radius in (2000, 4000, 8000, None):
                searched.append("同城" if radius is None else f"{radius // 1000}公里")
                try:
                    found = await self.candidate_provider.search(city=segment.city,
                        longitude=center[0], latitude=center[1], radius_m=radius)
                except PlaceProviderUnavailableError as exc:
                    search_failures.append(exc.category)
                    if exc.category == "CITY_SCOPE_UNAVAILABLE":
                        administrative_calls += exc.external_call_count
                    else:
                        poi_calls += exc.external_call_count
                        administrative_calls += int(exc.provider_binding.get("administrative_calls", 0))
                    continue
                poi_calls += getattr(found, "external_calls", 0)
                administrative_calls += getattr(found, "administrative_calls", 0)
                for candidate in found:
                    brand = self.brand_registry.match(candidate.name)
                    if brand is None or candidate.category != "住宿" or _city(candidate.city) != _city(segment.city):
                        continue
                    identity = self.brand_registry.identity(candidate, brand)
                    if identity.get("property_identity") != "OFFICIAL_NAME_ADDRESS":
                        unverified.add(candidate.canonical_place_id)
                        continue
                    if identity.get("property_status") != "LISTED":
                        continue
                    binding = {**candidate.provider_binding, **identity, "segment_key": segment.segment_key,
                               "overnight_days": segment.overnight_days, "context_hash": plan.context_hash}
                    recalled.setdefault(candidate.canonical_place_id, candidate.model_copy(update={
                        "brand": brand if identity["property_identity"] == "OFFICIAL_NAME_ADDRESS" else None,
                        "search_radius_m": radius, "provider_binding": binding}))
                if len(recalled) >= STAY_RECALL_CAP:
                    break
            area_search = getattr(self.candidate_provider, "search_area", None)
            if area_search is not None:
                from app.trip_understanding.city_knowledge import get_city_knowledge

                areas = [e for e in get_city_knowledge().entities if e.city == segment.city
                         and e.kind in {"commercial_area", "stay_area"} and "stay_search" in e.uses]
                anchor_names = " ".join(a.stop.name for a in segment.anchors)
                areas.sort(key=lambda e: (-sum(name in anchor_names for name in (e.canonical_name, *e.aliases)), e.entity_id))
                for area in areas[:2]:
                    try:
                        found = await area_search(city=segment.city, area=area.canonical_name, longitude=center[0], latitude=center[1])
                    except PlaceProviderUnavailableError as exc:
                        search_failures.append(exc.category)
                        if exc.category == "CITY_SCOPE_UNAVAILABLE":
                            administrative_calls += exc.external_call_count
                        else:
                            poi_calls += exc.external_call_count
                            administrative_calls += int(exc.provider_binding.get("administrative_calls", 0))
                        continue
                    poi_calls += getattr(found, "external_calls", 0)
                    administrative_calls += getattr(found, "administrative_calls", 0)
                    searched.append(f"区域：{area.canonical_name}")
                    for candidate in found:
                        brand = self.brand_registry.match(candidate.name)
                        if brand is None or candidate.category != "住宿" or _city(candidate.city) != _city(segment.city):
                            continue
                        identity = self.brand_registry.identity(candidate, brand)
                        if identity.get("property_identity") != "OFFICIAL_NAME_ADDRESS":
                            unverified.add(candidate.canonical_place_id)
                            continue
                        if identity.get("property_status") != "LISTED":
                            continue
                        binding = {**candidate.provider_binding, **identity, "segment_key": segment.segment_key,
                            "overnight_days": segment.overnight_days, "context_hash": plan.context_hash,
                            "area_search_seed": area.canonical_name}
                        if candidate.canonical_place_id in recalled:
                            recalled[candidate.canonical_place_id].provider_binding["area_search_seed"] = area.canonical_name
                        else:
                            recalled[candidate.canonical_place_id] = candidate.model_copy(update={
                                "brand": brand if identity["property_identity"] == "OFFICIAL_NAME_ADDRESS" else None,
                                "provider_binding": binding})
            property_search = getattr(self.candidate_provider, "search_property", None)
            if property_search is not None:
                properties = self.brand_registry.property_seeds(segment.city, *center)
                # One verified Huazhu branch plus one other verified chain.
                # A second brand in Huazhu does not replace another group.
                verified_seeds = [p for p in properties if p.get("provider_matches")]
                seeds = [next((p for p in verified_seeds
                    if (self.brand_registry.metadata.get(p["brand"], {}).get("group") == "华住") == preferred), None)
                    for preferred in (True, False)]
                for property_seed in (p for p in seeds if p is not None):
                    try:
                        search_name = next((m["name"] for m in property_seed.get("provider_matches", []) if m.get("name")), property_seed["name"])
                        found = await property_search(city=segment.city, name=search_name,
                            longitude=center[0], latitude=center[1])
                    except PlaceProviderUnavailableError as exc:
                        search_failures.append(exc.category)
                        if exc.category == "CITY_SCOPE_UNAVAILABLE":
                            administrative_calls += exc.external_call_count
                        else:
                            poi_calls += exc.external_call_count
                            administrative_calls += int(exc.provider_binding.get("administrative_calls", 0))
                        continue
                    poi_calls += getattr(found, "external_calls", 0)
                    administrative_calls += getattr(found, "administrative_calls", 0)
                    searched.append(f"门店：{property_seed['name']}")
                    for candidate in found:
                        brand = self.brand_registry.match(candidate.name)
                        if brand is None or candidate.category != "住宿" or _city(candidate.city) != _city(segment.city):
                            continue
                        identity = self.brand_registry.identity(candidate, brand)
                        if identity.get("property_identity") != "OFFICIAL_NAME_ADDRESS" or identity.get("property_status") != "LISTED":
                            continue
                        binding = {**candidate.provider_binding, **identity, "segment_key": segment.segment_key,
                            "overnight_days": segment.overnight_days, "context_hash": plan.context_hash}
                        recalled[candidate.canonical_place_id] = candidate.model_copy(update={"brand": brand, "provider_binding": binding})
            recalled_sorted = sorted(recalled.values(), key=lambda c: (haversine_meters(*center, c.longitude, c.latitude), c.canonical_place_id))[:STAY_RECALL_CAP]
            verified = [c for c in recalled.values() if c.provider_binding.get("property_identity") == "OFFICIAL_NAME_ADDRESS"]
            if verified and all(c.canonical_place_id not in {v.canonical_place_id for v in verified} for c in recalled_sorted):
                recalled_sorted = recalled_sorted[:STAY_RECALL_CAP - 1] + [min(verified,
                    key=lambda c: haversine_meters(*center, c.longitude, c.latitude))]
            # Reserve measured places for nearby chains as well as preferred brands.
            nearby = recalled_sorted[:3]
            preferred = sorted(recalled_sorted, key=lambda c: (c.provider_binding.get("brand_priority", 1),
                haversine_meters(*center, c.longitude, c.latitude), c.canonical_place_id))
            shortlist = list({c.canonical_place_id: c for c in [*nearby, *preferred]}.values())[:STAY_EVALUATION_CAP]
            scored = [item for item in await asyncio.gather(*(self._score_candidate(segment_plan, candidate,
                started, operation_now, budget, segment.segment_key) for candidate in shortlist)) if item is not None]
            scored = rank_stay_candidates(scored)
            for rank, item in enumerate(scored, 1):
                item.candidate.provider_binding["segment_rank"] = rank
            status = ("READY" if len(scored) >= 3 and not search_failures and not any(c.missing_leg_count for c in scored[:3])
                      else "PARTIAL" if scored else "UNAVAILABLE")
            message = "按每晚返回和次日出发比较住宿" if status == "READY" else "部分住宿或通勤信息尚未核对完整"
            if len(scored) < 3:
                message = f"已核验且有可用通勤信息的连锁酒店仅{len(scored)}家；其余门店尚未核实，不补入建议"
            metadata.update(status=status, message=message, searched_scopes=searched,
                unverified_branch_count=len(unverified), verified_branch_count=len(recalled))
            segment_results.append(metadata)
            scored_all.extend(scored)
            scopes_all.extend(f"{segment.city}：{scope}" for scope in searched)
            failures.extend(search_failures)
        status = ("READY" if scored_all and all(s["status"] in {"READY", "PRESERVED"} for s in segment_results)
                  else "PARTIAL" if scored_all else "UNAVAILABLE")
        binding = {"candidate_provider_calls": poi_calls, "administrative_calls": administrative_calls,
                   "route_external_calls": budget.actual_calls, "route_cache_hits": budget.cache_hits,
                   "route_attempts": budget.attempts, "segment_route_attempts": budget.segment_attempts,
                   "task_route_budget": budget.task_limit, "segment_route_budget": STAY_SEGMENT_ROUTE_BUDGET,
                   "policy_version": STAY_POLICY_VERSION, "brand_registry_sha256": HOTEL_BRAND_REGISTRY_SHA256,
                   "context_hash": plan.context_hash, "segments": segment_results, "raw_provider_response_retained": False}
        binding["expected_boundary_count"] = sum(s.expected_boundary_count for s in segments)
        binding["missing_boundary_count"] = sum(len(s.missing_boundaries) for s in segments)
        snapshot = {"plan_ref": plan.plan_ref.model_dump(mode="json"), "policy_hash": STAY_POLICY_SHA256,
                    "segments": segment_results, "candidates": [c.model_dump(mode="json") for c in scored_all]}
        return StayRecommendationOutput(plan_ref=plan.plan_ref, policy_hash=STAY_POLICY_SHA256, status=status,
            area_summary="按连续过夜城市分段比较住宿", searched_scopes=scopes_all, candidates=scored_all,
            snapshot_sha256=canonical_sha256(snapshot), provider_binding=binding,
            failure={"search_failures": failures} if status != "READY" else {},
            started_at=started, finished_at=operation_now(), observed_at=started)


def stay_plan_from_map(plan: MapRenderPlan) -> StayRecommendationPlan | None:
    contexts = overnight_segments(plan)
    if not contexts:
        return None
    segments = [StaySegmentPlan(segment_key=s.key, city=s.city, overnight_days=s.overnight_days,
        anchors=[StayAnchor(day_index=day, direction=direction, stop=stop) for day, direction, stop in s.anchors],
        preserved_hotels=s.preserved_hotels, pending_lodging_roles=s.pending_lodging_roles,
        uncertain=s.uncertain, expected_boundary_count=s.expected_boundary_count,
        missing_boundaries=s.missing_boundaries) for s in contexts]
    anchors = [a for s in segments for a in s.anchors]
    center = geometric_median([(a.stop.longitude, a.stop.latitude) for a in anchors])
    return StayRecommendationPlan(understanding_id=plan.understanding_id, plan_ref=plan.plan_ref,
        city=next((s.city for s in segments if s.city), "目的地待确认"),
        center_longitude=center[0] if center else 0, center_latitude=center[1] if center else 0,
        overnight_days=[day for s in segments for day in s.overnight_days], anchors=anchors,
        segments=segments, context_hash=stay_context_hash(plan))
