"""User-selected POIs with authenticated, short-lived, resource-bound credentials."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, field_validator

from app.config import get_settings
from app.constraints.amap_types import classify_amap_type_signals
from app.schemas.place import PlaceCategory
from app.trip_understanding.amap_place import (
    AmapPlaceResolver, _admin_matches, _coordinates, _expected_category, _CATEGORY_LABELS, _name_match_tier, _CITY_BOUNDS,
    _visitor_type_compatible,
)
from app.trip_understanding.errors import CommandTargetChangedError, PlaceProviderUnavailableError
from app.trip_understanding.models import StrictModel, LodgingRecoveryIntent, DiningAccessView, safe_poi_photo_url
from app.trip_understanding.landmark_hints import landmark_hint, verified_technical_landmark
from app.trip_understanding.city_knowledge import get_city_knowledge
from app.trip_understanding.pipeline import atomic_place_rejection_reason


class CandidateSearchRequest(StrictModel):
    activity_token: str = Field(min_length=20, max_length=80)
    query: str = Field(min_length=1, max_length=40)
    city: str | None = Field(default=None, min_length=2, max_length=20, pattern=r"^[\u4e00-\u9fff]+$")


class PendingLodgingCandidateRequest(StrictModel):
    pending_token: str = Field(min_length=20, max_length=80)
    query: str = Field(min_length=1, max_length=40)
    city: str | None = Field(default=None, min_length=2, max_length=20, pattern=r"^[\u4e00-\u9fff]+$")
    intent: LodgingRecoveryIntent


class GCJ02Position(StrictModel):
    longitude: float = Field(ge=73, le=136)
    latitude: float = Field(ge=18, le=54)
    coordinate_system: Literal["GCJ02"] = "GCJ02"


class DiningPOIInfo(StrictModel):
    photo_url: str | None = None
    cuisine: str | None = Field(default=None, max_length=60)
    tags: list[str] = Field(default_factory=list, max_length=12)
    rating: float | None = Field(default=None, gt=0, le=5, allow_inf_nan=False)
    cost: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    source: Literal["AMAP_POI_V2"] = "AMAP_POI_V2"
    observed_at: datetime

    @field_validator("photo_url", mode="before")
    @classmethod
    def safe_photo(cls, value):
        return safe_poi_photo_url(value)


class CandidatePlace(StrictModel):
    photo_url: str | None = None
    canonical_place_id: str
    city: str
    name: str = Field(min_length=1, max_length=40)
    category: str
    area_or_address: str
    position: GCJ02Position
    business_area: str | None = None
    dining_info: DiningPOIInfo | None = None
    provider_parent_place_id: str | None = None
    dining_parent_activity_token: str | None = None
    dining_access: DiningAccessView | None = None
    meal_evidence_status: Literal["LIGHT_FOOD_ITEMS_ONLY", "UNSPECIFIED"] = "UNSPECIFIED"

    @field_validator("photo_url", mode="before")
    @classmethod
    def valid_photo(cls, value):
        return safe_poi_photo_url(value)

    def receipt(self) -> dict:
        return {"status": "USER_CONFIRMED", "provider": "AMAP_POI_V2",
                "city": self.city, "coordinates": self.position.model_dump(),
                "category": self.category, "area_or_address": self.area_or_address,
                **({"dining_parent_place_id": self.provider_parent_place_id} if self.provider_parent_place_id else {})}


class PublicPlaceCandidate(StrictModel):
    photo_url: str | None = None
    candidate_token: str
    name: str
    category: str
    area_or_address: str
    position: GCJ02Position
    business_area: str | None = None
    dining_info: DiningPOIInfo | None = None
    dining_access: DiningAccessView | None = None
    meal_evidence_status: Literal["LIGHT_FOOD_ITEMS_ONLY", "UNSPECIFIED"] = "UNSPECIFIED"


class CandidateSearchView(StrictModel):
    status: Literal["AVAILABLE", "EMPTY", "UNAVAILABLE"]
    candidates: list[PublicPlaceCandidate] = Field(default_factory=list)


def _cipher() -> Fernet:
    settings = get_settings()
    secret = settings.trip_understanding_cookie_signing_key or settings.jwt_secret_key
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("poi-selection:" + secret).encode()).digest()))


def issue_candidate(place: CandidatePlace, *, public_resource_id: str, activity_token: str,
                    expected_etag: str, now: datetime, expires_at: datetime | None = None) -> PublicPlaceCandidate:
    expiry = now + timedelta(minutes=10)
    if expires_at is not None:
        expiry = min(expiry, expires_at)
    body = {"resource": public_resource_id, "activity": activity_token, "etag": expected_etag,
            "expires": expiry.timestamp(), "place": place.model_dump(mode="json")}
    token = _cipher().encrypt(json.dumps(body, ensure_ascii=False).encode()).decode()
    return PublicPlaceCandidate(candidate_token=token, **place.model_dump(exclude={
        "canonical_place_id", "city", "provider_parent_place_id", "dining_parent_activity_token"}))


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
    from app.trip_understanding.ranked_places import ranked_candidates

    settings = get_settings()
    if not settings.amap_api_key or settings.trip_understanding_provider_mode != "live":
        return None
    provider = AmapPlaceResolver(api_key=settings.amap_api_key)
    try:
        candidates, _receipt = await ranked_candidates(provider, city=city, query=query, category_hint=category_hint)
        places = []
        for candidate in candidates:
            row = candidate.raw
            address = row.get("address")
            place = CandidatePlace(canonical_place_id=f"amap:{row['id']}", city=city,
                photo_url=next((url for photo in (row.get('photos') or []) if isinstance(photo, dict)
                    if (url := safe_poi_photo_url(photo.get('url')))), None),
                name=str(row['name']), category=_CATEGORY_LABELS[candidate.category],
                area_or_address=str(address)[:120] if isinstance(address, str) and address else str(row.get("adname") or city),
                position=GCJ02Position(longitude=candidate.coordinates[0], latitude=candidate.coordinates[1]))
            if candidate.category == PlaceCategory.FOOD:
                from app.trip_understanding.dining import dining_metadata
                place = dining_metadata(place, row)
            places.append(place)
        return places
    except PlaceProviderUnavailableError:
        return None
    finally:
        await provider.aclose()
