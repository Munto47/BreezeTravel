"""Live route evidence for nearby, commute-time and transfer constraints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import weakref
from datetime import datetime, timezone
from typing import Any, Literal

import aiohttp
from pydantic import BaseModel

from app.config import settings
from app.constraints.evidence_resolver import finalize_place_evidence
from app.schemas.place import Coordinates, EvidenceStatus, GeoEvidence, Place
from app.schemas.recommendation_plan import RecommendationPlan


class RouteResult(BaseModel):
    status: Literal["ok", "unknown"]
    duration_minutes: int | None = None
    distance_km: float | None = None
    transfer_count: int | None = None
    source: str
    response_hash: str | None = None
    observed_at: datetime | None = None
    failure_reason: str | None = None


_ROUTE_ENDPOINTS = {
    "walking": "https://restapi.amap.com/v3/direction/walking",
    "driving": "https://restapi.amap.com/v3/direction/driving",
    "transit": "https://restapi.amap.com/v3/direction/transit/integrated",
}
_ROUTE_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_ROUTE_SEMAPHORES_LOCK = threading.Lock()


def _route_semaphore() -> asyncio.Semaphore:
    """Return a limiter owned by the current event loop.

    Snapshot replay uses a thread pool and one ``asyncio.run`` loop per worker.
    An asyncio primitive cannot be shared across those loops once it blocks.
    """
    loop = asyncio.get_running_loop()
    with _ROUTE_SEMAPHORES_LOCK:
        semaphore = _ROUTE_SEMAPHORES.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(max(1, settings.amap_max_concurrency))
            _ROUTE_SEMAPHORES[loop] = semaphore
        return semaphore


def _hash(payload: Any) -> str:
    value = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_route(data: dict[str, Any], mode: str) -> tuple[int, float, int | None] | None:
    route = data.get("route") or {}
    if mode == "transit":
        options = route.get("transits") or []
        if not options:
            return None
        first = options[0]
        duration = int(float(first.get("duration") or 0))
        distance = float(first.get("distance") or route.get("distance") or 0)
        ride_legs = 0
        for segment in first.get("segments") or []:
            buslines = ((segment.get("bus") or {}).get("buslines") or [])
            subway = ((segment.get("railway") or {}).get("name"))
            if buslines or subway:
                ride_legs += 1
        return max(1, round(duration / 60)), round(distance / 1000, 3), max(0, ride_legs - 1)
    paths = route.get("paths") or []
    if not paths:
        return None
    first = paths[0]
    duration = int(float(first.get("duration") or 0))
    distance = float(first.get("distance") or 0)
    return max(1, round(duration / 60)), round(distance / 1000, 3), None


async def fetch_amap_route(
    session: aiohttp.ClientSession,
    origin: Coordinates,
    destination: Coordinates,
    mode: str,
    city: str,
) -> RouteResult:
    if settings.amap_mock or settings.demo_mode or not settings.amap_api_key:
        return RouteResult(status="unknown", source="route_unavailable", failure_reason="live_route_disabled")
    endpoint = _ROUTE_ENDPOINTS.get(mode)
    if not endpoint:
        return RouteResult(status="unknown", source="route_unavailable", failure_reason="unsupported_mode")
    params = {
        "key": settings.amap_api_key,
        "origin": f"{origin.lng},{origin.lat}",
        "destination": f"{destination.lng},{destination.lat}",
        "output": "json",
    }
    if mode == "transit":
        params["city"] = city
        params["cityd"] = city
        params["strategy"] = "0"
    try:
        async with session.get(endpoint, params=params, timeout=aiohttp.ClientTimeout(total=6)) as response:
            response.raise_for_status()
            data = await response.json()
        observed_at = datetime.now(timezone.utc)
        response_hash = _hash(data)
        if data.get("status") != "1":
            return RouteResult(
                status="unknown", source="amap_route", response_hash=response_hash,
                observed_at=observed_at,
                failure_reason=str(data.get("info") or "provider_status_not_ok"),
            )
        parsed = _parse_route(data, mode)
        if parsed is None:
            return RouteResult(
                status="unknown", source="amap_route", response_hash=response_hash,
                observed_at=observed_at, failure_reason="no_route",
            )
        duration, distance, transfers = parsed
        return RouteResult(
            status="ok", duration_minutes=duration, distance_km=distance,
            transfer_count=transfers, source=f"amap_{mode}_route",
            response_hash=response_hash, observed_at=observed_at,
        )
    except Exception as exc:
        return RouteResult(
            status="unknown", source="amap_route",
            observed_at=datetime.now(timezone.utc), failure_reason=type(exc).__name__,
        )


def _anchor_coordinates(
    slot_id: str,
    anchor_name: str,
    places: list[Place],
    retrieval_audits: list[dict],
    bound_coords: Coordinates | None = None,
) -> Coordinates | None:
    if bound_coords is not None:
        return bound_coords
    compact = "".join(anchor_name.lower().split())
    for place in places:
        names = [place.name, *place.canonical_entity_names]
        if any(
            compact in "".join(name.lower().split())
            or "".join(name.lower().split()) in compact
            for name in names
        ):
            return place.coords
    for audit in reversed(retrieval_audits):
        if audit.get("slot_id") != slot_id or not audit.get("location"):
            continue
        try:
            lng, lat = str(audit["location"]).split(",", 1)
            return Coordinates(lng=float(lng), lat=float(lat))
        except (TypeError, ValueError):
            continue
    return None


async def enrich_geo_route_evidence(
    places: list[Place],
    plan: RecommendationPlan | dict | None,
    retrieval_audits: list[dict] | None = None,
) -> list[Place]:
    """Upgrade UNKNOWN commute evidence with live Amap route results."""
    if not plan or not places:
        return list(places)
    parsed_plan = plan if isinstance(plan, RecommendationPlan) else RecommendationPlan.model_validate(plan)
    slots = {slot.slot_id: slot for slot in parsed_plan.slots}
    audits = list(retrieval_audits or [])
    semaphore = _route_semaphore()

    async with aiohttp.ClientSession() as session:
        async def enrich(place: Place) -> Place:
            updated_geo: list[GeoEvidence] = []
            for evidence in place.geo_evidence:
                slot = slots.get(evidence.slot_id)
                if (
                    slot is None
                    or evidence.status != EvidenceStatus.UNKNOWN
                    or not slot.geo.anchor_place
                    or (slot.geo.max_travel_minutes is None and slot.geo.max_transfers is None)
                    or evidence.constraint_kind not in {"route", "proximity"}
                ):
                    updated_geo.append(evidence)
                    continue
                origin = _anchor_coordinates(
                    slot.slot_id, slot.geo.anchor_place, places, audits, slot.geo.anchor_coords,
                )
                if origin is None:
                    updated_geo.append(evidence.model_copy(update={
                        "source": "anchor_coordinates_unavailable",
                        "failure_reason": "anchor_coordinates_unavailable",
                    }))
                    continue
                async with semaphore:
                    route = await fetch_amap_route(
                        session, origin, place.coords, slot.geo.transport_mode, parsed_plan.city,
                    )
                if route.status != "ok":
                    updated_geo.append(evidence.model_copy(update={
                        "source": route.source,
                        "route_response_hash": route.response_hash,
                        "observed_at": route.observed_at,
                        "failure_reason": route.failure_reason,
                    }))
                    continue
                within_time = (
                    slot.geo.max_travel_minutes is None
                    or int(route.duration_minutes or 0) <= slot.geo.max_travel_minutes
                )
                within_transfers = (
                    slot.geo.max_transfers is None
                    or (
                        route.transfer_count is not None
                        and route.transfer_count <= slot.geo.max_transfers
                    )
                )
                updated_geo.append(evidence.model_copy(update={
                    "status": EvidenceStatus.VERIFIED,
                    "satisfies_constraint": within_time and within_transfers,
                    "estimated_travel_minutes": route.duration_minutes,
                    "transfer_count": route.transfer_count,
                    "transport_mode": slot.geo.transport_mode,
                    "source": route.source,
                    "route_response_hash": route.response_hash,
                    "observed_at": route.observed_at,
                    "failure_reason": None,
                }))
            return finalize_place_evidence(place.model_copy(update={"geo_evidence": updated_geo}))

        return await asyncio.gather(*(enrich(place) for place in places))
