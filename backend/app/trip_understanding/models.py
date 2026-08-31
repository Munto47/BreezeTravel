from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActivityRole(str, Enum):
    PLANNED = "PLANNED"
    OPTIONAL = "OPTIONAL"
    REFERENCE = "REFERENCE"
    EXCLUDED = "EXCLUDED"
    PASS_THROUGH = "PASS_THROUGH"


class DestinationBasis(str, Enum):
    EXPLICIT = "EXPLICIT"
    SOFT_ASSUMPTION = "SOFT_ASSUMPTION"


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
    destination_basis: DestinationBasis = DestinationBasis.EXPLICIT
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


class PlaceResolutionOutcome(StrictModel):
    place: ResolvedPlace | None = None
    receipt: dict[str, object]


class ResolvedActivity(StrictModel):
    compiled: CompiledActivity
    resolution_status: ResolutionStatus
    place: ResolvedPlace | None = None
    resolver_receipt: dict[str, object] = Field(default_factory=dict)

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
    candidate_token: str = Field(min_length=20, max_length=100)
    name: str
    brand: str
    category: str
    area_or_address: str
    commute_summary: str
    max_single_leg_minutes: int = Field(ge=0)
    transfer_count: int = Field(ge=0)
    evidence_gap: str | None = None
    reason: str
    available_actions: list[Literal["CHOOSE_STAY"]]
    selected: bool = False


class StaySuggestionView(StrictModel):
    status: Literal["PREPARING", "AVAILABLE", "NEEDS_UPDATE", "LIMITED", "UNAVAILABLE"]
    message: str
    area_summary: str | None = None
    searched_scopes: list[str] = Field(default_factory=list)
    candidates: list[StayCandidateView] = Field(default_factory=list)
    available_actions: list[Literal["CHOOSE_STAY"]] = Field(default_factory=list)


class StaySelectionRequest(StrictModel):
    candidate_token: str = Field(min_length=20, max_length=100)


class StaySelectionAppliedView(StrictModel):
    status: Literal["APPLIED"] = "APPLIED"
    selected_stay: str
    overnight_days: list[str]
    map_readiness: Literal["NEEDS_UPDATE"] = "NEEDS_UPDATE"


class StaySelectionOutcome(StrictModel):
    applied: StaySelectionAppliedView
    opaque_etag: str
    replayed: bool = False


class UserFacingTripResult(StrictModel):
    status: Literal["READY", "PARTIAL_RESULT", "BASIC_ONLY", "LIMITED"]
    assumptions: list[AssumptionChipView]
    days: list[TripDayView]
    map: MapReadinessView
    stay: StaySuggestionView
    available_actions: list[Literal["EDIT_ASSUMPTIONS", "EDIT_CARDS"]]


class MaterializedTripView(StrictModel):
    status: Literal["READY"] = "READY"
    message: str
    calendar: str
    party_size: int = Field(ge=1, le=50)
    checks_available: bool = True


class PublicTripCheckItem(StrictModel):
    check_token: str = Field(min_length=20, max_length=100)
    label: Literal["必须调整", "可以更好", "需要确认"]
    title: str
    message: str
    affected_days: list[str] = Field(default_factory=list)
    can_preview: bool = False


class PublicTripChecksView(StrictModel):
    status: Literal["READY", "STILL_NEEDS_CONFIRMATION"]
    message: str
    items: list[PublicTripCheckItem] = Field(max_length=3)
    remaining_must_adjust: int = Field(ge=0)
    available_actions: list[Literal["PREVIEW_CHANGE"]] = Field(default_factory=list)


class ChangePreviewRequest(StrictModel):
    check_token: str = Field(min_length=20, max_length=100)


class PublicChangePreview(StrictModel):
    change_token: str = Field(min_length=20, max_length=100)
    title: str
    summary: str
    affected_days: list[str] = Field(default_factory=list)
    before: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)
    available_actions: list[Literal["ADOPT_CHANGE"]] = Field(
        default_factory=lambda: ["ADOPT_CHANGE"]
    )


class ChangeAdoptRequest(StrictModel):
    change_token: str = Field(min_length=20, max_length=100)


class PublicChangeAdopted(StrictModel):
    status: Literal["APPLIED", "STILL_NEEDS_CONFIRMATION"]
    message: str
    changed_days: list[str] = Field(default_factory=list)
    map_readiness: Literal["NEEDS_UPDATE"] = "NEEDS_UPDATE"
    checks: PublicTripChecksView


