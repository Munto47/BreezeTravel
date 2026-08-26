from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.itineraries.models import CommitmentKind, ItineraryRevision, ResolutionStatus
from app.schemas.place import Coordinates, RetrievalExecutionMode


class ImportSourceType(str, Enum):
    AI_TEXT = "AI_TEXT"
    MANUAL_TEXT = "MANUAL_TEXT"
    SCREENSHOT_OCR = "SCREENSHOT_OCR"


class ImportStatus(str, Enum):
    PARSED = "PARSED"
    NEEDS_RESOLUTION = "NEEDS_RESOLUTION"
    READY = "READY"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


class SourceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> "SourceSpan":
        if self.end <= self.start:
            raise ValueError("source span end must be after start")
        return self


class RawStop(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw_stop_id: str
    import_id: str
    day_index: int | None = Field(default=None, ge=0)
    raw_name: str = Field(min_length=1, max_length=160)
    raw_time: str | None = Field(default=None, max_length=80)
    source_span: SourceSpan
    source_sentence: str = Field(min_length=1, max_length=1000)
    commitment_kind: CommitmentKind | None = None
    fixed_commitment: bool = False


class ResolvedPlaceReceipt(BaseModel):
    """Immutable provider receipt used to materialize one resolved POI.

    A display name is not enough to put a stop on the map or audit it later.
    This receipt binds the canonical/provider identity, coordinates and the
    exact retrieval observation that justified the resolution.
    """

    model_config = ConfigDict(frozen=True)

    canonical_place_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_place_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    city: str = Field(min_length=1)
    district: str | None = None
    address: str | None = None
    category: str | None = None
    provider_raw_type: str | None = Field(default=None, max_length=500)
    provider_raw_typecode: str | None = Field(default=None, max_length=80)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    execution_mode: RetrievalExecutionMode
    source_url: str | None = None

    @model_validator(mode="after")
    def validate_observed_at(self) -> "ResolvedPlaceReceipt":
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("resolved place receipt observed_at must be timezone-aware")
        return self


class PlaceCandidate(BaseModel):
    place_id: str
    name: str
    city: str
    district: str | None = None
    address: str | None = None
    category: str | None = None
    coords: Coordinates | None = None
    retrieval_provider: str | None = None
    execution_mode: str | None = None
    retrieval_request_hash: str | None = None
    retrieval_response_hash: str | None = None
    retrieval_observed_at: datetime | None = None
    source_url: str | None = None
    opening_hours: str | None = None
    phone: str | None = None
    amap_rating: float | None = Field(default=None, ge=0, le=5)
    amap_price: float | None = Field(default=None, ge=0)
    resolved_place_receipt: ResolvedPlaceReceipt | None = None
    score: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)


class ResolutionRejectionReason(str, Enum):
    WRONG_CITY = "WRONG_CITY"


class RejectedPlaceCandidate(BaseModel):
    """Provider-backed candidate rejected by a hard resolution boundary.

    Rejected candidates are intentionally kept outside ``candidates`` so a
    client cannot confirm them.  The receipt is retained for audit/readback;
    it proves what the provider returned without promoting that result to an
    applicable itinerary fact.
    """

    model_config = ConfigDict(frozen=True)

    place_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    reason: ResolutionRejectionReason
    target_city: str = Field(min_length=1)
    resolved_place_receipt: ResolvedPlaceReceipt

    @model_validator(mode="after")
    def validate_receipt_identity(self) -> "RejectedPlaceCandidate":
        if self.resolved_place_receipt.canonical_place_id != self.place_id:
            raise ValueError("rejected candidate receipt must match place_id")
        if self.resolved_place_receipt.name != self.name:
            raise ValueError("rejected candidate receipt must match name")
        return self


class ResolvedStop(BaseModel):
    raw_stop_id: str
    canonical_place_id: str | None = None
    candidates: list[PlaceCandidate] = Field(default_factory=list)
    rejected_candidates: list[RejectedPlaceCandidate] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    resolution_status: ResolutionStatus
    resolution_version: int = Field(default=1, gt=0)
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> "ResolvedStop":
        offered_ids = {candidate.place_id for candidate in self.candidates}
        rejected_ids = {candidate.place_id for candidate in self.rejected_candidates}
        if offered_ids & rejected_ids:
            raise ValueError("rejected candidates cannot also be offered for confirmation")
        if self.canonical_place_id in rejected_ids:
            raise ValueError("rejected candidates cannot become the canonical resolution")
        if self.resolution_status in {ResolutionStatus.AUTO_MATCHED, ResolutionStatus.USER_CONFIRMED}:
            if not self.canonical_place_id:
                raise ValueError("resolved stops require canonical_place_id")
        if self.resolution_status == ResolutionStatus.USER_CONFIRMED and not self.confirmed_by:
            raise ValueError("user-confirmed stops require confirmed_by")
        return self


def selected_place_candidate(resolution: ResolvedStop) -> PlaceCandidate | None:
    if not resolution.canonical_place_id:
        return None
    return next(
        (candidate for candidate in resolution.candidates if candidate.place_id == resolution.canonical_place_id),
        None,
    )


def resolution_is_materializable(resolution: ResolvedStop) -> bool:
    if resolution.resolution_status not in {
        ResolutionStatus.AUTO_MATCHED,
        ResolutionStatus.USER_CONFIRMED,
    }:
        return False
    candidate = selected_place_candidate(resolution)
    receipt = candidate.resolved_place_receipt if candidate is not None else None
    return bool(
        receipt is not None
        and receipt.canonical_place_id == resolution.canonical_place_id
        and re.fullmatch(r"[0-9a-f]{64}", receipt.request_hash)
        and re.fullmatch(r"[0-9a-f]{64}", receipt.response_hash)
        and receipt.observed_at.tzinfo is not None
        and receipt.observed_at.utcoffset() is not None
    )


def resolution_set_is_ready(raw_stops: list[RawStop], resolutions: list[ResolvedStop]) -> bool:
    return (
        bool(raw_stops)
        and len(resolutions) == len(raw_stops)
        and {item.raw_stop_id for item in resolutions} == {item.raw_stop_id for item in raw_stops}
        and all(resolution_is_materializable(item) for item in resolutions)
    )


class ItineraryImport(BaseModel):
    import_id: str
    workspace_id: str
    source_type: ImportSourceType
    raw_text: str = Field(min_length=1, max_length=12000)
    parse_version: str
    status: ImportStatus
    raw_stops: list[RawStop] = Field(default_factory=list)
    resolutions: list[ResolvedStop] = Field(default_factory=list)
    member_summary: list[str] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)
    state_version: int = Field(default=1, gt=0)
    applied_revision: int | None = Field(default=None, gt=0)
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ImportApplyResult(BaseModel):
    itinerary_import: ItineraryImport
    revision: ItineraryRevision
    resolved_place_receipts: list[ResolvedPlaceReceipt] = Field(default_factory=list)
    idempotent_replay: bool = False


class ImportParseDraft(BaseModel):
    raw_stops: list[RawStop]
    member_summary: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
