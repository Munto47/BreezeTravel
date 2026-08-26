"""Structured recommendation-plan contracts for compound place discovery."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.place import Coordinates, PlaceCategory


class GeoConstraint(BaseModel):
    administrative_district: Optional[str] = None
    anchor_place: Optional[str] = None
    anchor_place_id: Optional[str] = None
    anchor_coords: Optional[Coordinates] = None
    anchor_response_hash: Optional[str] = None
    anchor_observed_at: Optional[datetime] = None
    max_radius_km: Optional[float] = Field(None, gt=0)
    max_travel_minutes: Optional[int] = Field(None, gt=0)
    max_transfers: Optional[int] = Field(None, ge=0)
    transport_mode: str = "walking"


class RecommendationSlot(BaseModel):
    slot_id: str
    category: PlaceCategory
    order: int = Field(ge=1)
    min_results: int = Field(default=1, ge=1, le=10)
    entity_name: Optional[str] = None
    entity_aliases: list[str] = Field(default_factory=list)
    provider_match_aliases: list[str] = Field(
        default_factory=list,
        description="Provider result names allowed for a bounded registry query; not a user-requested entity contract.",
    )
    query: str
    provider_typecodes: list[str] = Field(default_factory=list)
    geo: GeoConstraint = Field(default_factory=GeoConstraint)


class RecommendationPlan(BaseModel):
    version: str = "1.0"
    user_request: str
    city: str
    slots: list[RecommendationSlot]
