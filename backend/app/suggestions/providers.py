from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings
from app.constraints.amap_types import classify_amap_type
from app.constraints.geo_routes import fetch_amap_route
from app.importing.models import ResolvedPlaceReceipt
from app.schemas.place import Coordinates, PlaceCategory, RetrievalExecutionMode
from app.suggestions.models import (
    CandidateCurrentFact,
    FrozenCanonicalPlace,
    RouteReceipt,
    RouteReceiptLeg,
    SuggestionIntent,
)
from app.suggestions.suitability import classify_provider_suitability


def _hash(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class AnchorRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    coords: Coordinates


class ProviderCandidateQuery(BaseModel):
    """Complete spatial query contract for the next-stop Provider call.

    An append query is centred on ``anchor_coords``.  An insertion query binds
    both ends of the exact edge.  City/category-only retrieval is impossible to
    represent with this model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    city: str = Field(min_length=1)
    intents: tuple[SuggestionIntent, ...] = Field(min_length=1)
    typecodes: tuple[str, ...] = Field(min_length=1)
    radius_m: int = Field(ge=100, le=50_000)
    anchor_name: str = Field(min_length=1)
    anchor_place_id: str | None = Field(default=None, min_length=1)
    anchor_coords: Coordinates | None = None
    anchor_role: Literal["PREVIOUS", "NEXT"] = "PREVIOUS"
    previous_anchor: AnchorRef | None = None
    next_anchor: AnchorRef | None = None
    keywords: tuple[str, ...] = Field(default_factory=tuple)
    transport_mode: str = Field(default="walking", pattern=r"^(walking|driving|transit)$")

    @model_validator(mode="after")
    def require_spatial_anchor(self) -> "ProviderCandidateQuery":
        has_edge_end = self.previous_anchor is not None or self.next_anchor is not None
        if has_edge_end and not (self.previous_anchor and self.next_anchor):
            raise ValueError("an insertion query requires both previous_anchor and next_anchor")
        if self.anchor_coords is None and not (self.previous_anchor and self.next_anchor):
            raise ValueError("candidate query requires anchor coordinates or an explicit insertion edge")
        if self.anchor_coords is not None and self.anchor_place_id is None:
            raise ValueError("an anchor-coordinate query requires its canonical place id")
        if len(set(self.intents)) != len(self.intents):
            raise ValueError("candidate query intents must be unique")
        if len(set(self.typecodes)) != len(self.typecodes):
            raise ValueError("candidate query typecodes must be unique")
        return self

    @property
    def search_center(self) -> Coordinates:
        if self.anchor_coords is not None:
            return self.anchor_coords
        assert self.previous_anchor is not None and self.next_anchor is not None
        return Coordinates(
            lng=(self.previous_anchor.coords.lng + self.next_anchor.coords.lng) / 2,
            lat=(self.previous_anchor.coords.lat + self.next_anchor.coords.lat) / 2,
        )


class ProviderCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_place: FrozenCanonicalPlace
    provider_receipt: ResolvedPlaceReceipt
    popularity: float = Field(default=0.0, ge=0, le=1)
    content_relevance: float = Field(default=0.0, ge=0, le=1)
    member_suitability: float = Field(default=0.5, ge=0, le=1)
    budget_fit: float = Field(default=0.5, ge=0, le=1)
    soft_preference: float = Field(default=0.5, ge=0, le=1)
    diversity_tags: tuple[str, ...] = ()
    official_prior_refs: tuple[str, ...] = ()
    official_route_prior: float = Field(default=0.0, ge=0, le=1)
    hard_block_codes: tuple[str, ...] = ()
    current_facts: tuple[CandidateCurrentFact, ...] = ()

    @model_validator(mode="after")
    def bind_receipt(self) -> "ProviderCandidate":
        place = self.canonical_place
        receipt = self.provider_receipt
        if (
            receipt.canonical_place_id != place.place_id
            or receipt.name != place.name
            or receipt.city != place.city
            or receipt.longitude != place.coords.lng
            or receipt.latitude != place.coords.lat
        ):
            raise ValueError("provider candidate facts must be bound to its materialization receipt")
        return self


class ProviderCandidateBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_snapshot_id: str = Field(min_length=1)
    candidates: tuple[ProviderCandidate, ...] = ()
    retrieved_at: datetime

    @model_validator(mode="after")
    def validate_time(self) -> "ProviderCandidateBatch":
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("provider batch retrieved_at must be timezone-aware")
        return self


class ProviderCandidateSource(Protocol):
    async def search(self, query: ProviderCandidateQuery) -> ProviderCandidateBatch: ...


class RouteTimes(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(pattern=r"^(AVAILABLE|UNKNOWN)$")
    previous_to_candidate_minutes: int | None = Field(default=None, ge=0)
    candidate_to_next_minutes: int | None = Field(default=None, ge=0)
    previous_to_next_minutes: int | None = Field(default=None, ge=0)
    route_receipts: tuple[RouteReceipt, ...] = ()
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "RouteTimes":
        if self.status == "UNKNOWN" and not self.reason_code:
            raise ValueError("unknown route times require a reason code")
        if self.status == "AVAILABLE":
            values = (
                self.previous_to_candidate_minutes,
                self.candidate_to_next_minutes,
                self.previous_to_next_minutes,
            )
            if not (
                (values[0] is not None and values[1:] == (None, None))
                or (values[1] is not None and values[0] is None and values[2] is None)
                or all(value is not None for value in values)
            ):
                raise ValueError("available route times require one anchor leg or one complete insertion edge")
        return self


class CandidateRouteSource(Protocol):
    async def route_times(self, query: ProviderCandidateQuery, candidate: ProviderCandidate) -> RouteTimes: ...


class ControlledCandidateFact(BaseModel):
    """Raw deterministic fixture fact converted to a full provider receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    place_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    city: str = Field(min_length=1)
    category: PlaceCategory
    coords: Coordinates
    district: str | None = None
    address: str | None = None
    provider_raw_type: str | None = None
    provider_raw_typecode: str | None = None
    popularity: float = Field(default=0.0, ge=0, le=1)
    content_relevance: float = Field(default=0.0, ge=0, le=1)
    member_suitability: float = Field(default=0.5, ge=0, le=1)
    budget_fit: float = Field(default=0.5, ge=0, le=1)
    soft_preference: float = Field(default=0.5, ge=0, le=1)
    diversity_tags: tuple[str, ...] = ()
    official_prior_refs: tuple[str, ...] = ()
    official_route_prior: float = Field(default=0.0, ge=0, le=1)
    hard_block_codes: tuple[str, ...] = ()
    current_facts: tuple[CandidateCurrentFact, ...] = ()


class ControlledSnapshotCandidateSource:
    def __init__(
        self,
        facts: list[ControlledCandidateFact],
        *,
        snapshot_id: str,
        observed_at: datetime,
    ):
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("controlled snapshot observed_at must be timezone-aware")
        self.facts = tuple(facts)
        self.snapshot_id = snapshot_id
        self.observed_at = observed_at
        self.queries: list[ProviderCandidateQuery] = []

    async def search(self, query: ProviderCandidateQuery) -> ProviderCandidateBatch:
        self.queries.append(query)
        request_hash = _hash({"snapshot_id": self.snapshot_id, "query": query.model_dump(mode="json")})
        response_hash = _hash([fact.model_dump(mode="json") for fact in self.facts])
        candidates = tuple(
            _controlled_candidate(fact, request_hash, response_hash, self.observed_at)
            for fact in self.facts
            if _haversine_m(query.search_center, fact.coords) <= query.radius_m
        )
        return ProviderCandidateBatch(
            provider_snapshot_id=self.snapshot_id,
            candidates=candidates,
            retrieved_at=self.observed_at,
        )


class ControlledRouteSource:
    def __init__(self, times_by_place_id: dict[str, RouteTimes]):
        self.times_by_place_id = dict(times_by_place_id)

    async def route_times(self, query: ProviderCandidateQuery, candidate: ProviderCandidate) -> RouteTimes:
        route = self.times_by_place_id.get(
            candidate.canonical_place.place_id,
            RouteTimes(status="UNKNOWN", reason_code="ROUTE_PROVIDER_NO_FIXTURE"),
        )
        if route.status != "AVAILABLE" or route.route_receipts:
            return route
        return route.model_copy(update={
            "route_receipts": _controlled_route_receipts(query, candidate, route),
        })


class AmapCandidateSource:
    """Live Amap around-search adapter with a complete, sanitized receipt."""

    endpoint = "https://restapi.amap.com/v5/place/around"

    def __init__(self, *, api_key: str | None = None, timeout_seconds: float = 8.0):
        self.api_key = api_key if api_key is not None else settings.amap_api_key
        self.timeout_seconds = timeout_seconds

    async def search(self, query: ProviderCandidateQuery) -> ProviderCandidateBatch:
        if not self.api_key:
            raise RuntimeError("AMAP_API_KEY_MISSING")
        center = query.search_center
        params = {
            "key": self.api_key,
            "location": f"{center.lng},{center.lat}",
            "keywords": " ".join(query.keywords) or " ".join(intent.value for intent in query.intents),
            "types": "|".join(query.typecodes),
            "radius": query.radius_m,
            "region": query.city,
            "city_limit": "true",
            "show_fields": "business,photos",
            "page_size": 20,
            "output": "json",
        }
        safe_request = {
            "method": "GET",
            "url": self.endpoint,
            "params": {key: value for key, value in params.items() if key != "key"},
        }
        request_hash = _hash(safe_request)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                self.endpoint,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        observed_at = datetime.now(timezone.utc)
        response_hash = _hash(payload)
        if payload.get("status") != "1":
            raise RuntimeError(f"AMAP_PROVIDER_ERROR:{payload.get('info') or 'unknown'}")
        candidates = tuple(
            candidate
            for raw in payload.get("pois") or []
            if (candidate := _amap_candidate(raw, query, request_hash, response_hash, observed_at)) is not None
        )
        return ProviderCandidateBatch(
            provider_snapshot_id=f"amap-{response_hash}",
            candidates=candidates,
            retrieved_at=observed_at,
        )


class AmapRouteSource:
    """Route-time adapter; failures stay UNKNOWN and never become geo estimates."""

    async def route_times(self, query: ProviderCandidateQuery, candidate: ProviderCandidate) -> RouteTimes:
        candidate_coords = candidate.canonical_place.coords
        candidate_id = candidate.canonical_place.place_id
        if query.previous_anchor is not None:
            origin = query.previous_anchor.coords
            origin_place_id = query.previous_anchor.place_id
            destination = candidate_coords
            destination_place_id = candidate_id
            first_leg = RouteReceiptLeg.PREVIOUS_TO_CANDIDATE
        elif query.anchor_role == "PREVIOUS":
            origin = query.search_center
            origin_place_id = query.anchor_place_id
            destination = candidate_coords
            destination_place_id = candidate_id
            first_leg = RouteReceiptLeg.PREVIOUS_TO_CANDIDATE
        else:
            origin = candidate_coords
            origin_place_id = candidate_id
            destination = query.search_center
            destination_place_id = query.anchor_place_id
            first_leg = RouteReceiptLeg.CANDIDATE_TO_NEXT
        if origin_place_id is None or destination_place_id is None:
            return RouteTimes(status="UNKNOWN", reason_code="ROUTE_ENDPOINT_ID_UNKNOWN")
        async with aiohttp.ClientSession() as session:
            first = await fetch_amap_route(session, origin, destination, query.transport_mode, query.city)
            first_receipt = _amap_route_receipt(
                first,
                leg=first_leg,
                query=query,
                origin_place_id=origin_place_id,
                origin=origin,
                destination_place_id=destination_place_id,
                destination=destination,
            )
            if first.status != "ok" or first.duration_minutes is None or first_receipt is None:
                return RouteTimes(
                    status="UNKNOWN",
                    reason_code=(
                        f"ROUTE_PROVIDER_{(first.failure_reason or 'RECEIPT_INCOMPLETE').upper()}"
                    ),
                )
            if query.next_anchor is None:
                return RouteTimes(
                    status="AVAILABLE",
                    previous_to_candidate_minutes=(
                        first.duration_minutes
                        if first_leg is RouteReceiptLeg.PREVIOUS_TO_CANDIDATE
                        else None
                    ),
                    candidate_to_next_minutes=(
                        first.duration_minutes
                        if first_leg is RouteReceiptLeg.CANDIDATE_TO_NEXT
                        else None
                    ),
                    route_receipts=(first_receipt,),
                )
            second = await fetch_amap_route(
                session,
                candidate_coords,
                query.next_anchor.coords,
                query.transport_mode,
                query.city,
            )
            baseline = await fetch_amap_route(
                session,
                origin,
                query.next_anchor.coords,
                query.transport_mode,
                query.city,
            )
            second_receipt = _amap_route_receipt(
                second,
                leg=RouteReceiptLeg.CANDIDATE_TO_NEXT,
                query=query,
                origin_place_id=candidate_id,
                origin=candidate_coords,
                destination_place_id=query.next_anchor.place_id,
                destination=query.next_anchor.coords,
            )
            baseline_receipt = _amap_route_receipt(
                baseline,
                leg=RouteReceiptLeg.PREVIOUS_TO_NEXT,
                query=query,
                origin_place_id=origin_place_id,
                origin=origin,
                destination_place_id=query.next_anchor.place_id,
                destination=query.next_anchor.coords,
            )
            if (
                second.status != "ok"
                or baseline.status != "ok"
                or second.duration_minutes is None
                or baseline.duration_minutes is None
                or second_receipt is None
                or baseline_receipt is None
            ):
                return RouteTimes(status="UNKNOWN", reason_code="ROUTE_INSERT_EDGE_INCOMPLETE")
            return RouteTimes(
                status="AVAILABLE",
                previous_to_candidate_minutes=first.duration_minutes,
                candidate_to_next_minutes=second.duration_minutes,
                previous_to_next_minutes=baseline.duration_minutes,
                route_receipts=(first_receipt, second_receipt, baseline_receipt),
            )


_AMAP_ROUTE_ENDPOINTS = {
    "walking": "https://restapi.amap.com/v3/direction/walking",
    "driving": "https://restapi.amap.com/v3/direction/driving",
    "transit": "https://restapi.amap.com/v3/direction/transit/integrated",
}


def _route_request_hash(
    *,
    query: ProviderCandidateQuery,
    origin: Coordinates,
    destination: Coordinates,
) -> str:
    return _hash({
        "method": "GET",
        "url": _AMAP_ROUTE_ENDPOINTS[query.transport_mode],
        "params": {
            "origin": f"{origin.lng},{origin.lat}",
            "destination": f"{destination.lng},{destination.lat}",
            "output": "json",
            **(
                {"city": query.city, "cityd": query.city, "strategy": "0"}
                if query.transport_mode == "transit"
                else {}
            ),
        },
    })


def _amap_route_receipt(
    result: Any,
    *,
    leg: RouteReceiptLeg,
    query: ProviderCandidateQuery,
    origin_place_id: str,
    origin: Coordinates,
    destination_place_id: str,
    destination: Coordinates,
) -> RouteReceipt | None:
    """Bind only metadata returned by the actual Amap response.

    The request hash is computed from the exact sanitized request (API key
    omitted).  A missing Provider response hash or observation time is not
    synthesized and makes the route evidence unusable.
    """
    if (
        result.status != "ok"
        or result.duration_minutes is None
        or result.response_hash is None
        or result.observed_at is None
        or not str(result.source).startswith("amap_")
    ):
        return None
    return RouteReceipt(
        leg=leg,
        transport_mode=query.transport_mode,
        origin_place_id=origin_place_id,
        origin_coords=origin,
        destination_place_id=destination_place_id,
        destination_coords=destination,
        duration_minutes=result.duration_minutes,
        provider="amap",
        request_hash=_route_request_hash(query=query, origin=origin, destination=destination),
        response_hash=result.response_hash,
        observed_at=result.observed_at,
        snapshot_id=f"amap-route-{result.response_hash}",
        execution_mode=RetrievalExecutionMode.LIVE,
        max_age_seconds=900,
        source_url=_AMAP_ROUTE_ENDPOINTS[query.transport_mode],
    )


def _controlled_route_receipts(
    query: ProviderCandidateQuery,
    candidate: ProviderCandidate,
    route: RouteTimes,
) -> tuple[RouteReceipt, ...]:
    """Materialize deterministic receipts from the explicit fixture facts."""
    observed_at = candidate.provider_receipt.observed_at
    candidate_id = candidate.canonical_place.place_id
    candidate_coords = candidate.canonical_place.coords
    if query.previous_anchor is not None:
        previous_id = query.previous_anchor.place_id
        previous_coords = query.previous_anchor.coords
    else:
        assert query.anchor_place_id is not None and query.anchor_coords is not None
        previous_id = query.anchor_place_id
        previous_coords = query.anchor_coords

    legs: list[tuple[RouteReceiptLeg, str, Coordinates, str, Coordinates, int]] = []
    if route.previous_to_candidate_minutes is not None:
        legs.append((
            RouteReceiptLeg.PREVIOUS_TO_CANDIDATE,
            previous_id,
            previous_coords,
            candidate_id,
            candidate_coords,
            route.previous_to_candidate_minutes,
        ))
    if route.candidate_to_next_minutes is not None:
        next_id = query.next_anchor.place_id if query.next_anchor is not None else previous_id
        next_coords = query.next_anchor.coords if query.next_anchor is not None else previous_coords
        legs.append((
            RouteReceiptLeg.CANDIDATE_TO_NEXT,
            candidate_id,
            candidate_coords,
            next_id,
            next_coords,
            route.candidate_to_next_minutes,
        ))
    if query.next_anchor is not None and route.previous_to_next_minutes is not None:
        legs.append((
            RouteReceiptLeg.PREVIOUS_TO_NEXT,
            previous_id,
            previous_coords,
            query.next_anchor.place_id,
            query.next_anchor.coords,
            route.previous_to_next_minutes,
        ))
    receipts: list[RouteReceipt] = []
    for leg, origin_id, origin, destination_id, destination, duration in legs:
        fixture_payload = {
            "leg": leg.value,
            "mode": query.transport_mode,
            "origin_place_id": origin_id,
            "origin_coords": origin.model_dump(mode="json"),
            "destination_place_id": destination_id,
            "destination_coords": destination.model_dump(mode="json"),
            "duration_minutes": duration,
        }
        response_hash = _hash(fixture_payload)
        receipts.append(RouteReceipt(
            leg=leg,
            transport_mode=query.transport_mode,
            origin_place_id=origin_id,
            origin_coords=origin,
            destination_place_id=destination_id,
            destination_coords=destination,
            duration_minutes=duration,
            provider="controlled_route_snapshot",
            request_hash=_hash({
                "fixture": "controlled_route_snapshot",
                "query": query.model_dump(mode="json"),
                "candidate_id": candidate_id,
                "leg": leg.value,
            }),
            response_hash=response_hash,
            observed_at=observed_at,
            snapshot_id=f"controlled-route-{response_hash}",
            execution_mode=RetrievalExecutionMode.FIXTURE,
            max_age_seconds=3600,
            source_url=f"fixture://route/{response_hash}",
        ))
    return tuple(receipts)


def _controlled_candidate(
    fact: ControlledCandidateFact,
    request_hash: str,
    response_hash: str,
    observed_at: datetime,
) -> ProviderCandidate:
    place = FrozenCanonicalPlace(
        place_id=fact.place_id,
        name=fact.name,
        city=fact.city,
        district=fact.district,
        address=fact.address,
        category=fact.category.value,
        coords=fact.coords,
    )
    receipt = ResolvedPlaceReceipt(
        canonical_place_id=fact.place_id,
        provider="controlled_snapshot",
        provider_place_id=fact.place_id,
        name=fact.name,
        city=fact.city,
        district=fact.district,
        address=fact.address,
        category=fact.category.value,
        provider_raw_type=fact.provider_raw_type,
        provider_raw_typecode=fact.provider_raw_typecode,
        longitude=fact.coords.lng,
        latitude=fact.coords.lat,
        request_hash=request_hash,
        response_hash=response_hash,
        observed_at=observed_at,
        execution_mode=RetrievalExecutionMode.FIXTURE,
        source_url=f"fixture://{fact.place_id}",
    )
    return ProviderCandidate(
        canonical_place=place,
        provider_receipt=receipt,
        popularity=fact.popularity,
        content_relevance=fact.content_relevance,
        member_suitability=fact.member_suitability,
        budget_fit=fact.budget_fit,
        soft_preference=fact.soft_preference,
        diversity_tags=fact.diversity_tags,
        official_prior_refs=fact.official_prior_refs,
        official_route_prior=fact.official_route_prior,
        hard_block_codes=fact.hard_block_codes,
        current_facts=fact.current_facts,
    )


def _amap_candidate(
    raw: dict[str, Any],
    query: ProviderCandidateQuery,
    request_hash: str,
    response_hash: str,
    observed_at: datetime,
) -> ProviderCandidate | None:
    try:
        place_id = str(raw["id"]).strip()
        name = str(raw["name"]).strip()
        lng_raw, lat_raw = str(raw["location"]).split(",", 1)
        coords = Coordinates(lng=float(lng_raw), lat=float(lat_raw))
        city = str(raw.get("cityname") or query.city).strip().removesuffix("市")
        provider_raw_typecode = str(raw.get("typecode") or "").strip() or None
        provider_raw_type = str(raw.get("type") or "").strip() or None
        category = classify_amap_type(provider_raw_typecode or "", provider_raw_type or "")
        if not place_id or not name or category is PlaceCategory.UNKNOWN:
            return None
        business = raw.get("business") if isinstance(raw.get("business"), dict) else {}
        rating = business.get("rating")
        popularity = min(1.0, max(0.0, float(rating) / 5)) if rating not in (None, "") else 0.0
        place = FrozenCanonicalPlace(
            place_id=place_id,
            name=name,
            city=city,
            district=str(raw.get("adname") or "") or None,
            address=str(raw.get("address") or "") or None,
            category=category.value,
            coords=coords,
        )
        receipt = ResolvedPlaceReceipt(
            canonical_place_id=place_id,
            provider="amap",
            provider_place_id=place_id,
            name=name,
            city=city,
            district=place.district,
            address=place.address,
            category=category.value,
            provider_raw_type=provider_raw_type,
            provider_raw_typecode=provider_raw_typecode,
            longitude=coords.lng,
            latitude=coords.lat,
            request_hash=request_hash,
            response_hash=response_hash,
            observed_at=observed_at,
            execution_mode=RetrievalExecutionMode.LIVE,
            source_url=AmapCandidateSource.endpoint,
        )
        tags = tuple(
            part.strip()
            for part in str(business.get("keytag") or business.get("tag") or "").replace("，", ",").split(",")
            if part.strip()
        )
        opening = business.get("opentime_today") or business.get("opentime_week")
        current_facts = ()
        if isinstance(opening, str) and opening.strip():
            current_facts = (CandidateCurrentFact(
                fact_type="OPENING_HOURS",
                value=opening.strip(),
                provider="amap_v5_place_around",
                observed_at=observed_at,
                request_hash=request_hash,
                response_hash=response_hash,
                execution_mode=RetrievalExecutionMode.LIVE,
                source_url=AmapCandidateSource.endpoint,
                confidence=0.7,
            ),)
        suitability = classify_provider_suitability(
            name=name,
            provider_raw_type=provider_raw_type,
            provider_raw_typecode=provider_raw_typecode,
        )
        return ProviderCandidate(
            canonical_place=place,
            provider_receipt=receipt,
            popularity=popularity,
            content_relevance=0.5,
            diversity_tags=tags,
            hard_block_codes=suitability.hard_block_codes,
            current_facts=current_facts,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _haversine_m(left: Coordinates, right: Coordinates) -> float:
    radius = 6_371_000.0
    lat1, lat2 = math.radians(left.lat), math.radians(right.lat)
    dlat = lat2 - lat1
    dlng = math.radians(right.lng - left.lng)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))