class MaterializationOutcome(StrictModel):
    view: MaterializedTripView
    opaque_etag: str
    replayed: bool = False


class ChangePreviewOutcome(StrictModel):
    preview: PublicChangePreview
    replayed: bool = False


class ChangeAdoptOutcome(StrictModel):
    adopted: PublicChangeAdopted
    opaque_etag: str
    replayed: bool = False


class CreateDemoRequest(StrictModel):
    mode: Literal["DEMO"]


class TextSourceRequest(StrictModel):
    type: Literal["TEXT"]
    text: str = Field(min_length=1, max_length=50_000)

    @model_validator(mode="after")
    def source_is_not_blank(self) -> "TextSourceRequest":
        if not self.text.strip():
            raise ValueError("text source must contain visible content")
        return self


class ScreenshotBatchSourceRequest(StrictModel):
    type: Literal["SCREENSHOT_BATCH"]
    batch_ref: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


FullSourceRequest = Annotated[
    TextSourceRequest | ScreenshotBatchSourceRequest,
    Field(discriminator="type"),
]


class CreateFullRequest(StrictModel):
    mode: Literal["FULL"]
    source: FullSourceRequest


CreateTripUnderstandingRequest = Annotated[
    CreateDemoRequest | CreateFullRequest,
    Field(discriminator="mode"),
]


class TripUnderstandingAcceptedView(StrictModel):
    public_resource_id: str
    status: Literal["PROCESSING"] = "PROCESSING"
    message: str = "正在整理每天行程"
    result_url: str
    events_url: str


class ScreenshotBatchAcceptedView(StrictModel):
    batch_ref: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    expires_at: datetime
    outcome: Literal["COMPLETE", "PARTIAL"]
    message: str


class ScreenshotBatchCreateOutcome(StrictModel):
    accepted: ScreenshotBatchAcceptedView
    replayed: bool = False


class ScreenshotBatchAssetInput(StrictModel):
    upload_position: int = Field(ge=0, le=5)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    byte_size: int = Field(gt=0, le=10 * 1024 * 1024)
    storage_locator: str = Field(min_length=32, max_length=256)
    ocr_status: Literal["PENDING", "SUCCEEDED", "FAILED", "TIMED_OUT", "NO_TEXT"]


class ScreenshotCleanupReceiptInput(StrictModel):
    upload_position: int | None = Field(default=None, ge=0, le=5)
    attempt_number: int = Field(ge=1, le=3)
    terminal_reason: str = Field(min_length=1, max_length=80)
    cleanup_status: Literal["DELETED", "ALREADY_ABSENT", "DELETE_FAILED"]
    attempted_at: datetime
    error_category: str | None = Field(default=None, max_length=120)


class ScreenshotBatchPersistenceInput(StrictModel):
    batch_ref: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    owner_user_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_document_json: str = Field(min_length=1)
    source_document_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_text_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["COMPLETE", "PARTIAL"]
    expires_at: datetime
    assets: tuple[ScreenshotBatchAssetInput, ...] = Field(min_length=1, max_length=6)
    cleanup_receipts: tuple[ScreenshotCleanupReceiptInput, ...] = Field(min_length=1)


class ScreenshotBatchClaimInput(StrictModel):
    batch_ref: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    owner_user_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime
    assets: tuple[ScreenshotBatchAssetInput, ...] = Field(min_length=1, max_length=6)


class ScreenshotCleanupPersistenceInput(StrictModel):
    owner_user_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    assets: tuple[ScreenshotBatchAssetInput, ...] = Field(default=(), max_length=6)
    cleanup_receipts: tuple[ScreenshotCleanupReceiptInput, ...] = Field(min_length=1)
    privacy_blocked: bool = False


class ScreenshotBatchFailurePersistenceInput(StrictModel):
    batch_ref: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    owner_user_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["FAILED", "CANCELLED", "TIMED_OUT", "PRIVACY_BLOCKED"]
    expires_at: datetime
    last_error_category: str = Field(min_length=1, max_length=120)
    assets: tuple[ScreenshotBatchAssetInput, ...] = Field(default=(), max_length=6)
    cleanup_receipts: tuple[ScreenshotCleanupReceiptInput, ...] = Field(default=())


