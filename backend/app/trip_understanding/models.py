from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActivityRole(str, Enum):
    PLANNED = "PLANNED"
    OPTIONAL = "OPTIONAL"
    REFERENCE = "REFERENCE"
    EXCLUDED = "EXCLUDED"
    PASS_THROUGH = "PASS_THROUGH"


class ResolutionStatus(str, Enum):
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    UNRESOLVED = "UNRESOLVED"
    AUTO_MATCHED = "AUTO_MATCHED"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"


class ProposedMention(StrictModel):
    mention_id: str
    raw_text: str = Field(min_length=1)
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    role: ActivityRole
    day_index: int | None = Field(default=None, ge=1, le=14)
    sequence_index: int = Field(ge=0)
    atomic_place_name: str | None = None
    category_hint: str | None = None
    time_hint: str | None = None

    @model_validator(mode="after")
    def valid_span(self) -> "ProposedMention":
        if self.span_end <= self.span_start:
            raise ValueError("mention span must be non-empty")
        return self


class InferenceProposal(StrictModel):
    schema_version: Literal["trip-understanding-proposal-v1"] = "trip-understanding-proposal-v1"
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_name: str
    mentions: list[ProposedMention]
    binding: dict[str, object]


class CompiledActivity(StrictModel):
    activity_id: str
    public_activity_token: str
    mention: ProposedMention
    eligible_for_place_search: bool


class ResolvedPlace(StrictModel):
    canonical_place_id: str
    name: str
    category: str
    area_or_address: str
    provider_binding: dict[str, object]


class ResolvedActivity(StrictModel):
    compiled: CompiledActivity
    resolution_status: ResolutionStatus
    place: ResolvedPlace | None = None

    @model_validator(mode="after")
    def resolution_is_consistent(self) -> "ResolvedActivity":
        if (self.resolution_status == ResolutionStatus.AUTO_MATCHED) != (self.place is not None):
            raise ValueError("AUTO_MATCHED activities require exactly one resolved place")
        return self


class SourceClaimRecord(StrictModel):
    claim_id: str
    activity_id: str
    claim_type: Literal[
        "PLACE_MENTION",
        "ROLE",
        "DAY",
        "TIME_HINT",
        "ASSUMPTION",
        "EXCLUSION",
    ]
    span_start: int
    span_end: int
    quote: str


class AssumptionChipView(StrictModel):
    key: Literal["destination", "calendar", "party_size"]
    label: str
    value: str
    editable: bool


class ActivityCardView(StrictModel):
    activity_token: str = Field(min_length=20, max_length=80)
    name: str
    category: str
    area_or_address: str
    time_hint: str | None = None
    status: Literal["READY", "NEEDS_CONFIRMATION"]
    available_actions: list[Literal["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"]]


class TripDayView(StrictModel):
    label: str
    activities: list[ActivityCardView]


class MapReadinessView(StrictModel):
    status: Literal["PREPARING", "AVAILABLE", "NEEDS_UPDATE", "LIMITED", "UNAVAILABLE"]
    message: str
    available_actions: list[Literal["VIEW_MAP", "RENDER_MAP"]] = Field(default_factory=list)


class StayCandidateView(StrictModel):
    name: str
    category: str
    area_or_address: str
    commute_summary: str
    evidence_gap: str | None = None
    reason: str
    available_actions: list[Literal["CHOOSE_STAY"]]


class StaySuggestionView(StrictModel):
    status: Literal["AVAILABLE", "LIMITED", "UNAVAILABLE"]
    message: str
    candidates: list[StayCandidateView] = Field(default_factory=list)
    available_actions: list[Literal["CHOOSE_STAY"]] = Field(default_factory=list)


class UserFacingTripResult(StrictModel):
    status: Literal["READY", "PARTIAL_RESULT", "BASIC_ONLY"]
    assumptions: list[AssumptionChipView]
    days: list[TripDayView]
    map: MapReadinessView
    stay: StaySuggestionView
    available_actions: list[Literal["EDIT_ASSUMPTIONS", "EDIT_CARDS"]]


class CreateDemoRequest(StrictModel):
    mode: Literal["DEMO"]


class TripUnderstandingAcceptedView(StrictModel):
    public_resource_id: str
    status: Literal["PROCESSING"] = "PROCESSING"
    message: str = "正在整理每天行程"
    result_url: str
    events_url: str


class TripUnderstandingProgressView(StrictModel):
    status: Literal["PROCESSING"] = "PROCESSING"
    message: str
    retry_after_ms: int = Field(default=500, ge=100, le=5000)


class PublicEventPayload(StrictModel):
    status: Literal["PROCESSING", "READY"]
    message: Literal["正在整理每天行程", "正在核对地点", "卡片已可用"]


class PublicEventRecord(StrictModel):
    event_id: int = Field(gt=0)
    event_type: Literal["progress", "result_available"]
    payload: PublicEventPayload


class PublicResourceRecord(StrictModel):
    understanding_id: str
    public_resource_id: str
    state: Literal["PROCESSING", "READY", "PARTIAL", "FAILED", "DELETED"]
    current_result_id: str | None = None


class StoredResult(StrictModel):
    result: UserFacingTripResult
    opaque_etag: str


class CreateOutcome(StrictModel):
    accepted: TripUnderstandingAcceptedView
    replayed: bool = False


class TripUnderstandingJobRecord(StrictModel):
    job_id: str
    understanding_id: str
    revision: int
    status: Literal["RUNNING"]
    lease_owner: str
    lease_until: datetime
    attempt: int = Field(gt=0)
    max_attempts: int = Field(gt=0)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PipelineOutput(StrictModel):
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination: dict[str, object]
    assumptions: list[dict[str, object]]
    proposal: InferenceProposal
    inference_binding: dict[str, object]
    compiler_receipt: dict[str, object]
    activities: list[ResolvedActivity]
    claims: list[SourceClaimRecord]
    public_result: UserFacingTripResult
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
