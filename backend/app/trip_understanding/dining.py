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
from app.trip_understanding.models import StrictModel, DiningInsertCommand, PlaceConfirmCommand, LodgingRecoverCommand
from app.trip_understanding.pipeline import atomic_place_rejection_reason
from app.trip_understanding.stay import haversine_meters
from app.trip_understanding.city_scope import CityScope, CityScopeLookup
from app.trip_understanding.errors import PlaceProviderUnavailableError

DINING_RADIUS_METERS = 1200
_SCOPES = CityScopeLookup()


class DiningSearchRequest(StrictModel):
    activity_token: str = Field(min_length=20, max_length=80)


class DiningCandidateView(PublicPlaceCandidate):
    reason: str


class DiningCandidatesView(StrictModel):
    status: Literal["AVAILABLE", "EMPTY", "UNAVAILABLE", "NEEDS_CONFIRMATION"]
    message: str
    candidates: list[DiningCandidateView] = Field(default_factory=list, max_length=3)


def dining_binding(activity_token: str, *, before: bool = False) -> str:
    return f"dining-{'before' if before else 'after'}:{activity_token}"


def verify_command_candidate(command, *, public_resource_id: str, expected_etag: str, now):
    if isinstance(command, LodgingRecoverCommand):
        from app.trip_understanding.lodging_recovery import recovery_binding
        return verify_candidate(command.candidate_token, public_resource_id=public_resource_id,
            activity_token=recovery_binding(command.pending_token, command.intent), expected_etag=expected_etag, now=now)
    if not isinstance(command, (DiningInsertCommand, PlaceConfirmCommand)):
        return None
    binding = dining_binding(command.after_activity_token, before=command.insert_before) if isinstance(command, DiningInsertCommand) else command.activity_token
    return verify_candidate(command.candidate_token, public_resource_id=public_resource_id,
        activity_token=binding, expected_etag=expected_etag, now=now)


def valid_anchor(anchor: MapStop | None) -> bool:
    return bool(anchor and anchor.resolution_status == "AUTO_MATCHED" and anchor.canonical_place_id
                and anchor.city and re.fullmatch(r"[\u4e00-\u9fff]{2,20}", anchor.city)
                and anchor.city != "目的地待确认" and anchor.longitude is not None and anchor.latitude is not None)


def select_dining_rows(rows: list, *, anchor: MapStop, excluded_ids: set[str], scope: CityScope | None = None,
                       meal_only: bool = False) -> list[CandidatePlace]:
    if not valid_anchor(anchor):
        return []
    accepted: dict[str, tuple[float, CandidatePlace]] = {}
    bounds = scope.bounds if scope else _CITY_BOUNDS.get(anchor.city)
    if not bounds:
        return []
    west, east, south, north = bounds
    for row in rows:
        if not isinstance(row, dict):
            continue
        name, identifier = str(row.get("name") or "").strip(), str(row.get("id") or "").strip()
        canonical = f"amap:{identifier}"
        if not identifier or canonical in excluded_ids or identifier in excluded_ids:
            continue
        if atomic_place_rejection_reason(name) or not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff·（）()—_ -]{1,40}", name):
            continue
        if re.search(r"内部专用|内部食堂|不对外(?:开放|营业)|仅限(?:内部|员工|职工)", name):
            # Provider restaurant codes also include staff-only canteens.
            # Explicit access restrictions cannot be a visitor meal suggestion;
            # public restaurant brands containing 食堂 remain eligible.
            continue
        if not (scope.matches(row) if scope else _admin_matches(row, expected_city=anchor.city, expected_district=None)):
            continue
        signals = classify_amap_type_signals(str(row.get("typecode") or ""), str(row.get("type") or ""))
        if not signals.complete or signals.conflict or signals.category != PlaceCategory.FOOD:
            continue
        if meal_only and not re.fullmatch(r"050[123]\d{2}", str(row.get("typecode") or "")):
            # A generic food category also contains tea, drinks, food banks and
            # catering companies. Only a specific meal-serving POI qualifies.
            continue
        if meal_only and re.search(r"宴会厅|婚宴中心|婚宴会馆|婚礼宴会|团膳|中央厨房", name):
            # Group/event catering can have a normal restaurant type code.
            # Keep public hotel restaurants and ordinary restaurant names.
            continue
        coordinates = _coordinates(row.get("location"))
        if not coordinates or not (west <= coordinates[0] <= east and south <= coordinates[1] <= north):
            continue
        distance = haversine_meters(anchor.longitude, anchor.latitude, *coordinates)
        if not 0 <= distance <= DINING_RADIUS_METERS:
            continue
        address = row.get("address")
        business = row.get("business")
        area = row.get("business_area") or (business.get("business_area") if isinstance(business, dict) else None)
        area = area if isinstance(area, str) and re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9·、（）() -]{1,40}", area) else None
        place = CandidatePlace(canonical_place_id=canonical, city=anchor.city, name=name, category="餐饮", business_area=area,
            area_or_address=address[:120] if isinstance(address, str) and address else str(row.get("adname") or anchor.city)[:120],
            position=GCJ02Position(longitude=coordinates[0], latitude=coordinates[1]))
        accepted[canonical] = (distance, place)
    return [place for _, place in sorted(accepted.values(), key=lambda item: (item[0], item[1].name))[:3]]


async def search_dining(*, anchor: MapStop, excluded_ids: set[str], receipt: dict | None = None,
                        meal_only: bool = False) -> list[CandidatePlace] | None:
    receipt = receipt if receipt is not None else {}
    receipt.update(poi_http_attempts=0, district_http_attempts=0)
    settings = get_settings()
    if not valid_anchor(anchor) or not settings.amap_api_key or settings.trip_understanding_provider_mode != "live":
        return None
    # Existing AMap POI 2.0, one request. No route, rating, price or opening-hour inference.
    params = {"key": settings.amap_api_key, "location": f"{anchor.longitude:.6f},{anchor.latitude:.6f}",
        "radius": DINING_RADIUS_METERS, "types": "050100|050200|050300" if meal_only else "050000", "sortrule": "distance", "page_size": 25, "page_num": 1,
        "region": anchor.city, "city_limit": "true", "output": "json", "show_fields": "business"}
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            scope = None
            if anchor.city not in _CITY_BOUNDS:
                scope_receipt = {}
                try:
                    scope = await _SCOPES.get(anchor.city, client=client, api_key=settings.amap_api_key, timeout=4.0, receipt=scope_receipt)
                finally:
                    receipt["district_http_attempts"] = scope_receipt.get("external_calls", 0)
                if scope is None:
                    return None
            receipt["poi_http_attempts"] += 1
            response = await client.get("https://restapi.amap.com/v5/place/around", params=params)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "1" or not isinstance(payload.get("pois"), list):
            return None
    except (httpx.HTTPError, ValueError, PlaceProviderUnavailableError):
        return None
    return select_dining_rows(payload["pois"], anchor=anchor, excluded_ids=excluded_ids, scope=scope, meal_only=meal_only)