class ConfirmationSourceSpan(StrictModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def span_is_non_empty(self) -> "ConfirmationSourceSpan":
        if self.end <= self.start:
            raise ValueError("confirmation source span must be non-empty")
        return self


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


class ActivityInsertCommand(StrictModel):
    command_type: Literal["ACTIVITY_INSERT"]
    day_index: int = Field(ge=1, le=14)
    position: int = Field(ge=0, le=80)
    name: str = Field(min_length=1, max_length=40)
    category: str = Field(default="地点", min_length=1, max_length=40)
    area_or_address: str = Field(default="地点待确认", min_length=1, max_length=120)
    time_hint: str | None = Field(default=None, max_length=80)


class ActivityDeleteCommand(StrictModel):
    command_type: Literal["ACTIVITY_DELETE"]
    activity_token: str = Field(min_length=20, max_length=80)


class ActivityMoveCommand(StrictModel):
    command_type: Literal["ACTIVITY_MOVE"]
    activity_token: str = Field(min_length=20, max_length=80)
    target_day_index: int = Field(ge=1, le=14)
    target_position: int = Field(ge=0, le=80)


class ActivityTextEditCommand(StrictModel):
    command_type: Literal["ACTIVITY_TEXT_EDIT"]
    activity_token: str = Field(min_length=20, max_length=80)
    name: str | None = Field(default=None, min_length=1, max_length=40)
    time_hint: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def has_edit(self) -> "ActivityTextEditCommand":
        if self.name is None and self.time_hint is None:
            raise ValueError("activity text edit requires name or time_hint")
        return self


class PlaceReplacementInput(StrictModel):
    name: str = Field(min_length=1, max_length=40)
    category: str = Field(min_length=1, max_length=40)
    area_or_address: str = Field(min_length=1, max_length=120)


class PlaceReplaceCommand(StrictModel):
    command_type: Literal["PLACE_REPLACE"]
    activity_token: str = Field(min_length=20, max_length=80)
    replacement: PlaceReplacementInput


class AssumptionSetCommand(StrictModel):
    command_type: Literal["ASSUMPTION_SET"]
    key: Literal["destination", "calendar", "party_size"]
    value: str = Field(min_length=1, max_length=100)


TripUnderstandingCommand = Annotated[
    ActivityInsertCommand
    | ActivityDeleteCommand
    | ActivityMoveCommand
    | ActivityTextEditCommand
    | PlaceReplaceCommand
    | AssumptionSetCommand,
    Field(discriminator="command_type"),
]


class CommandAppliedView(StrictModel):
    status: Literal["APPLIED"] = "APPLIED"
    changed_days: list[str]
    map_readiness: Literal["NEEDS_UPDATE"] = "NEEDS_UPDATE"


class CommandOutcome(StrictModel):
    applied: CommandAppliedView
    opaque_etag: str
    replayed: bool = False


class ClaimedTripView(StrictModel):
    status: Literal["CLAIMED"] = "CLAIMED"
    public_resource_id: str


class ClaimOutcome(StrictModel):
    claimed: ClaimedTripView
    opaque_etag: str
    replayed: bool = False


class DeletionOutcome(StrictModel):
    replayed: bool = False


class AccountTravelDataDeleteRequest(StrictModel):
    confirmation: Literal["DELETE_ALL_TRAVEL_DATA"]


class TravelDataDeletionStatusView(StrictModel):
    status: Literal["IN_PROGRESS", "COMPLETED", "RETRY_REQUIRED"]
    message: str
    next_action: Literal["NONE", "RETRY"]


class TravelDataDeletionOutcome(StrictModel):
    view: TravelDataDeletionStatusView
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


class TripUnderstandingSourcePayload(StrictModel):
    source_type: Literal["FIXED_DEMO", "TEXT", "SCREENSHOT_OCR"]
    text: str
    requires_confirmation_spans: tuple[ConfirmationSourceSpan, ...] = ()
    partial_source: bool = False


class PipelineOutput(StrictModel):
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination: dict[str, object]
    assumptions: list[dict[str, object]]
    proposal: InferenceProposal
    inference_binding: dict[str, object]
    compiler_receipt: dict[str, object]
    resolution_receipt: dict[str, object]
    activities: list[ResolvedActivity]
    claims: list[SourceClaimRecord]
    public_result: UserFacingTripResult
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
