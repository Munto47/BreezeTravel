"""Bounded nearby dining lookup. Coordinates and candidates stay server-authoritative."""
from __future__ import annotations

import re
from typing import Literal

import httpx
from pydantic import Field

from app.config import get_settings
from app.constraints.amap_types import classify_amap_type_signals
from app.schemas.place import PlaceCategory
from app.trip_understanding.amap_place import _admin_matches, _coordinates
from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, PublicPlaceCandidate, _CITY_BOUNDS, verify_candidate
from app.trip_understanding.map_render import MapStop
from app.trip_understanding.models import StrictModel, DiningInsertCommand, PlaceConfirmCommand
from app.trip_understanding.pipeline import atomic_place_rejection_reason
from app.trip_understanding.stay import haversine_meters

DINING_RADIUS_METERS = 1200


class DiningSearchRequest(StrictModel):
    activity_token: str = Field(min_length=20, max_length=80)


class DiningCandidateView(PublicPlaceCandidate):
    reason: str


class DiningCandidatesView(StrictModel):
    status: Literal["AVAILABLE", "EMPTY", "UNAVAILABLE", "NEEDS_CONFIRMATION"]
    message: str
    candidates: list[DiningCandidateView] = Field(default_factory=list, max_length=3)


def dining_binding(activity_token: str) -> str:
    return f"dining-after:{activity_token}"


def verify_command_candidate(command, *, public_resource_id: str, expected_etag: str, now):
    if not isinstance(command, (DiningInsertCommand, PlaceConfirmCommand)):
        return None
    binding = dining_binding(command.after_activity_token) if isinstance(command, DiningInsertCommand) else command.activity_token
    return verify_candidate(command.candidate_token, public_resource_id=public_resource_id,
        activity_token=binding, expected_etag=expected_etag, now=now)


def valid_anchor(anchor: MapStop | None) -> bool:
    return bool(anchor and anchor.resolution_status == "AUTO_MATCHED" and anchor.canonical_place_id
                and anchor.city in _CITY_BOUNDS and anchor.longitude is not None and anchor.latitude is not None)


def select_dining_rows(rows: list, *, anchor: MapStop, excluded_ids: set[str]) -> list[CandidatePlace]:
    if not valid_anchor(anchor):
        return []
    accepted: dict[str, tuple[float, CandidatePlace]] = {}
    west, east, south, north = _CITY_BOUNDS[anchor.city]
    for row in rows:
        if not isinstance(row, dict):
            continue
        name, identifier = str(row.get("name") or "").strip(), str(row.get("id") or "").strip()
        canonical = f"amap:{identifier}"
        if not identifier or canonical in excluded_ids or identifier in excluded_ids:
            continue
        if atomic_place_rejection_reason(name) or not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff·（）()—_ -]{1,40}", name):
            continue
        if not _admin_matches(row, expected_city=anchor.city, expected_district=None):
            continue
        signals = classify_amap_type_signals(str(row.get("typecode") or ""), str(row.get("type") or ""))
        if not signals.complete or signals.conflict or signals.category != PlaceCategory.FOOD:
            continue
        coordinates = _coordinates(row.get("location"))
        if not coordinates or not (west <= coordinates[0] <= east and south <= coordinates[1] <= north):
            continue
        distance = haversine_meters(anchor.longitude, anchor.latitude, *coordinates)
        if not 0 <= distance <= DINING_RADIUS_METERS:
            continue
        address = row.get("address")
        place = CandidatePlace(canonical_place_id=canonical, city=anchor.city, name=name, category="餐饮",
            area_or_address=address[:120] if isinstance(address, str) and address else str(row.get("adname") or anchor.city)[:120],
            position=GCJ02Position(longitude=coordinates[0], latitude=coordinates[1]))
        accepted[canonical] = (distance, place)
    return [place for _, place in sorted(accepted.values(), key=lambda item: (item[0], item[1].name))[:3]]


async def search_dining(*, anchor: MapStop, excluded_ids: set[str]) -> list[CandidatePlace] | None:
    settings = get_settings()
    if not valid_anchor(anchor) or not settings.amap_api_key or settings.trip_understanding_provider_mode != "live":
        return None
    # Existing AMap POI 2.0, one request. No route, rating, price or opening-hour inference.
    params = {"key": settings.amap_api_key, "location": f"{anchor.longitude:.6f},{anchor.latitude:.6f}",
        "radius": DINING_RADIUS_METERS, "types": "050000", "sortrule": "distance", "page_size": 25, "page_num": 1,
        "region": anchor.city, "city_limit": "true", "output": "json"}
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.get("https://restapi.amap.com/v5/place/around", params=params)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "1" or not isinstance(payload.get("pois"), list):
            return None
    except (httpx.HTTPError, ValueError):
        return None
    return select_dining_rows(payload["pois"], anchor=anchor, excluded_ids=excluded_ids)
