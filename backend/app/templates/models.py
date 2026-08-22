from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.itineraries.models import SUPPORTED_CITIES
from app.schemas.place import Coordinates, Place


class TemplateStatus(str, Enum):
    DRAFT = "DRAFT"
    REVIEWED = "REVIEWED"
    RETIRED = "RETIRED"


class TemplateProvenance(str, Enum):
    """Who supplied the template content, kept separate from its lifecycle."""

    MODEL_GENERATED = "MODEL_GENERATED"
    HUMAN_CURATED = "HUMAN_CURATED"


class CandidateTier(str, Enum):
    ON_THE_WAY = "ON_THE_WAY"
    ACCEPTABLE = "ACCEPTABLE"
    ANOTHER_DAY = "ANOTHER_DAY"
    NOT_FEASIBLE = "NOT_FEASIBLE"


class EvidenceFreshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICTING = "CONFLICTING"


class SourceReference(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    url: str | None = None
    observed_at: datetime | None = None
    provenance: TemplateProvenance = TemplateProvenance.HUMAN_CURATED
    note: str = ""


class RouteZone(BaseModel):
    model_config = ConfigDict(frozen=True)

    zone_id: str = Field(min_length=1, max_length=80)
    city: str
    district: str = ""
    center: Coordinates
    preferred_transport: str = "transit"
    nearby_zone_ids: list[str] = Field(default_factory=list)
    incompatible_same_day_zone_ids: list[str] = Field(default_factory=list)


class AnchorSlot(BaseModel):
    model_config = ConfigDict(frozen=True)

    slot_id: str = Field(min_length=1, max_length=100)
    day_offset: int = Field(ge=0, le=4)
    time_window: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
    zone_id: str = Field(min_length=1)
    slot_type: str = Field(min_length=1)
    category_constraints: list[str] = Field(default_factory=list)
    anchor_place_ids: list[str] = Field(default_factory=list)
    alternative_group_id: str | None = None
    optional: bool = False
    dwell_minutes: int = Field(default=90, ge=0, le=720)


class AlternativeGroup(BaseModel):
    model_config = ConfigDict(frozen=True)

    group_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1)
    zone_id: str = Field(min_length=1)
    min_choices: int = Field(default=0, ge=0)
    max_choices: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> "AlternativeGroup":
        if self.min_choices > self.max_choices:
            raise ValueError("alternative group min_choices must not exceed max_choices")
        return self


class CityRouteTemplate(BaseModel):
    model_config = ConfigDict(frozen=True)

    template_id: str = Field(min_length=1, max_length=100)
    city: str
    name: str = Field(min_length=1, max_length=160)
    template_version: int = Field(ge=1)
    suitable_days: list[int] = Field(min_length=1)
    suitable_groups: list[str] = Field(default_factory=list)
    budget_level: str = "medium"
    intensity: str = "medium"
    route_zones: list[RouteZone] = Field(min_length=1)
    anchor_slots: list[AnchorSlot] = Field(default_factory=list)
    # This is a deliberately separate projection from the revision stop.  A
    # stop only persists an id/name, while this carries the coordinate and its
    # provenance needed to make local template editing usable.  For model
    # drafts these are synthetic anchors, never provider or human evidence.
    anchor_places: list[Place] = Field(default_factory=list)
    alternative_groups: list[AlternativeGroup] = Field(default_factory=list)
    hotel_area_rules: list[dict[str, object]] = Field(default_factory=list)
    source_refs: list[SourceReference] = Field(default_factory=list)
    status: TemplateStatus = TemplateStatus.DRAFT
    provenance: TemplateProvenance = TemplateProvenance.HUMAN_CURATED
    last_verified_at: datetime | None = None

    @model_validator(mode="after")
    def validate_template(self) -> "CityRouteTemplate":
        if self.city not in SUPPORTED_CITIES:
            raise ValueError("CITY_NOT_SUPPORTED")
        if any(day < 2 or day > 5 for day in self.suitable_days):
            raise ValueError("suitable_days must be within 2 to 5")
        zones = {zone.zone_id for zone in self.route_zones}
        if len(zones) != len(self.route_zones):
            raise ValueError("route zone ids must be unique")
        if any(zone.city != self.city for zone in self.route_zones):
            raise ValueError("route zone city must match template city")
        if any(slot.zone_id not in zones for slot in self.anchor_slots):
            raise ValueError("anchor slot references missing route zone")
        anchor_place_ids = [place.place_id for place in self.anchor_places]
        if len(anchor_place_ids) != len(set(anchor_place_ids)):
            raise ValueError("anchor place ids must be unique")
        if any(place.city != self.city for place in self.anchor_places):
            raise ValueError("anchor place city must match template city")
        known_anchor_place_ids = set(anchor_place_ids)
        if any(place_id not in known_anchor_place_ids for slot in self.anchor_slots for place_id in slot.anchor_place_ids):
            raise ValueError("anchor slot references missing anchor place projection")
        groups = {group.group_id for group in self.alternative_groups}
        if any(slot.alternative_group_id and slot.alternative_group_id not in groups for slot in self.anchor_slots):
            raise ValueError("anchor slot references missing alternative group")
        # Model output can provide a draft but never satisfies the human-review
        # predicate merely by changing status text.
        if self.status is TemplateStatus.REVIEWED and self.provenance is TemplateProvenance.MODEL_GENERATED:
            raise ValueError("MODEL_GENERATED_TEMPLATE_REQUIRES_HUMAN_REVIEW")
        if self.status is TemplateStatus.REVIEWED and (not self.source_refs or self.last_verified_at is None):
            raise ValueError("REVIEWED_TEMPLATE_REQUIRES_SOURCES_AND_VERIFICATION_TIME")
        return self


class RouteEstimate(BaseModel):
    model_config = ConfigDict(frozen=True)

    minutes: int | None = Field(default=None, ge=0)
    source: str = "unavailable"
    freshness: EvidenceFreshness = EvidenceFreshness.UNAVAILABLE
    observed_at: datetime | None = None
    failure_reason: str | None = None


class CandidateGate(BaseModel):
    hard_constraint_passed: bool = True
    opening_time_fit: bool | None = None
    reservation_fit: bool | None = None
    member_suitability: bool | None = None
    reason_codes: list[str] = Field(default_factory=list)


class CandidateSuggestion(BaseModel):
    candidate: Place
    tier: CandidateTier
    insertion_route_minutes: int | None = None
    current_route_minutes: int | None = None
    delta_route_minutes: int | None = None
    route_evidence: list[RouteEstimate] = Field(default_factory=list)
    evidence_freshness: EvidenceFreshness = EvidenceFreshness.UNAVAILABLE
    hard_gate_passed: bool
    explanation_codes: list[str] = Field(default_factory=list)
    explanation: str


class HotelAreaScore(BaseModel):
    area_id: str
    score_minutes: int | None = None
    all_days_covered: bool
    evidence_freshness: EvidenceFreshness
    explanation_codes: list[str] = Field(default_factory=list)


class HotelSuggestion(BaseModel):
    hotel: Place
    area_score: HotelAreaScore
    hotel_evidence_freshness: EvidenceFreshness
    hard_gate_passed: bool
