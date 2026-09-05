"""User-selected POIs with authenticated, short-lived, resource-bound credentials."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field

from app.config import get_settings
from app.constraints.amap_types import classify_amap_type_signals
from app.schemas.place import PlaceCategory
from app.trip_understanding.amap_place import (
    AmapPlaceResolver, _admin_matches, _coordinates, _expected_category, _CATEGORY_LABELS,
)
from app.trip_understanding.errors import CommandTargetChangedError, PlaceProviderUnavailableError
from app.trip_understanding.models import StrictModel

_CITY_BOUNDS = {"北京": (115.4, 117.6, 39.4, 41.1), "上海": (120.8, 122.3, 30.6, 31.9), "杭州": (118.3, 120.8, 29.1, 30.8)}


class CandidateSearchRequest(StrictModel):
    activity_token: str = Field(min_length=20, max_length=80)
    query: str = Field(min_length=1, max_length=40)


class GCJ02Position(StrictModel):
    longitude: float = Field(ge=73, le=136)
    latitude: float = Field(ge=18, le=54)
    coordinate_system: Literal["GCJ02"] = "GCJ02"


class CandidatePlace(StrictModel):
    canonical_place_id: str
    city: str
    name: str = Field(min_length=1, max_length=40)
    category: str
    area_or_address: str
    position: GCJ02Position

    def receipt(self) -> dict:
        return {"status": "USER_CONFIRMED", "provider": "AMAP_POI_V2",
                "city": self.city, "coordinates": self.position.model_dump(),
                "category": self.category, "area_or_address": self.area_or_address}


class PublicPlaceCandidate(StrictModel):
    candidate_token: str
    name: str
    category: str
    area_or_address: str
    position: GCJ02Position


class CandidateSearchView(StrictModel):
    status: Literal["AVAILABLE", "EMPTY", "UNAVAILABLE"]
    candidates: list[PublicPlaceCandidate] = Field(default_factory=list)


def _cipher() -> Fernet:
    settings = get_settings()
    secret = settings.trip_understanding_cookie_signing_key or settings.jwt_secret_key
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("poi-selection:" + secret).encode()).digest()))


def issue_candidate(place: CandidatePlace, *, public_resource_id: str, activity_token: str,
                    expected_etag: str, now: datetime) -> PublicPlaceCandidate:
    body = {"resource": public_resource_id, "activity": activity_token, "etag": expected_etag,
            "expires": (now + timedelta(minutes=10)).timestamp(), "place": place.model_dump()}
    token = _cipher().encrypt(json.dumps(body, ensure_ascii=False).encode()).decode()
    return PublicPlaceCandidate(candidate_token=token, **place.model_dump(exclude={"canonical_place_id", "city"}))


def verify_candidate(token: str, *, public_resource_id: str, activity_token: str,
                     expected_etag: str, now: datetime) -> CandidatePlace:
    try:
        body = json.loads(_cipher().decrypt(token.encode()))
        if (body["resource"] != public_resource_id or body["activity"] != activity_token
                or body["etag"] != expected_etag or body["expires"] <= now.timestamp()):
            raise ValueError("candidate binding changed")
        return CandidatePlace.model_validate(body["place"])
    except (InvalidToken, ValueError, KeyError, TypeError) as exc:
        raise CommandTargetChangedError("place selection expired or changed") from exc


async def search_candidates(*, city: str, query: str, category_hint: str | None) -> list[CandidatePlace] | None:
    settings = get_settings()
    if city not in {"北京", "上海", "杭州"} or not settings.amap_api_key or settings.trip_understanding_provider_mode != "live":
        return None
    if not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff·（）()—_ -]{1,40}", query.strip()):
        return []
    provider = AmapPlaceResolver(api_key=settings.amap_api_key)
    try:
        rows, _receipt = await provider._query_provider(city=city, query_name=query.strip(),
            original_atomic=query.strip(), category_basis="USER_SEARCH", typecodes=[], lexicon_binding={})
    except PlaceProviderUnavailableError:
        return None
    finally:
        await provider.aclose()
    expected = _expected_category(category_hint)
    places: dict[str, CandidatePlace] = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        poi_id = str(row.get("id") or "").strip()
        if not poi_id or not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff·（）()—_ -]{1,40}", name):
            continue
        if not _admin_matches(row, expected_city=city, expected_district=None):
            continue
        signals = classify_amap_type_signals(str(row.get("typecode") or ""), str(row.get("type") or ""))
        if not signals.complete or signals.conflict or signals.category == PlaceCategory.UNKNOWN:
            continue
        if expected is not None and signals.category != expected:
            continue
        coordinates = _coordinates(row.get("location"))
        if not coordinates or not (73 <= coordinates[0] <= 136 and 18 <= coordinates[1] <= 54):
            continue
        west, east, south, north = _CITY_BOUNDS[city]
        if not (west <= coordinates[0] <= east and south <= coordinates[1] <= north):
            continue
        address = row.get("address")
        places[poi_id] = CandidatePlace(canonical_place_id=f"amap:{poi_id}", city=city,
            name=name, category=_CATEGORY_LABELS[signals.category],
            area_or_address=str(address)[:120] if isinstance(address, str) and address else str(row.get("adname") or city),
            position=GCJ02Position(longitude=coordinates[0], latitude=coordinates[1]))
    return list(places.values())[:6]
