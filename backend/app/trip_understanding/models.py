from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.trip_understanding.timing import ActivityTiming


MAX_TRIP_ACTIVITIES = 160


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


class ProposedMention(ActivityTiming):
    mention_id: str
    raw_text: str = Field(min_length=1)
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    role: ActivityRole
    day_index: int | None = Field(default=None, ge=1, le=14)
    sequence_index: int = Field(ge=0)
    atomic_place_name: str | None = None
    category_hint: str | None = None
    meal_role: Literal["BREAKFAST", "LUNCH", "DINNER", "SNACK"] | None = None
    lodging_event: Literal["OVERNIGHT", "CHECK_OUT", "DEPARTURE", "LUGGAGE_PICKUP"] | None = None
    lodging_scope: Literal["WHOLE_TRIP", "DAY"] | None = None
    lodging_role_uncertain: bool = False
    pending_lodging_scope: bool = False
    pending_lodging_issue_count: int = Field(default=0, ge=0)
    lodging_excluded_nights: list[Annotated[int, Field(ge=1, le=14)]] = Field(default_factory=list, max_length=14)
    lodging_exclusion_evidence: str | None = None
    lodging_exclusion_evidence_start: int | None = Field(default=None, ge=0)
    lodging_exclusion_evidence_end: int | None = Field(default=None, ge=0)
    lodging_evidence: str | None = None
    lodging_evidence_start: int | None = Field(default=None, ge=0)
    lodging_evidence_end: int | None = Field(default=None, ge=0)
    time_hint: str | None = None
    city_hint: str | None = None
    city_evidence: str | None = None
    choice_group_id: str | None = None
    choice_group_selectable: bool = False
    branch_id: str | None = None
    branch_label: str | None = None
    parent_mention_id: str | None = None
    relation_type: Literal["INTERNAL_DETAIL"] | None = None
    detail_kind: Literal["VISIT", "ENTRY", "EXIT", "EXTERIOR_ONLY", "PICKUP_ONLY"] | None = None
    role_evidence: str | None = None
    role_evidence_start: int | None = Field(default=None, ge=0)
    role_evidence_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_span(self) -> "ProposedMention":
        if self.span_end <= self.span_start:
            raise ValueError("mention span must be non-empty")
        return self


class SemanticDiagnostic(StrictModel):
    """Private, source-bound recovery metadata; never a public error payload."""

    category: str = Field(min_length=1, max_length=80)
    field: str = Field(default="document", max_length=120)
    span_start: int | None = Field(default=None, ge=0)
    span_end: int | None = Field(default=None, ge=0)
    retryable: bool = True


class InferenceProposal(StrictModel):
    schema_version: Literal["trip-understanding-proposal-v1"] = "trip-understanding-proposal-v1"
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_name: str
    destination_basis: DestinationBasis = DestinationBasis.EXPLICIT
    mentions: list[ProposedMention]
    binding: dict[str, object]
    day_labels: dict[int, str] = Field(default_factory=dict)
    day_count: int = Field(default=0, ge=0, le=14)
    unprocessed_count: int = Field(default=0, ge=0)
    # Only source-scoped processing may assign unfinished work to a day.
    # Unknown attribution remains in the global count, never guessed from an empty day.
    unprocessed_by_day: dict[Annotated[int, Field(ge=1, le=14)], Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    diagnostics: list[SemanticDiagnostic] = Field(default_factory=list)


class SourceSemanticPlan(InferenceProposal):
    """Source-validated meaning produced by every current model adapter.

    Days, roles, branches, order and per-visit city evidence are authoritative
    here. Downstream identity lookup may resolve a place or leave it pending;
    it must not reinterpret those semantics. ``binding`` contains observations
    only and cannot select a different processing contract.

    Contract additions are optional fields with defaults for historical records;
    the type itself requires no wire discriminator or public version change.
    Readers replaying model drafts must explicitly construct this type rather
    than infer it from diagnostic text.
    """

    # Explicit saved-route alternatives with no assigned day are exposed through
    # the private supplementary view. Ordinary model plans retain their defaults.
    unassigned_alternative_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_unassigned_alternatives(self) -> "SourceSemanticPlan":
        allowed = {item.mention_id for item in self.mentions
                   if item.role == ActivityRole.OPTIONAL and item.day_index is None and item.atomic_place_name}
        selected = set(self.unassigned_alternative_ids)
        if len(selected) != len(self.unassigned_alternative_ids) or not selected <= allowed:
            raise ValueError("unassigned alternatives must identify distinct undated optional mentions")
        return self


class CompiledActivity(StrictModel):
    activity_id: str
    public_activity_token: str
    mention: ProposedMention
    eligible_for_place_search: bool


def safe_poi_photo_url(value: object) -> str | None:
    """Only POI image CDNs, no credentials, query secrets or arbitrary origins."""
    if not isinstance(value, str) or not value or len(value) > 1000:
        return None
    if any(ord(char) < 33 for char in value) or "\\" in value:
        return None
    try:
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"store.is.autonavi.com", "aos-cdn-image.amap.com", "aos-comment.amap.com", "vdata.amap.com"}
            or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.query or parsed.fragment
            or not parsed.path.startswith("/") or parsed.path == "/"
        ):
            return None
        return parsed._replace(scheme="https").geturl()
    except ValueError:
        return None


class ResolvedPlace(StrictModel):
    canonical_place_id: str
    name: str
    category: str
    area_or_address: str
    provider_binding: dict[str, object]
    photo_url: str | None = None

    @field_validator("photo_url", mode="before")
    @classmethod
    def valid_photo(cls, value: object) -> str | None:
        return safe_poi_photo_url(value)


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


class KnowledgeSuggestionView(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal[
        "TYPICAL_DURATION",
        "SUITABLE_TIME",
        "NIGHT_VIEW",
        "SEASON",
        "RESERVATION_ADVICE",
    ]
    text: str = Field(min_length=3, max_length=300)
    source_name: str = Field(min_length=2, max_length=200)
    source_url: str = Field(pattern=r"^https://", max_length=1000)
    freshness: str = Field(min_length=3, max_length=100)

    @field_validator("source_url")
    @classmethod
    def source_url_is_public_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            value != value.strip()
            or parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("source_url must be a public HTTPS URL")
        return value


class SourceDetailView(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    optional: bool = False


class ActivityCardView(ActivityTiming):
    photo_url: str | None = None
    city: str | None = None

    @field_validator("photo_url", mode="before")
    @classmethod
    def valid_photo(cls, value: object) -> str | None:
        return safe_poi_photo_url(value)

    activity_token: str = Field(min_length=20, max_length=80)
    name: str
    category: str
    meal_role: Literal["BREAKFAST", "LUNCH", "DINNER", "SNACK"] | None = None
    lodging_event: Literal["OVERNIGHT", "CHECK_OUT", "DEPARTURE", "LUGGAGE_PICKUP", "VISIT_ONLY"] | None = None
    lodging_scope: Literal["WHOLE_TRIP", "DAY"] | None = None
    lodging_role_uncertain: bool = False
    lodging_excluded_nights: list[Annotated[int, Field(ge=1, le=14)]] = Field(default_factory=list, max_length=14)
    area_or_address: str
    time_hint: str | None = None
    status: Literal["READY", "NEEDS_CONFIRMATION"]
    available_actions: list[Literal["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"]]
    knowledge_suggestions: list[KnowledgeSuggestionView] = Field(
        default_factory=list,
        max_length=3,
    )
    source_details: list[SourceDetailView] = Field(default_factory=list, max_length=MAX_TRIP_ACTIVITIES)


class ActivityAlternativeView(ActivityTiming):
    name: str = Field(min_length=1, max_length=40)
    category: str = Field(min_length=1, max_length=40)
    city: str | None = None
    activity_token: str | None = Field(default=None, min_length=20, max_length=80)
    choice_group_token: str | None = Field(default=None, min_length=20, max_length=80)
    choice_group_selectable: bool = False
    branch_token: str | None = Field(default=None, min_length=20, max_length=80)
    branch_label: str | None = Field(default=None, max_length=40)
    insertion_position: int | None = Field(default=None, ge=0, le=MAX_TRIP_ACTIVITIES)
    after_activity_token: str | None = Field(default=None, min_length=20, max_length=80)
    before_activity_token: str | None = Field(default=None, min_length=20, max_length=80)
    meal_role: Literal["BREAKFAST", "LUNCH", "DINNER", "SNACK"] | None = None
    source_details: list[SourceDetailView] = Field(default_factory=list, max_length=MAX_TRIP_ACTIVITIES)


class ChoiceSelectionView(StrictModel):
    choice_group_token: str = Field(min_length=20, max_length=80)
    branch_token: str = Field(min_length=20, max_length=80)
    activity_tokens: list[Annotated[str, Field(min_length=20, max_length=80)]] = Field(default_factory=list, max_length=MAX_TRIP_ACTIVITIES)
    status: Literal["SELECTED", "MODIFIED"] = "SELECTED"


class PendingLodgingRefView(StrictModel):
    pending_token: str = Field(min_length=20, max_length=80)
    status: Literal["NEEDS_CONFIRMATION"] = "NEEDS_CONFIRMATION"
    unprocessed_count: int = Field(default=1, ge=1)


class LodgingConstraintView(ActivityCardView):
    scope: Literal["WHOLE_TRIP", "NIGHTS"]
    overnight_days: list[Annotated[int, Field(ge=1, le=13)]] = Field(min_length=1, max_length=13)


class MealSlotView(StrictModel):
    meal_role: Literal["BREAKFAST", "LUNCH", "DINNER", "SNACK", "UNSPECIFIED"]
    preference_text: str | None = Field(default=None, min_length=1, max_length=1000)
    after_activity_token: str | None = None
    before_activity_token: str | None = None
    selection_status: Literal["UNKNOWN", "UNSELECTED", "SELECTED"] = "UNKNOWN"
    selected_activity_token: str | None = Field(default=None, min_length=20, max_length=80)

    @model_validator(mode="after")
    def selection_has_token(self):
        if (self.selection_status == "SELECTED") != (self.selected_activity_token is not None):
            raise ValueError("selected meal status and activity must agree")
        return self


class TripDayView(StrictModel):
    label: str
    activities: list[ActivityCardView]
    alternatives: list[ActivityAlternativeView] = Field(default_factory=list)
    choice_selections: list[ChoiceSelectionView] = Field(default_factory=list, max_length=80)
    meal_slots: list[MealSlotView] = Field(default_factory=list)
    unprocessed_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def selected_meals_belong_to_day(self):
        cards = {card.activity_token: card for card in self.activities}
        selected_tokens = set()
        for slot in self.meal_slots:
            if slot.selection_status != "SELECTED":
                continue
            selected = cards.get(slot.selected_activity_token)
            if (selected is None or selected.category != "餐饮" or selected.meal_role != (None if slot.meal_role == "UNSPECIFIED" else slot.meal_role)
                    or slot.selected_activity_token in selected_tokens):
                raise ValueError("selected meal must reference this day's matching restaurant")
            selected_tokens.add(slot.selected_activity_token)
        return self

    @model_validator(mode="after")
    def selected_choices_belong_to_day(self):
        groups = set()
        cards = {card.activity_token for card in self.activities}
        used = set()
        for selection in self.choice_selections:
            members = [item for item in self.alternatives if item.choice_group_token == selection.choice_group_token
                       and item.branch_token == selection.branch_token]
            tokens = set(selection.activity_tokens)
            if (selection.choice_group_token in groups or not members or len(tokens) != len(selection.activity_tokens)
                    or not tokens <= cards or used.intersection(tokens)
                    or (selection.status == "SELECTED" and len(tokens) != len(members))):
                raise ValueError("choice selection must reference its current day's branch and cards")
            groups.add(selection.choice_group_token)
            used.update(tokens)
        return self


class MapReadinessView(StrictModel):
    status: Literal["PREPARING", "AVAILABLE", "NEEDS_UPDATE", "LIMITED", "UNAVAILABLE"]
    message: str
    available_actions: list[Literal["VIEW_MAP", "RENDER_MAP"]] = Field(default_factory=list)


class StayCandidateView(StrictModel):
    max_single_leg_minutes: int | None = Field(default=None, ge=0)
    candidate_token: str = Field(min_length=20, max_length=100)
    name: str
    brand: str
    category: str
    area_or_address: str
    commute_summary: str
    transfer_count: int = Field(ge=0)
    reason: str
    available_actions: list[Literal["CHOOSE_STAY"]]
    selected: bool = False
    brand_group: str | None = None
    brand_note: str | None = None


class StaySegmentView(StrictModel):
    segment_token: str
    city: str | None = None
    overnight_days: list[str] = Field(default_factory=list)
    status: Literal["PREPARING", "AVAILABLE", "NEEDS_UPDATE", "LIMITED", "UNAVAILABLE"]
    message: str
    candidates: list[StayCandidateView] = Field(default_factory=list)
    preserved_hotels: list[str] = Field(default_factory=list)
    expected_boundary_count: int = Field(default=0, ge=0)
    missing_boundary_count: int = Field(default=0, ge=0)


class StaySuggestionView(StrictModel):
    status: Literal["PREPARING", "AVAILABLE", "NEEDS_UPDATE", "LIMITED", "UNAVAILABLE"]
    message: str
    area_summary: str | None = None
    searched_scopes: list[str] = Field(default_factory=list)
    candidates: list[StayCandidateView] = Field(default_factory=list)
    available_actions: list[Literal["CHOOSE_STAY"]] = Field(default_factory=list)
    segments: list[StaySegmentView] = Field(default_factory=list)


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


class TripRecognitionCoverage(StrictModel):
    """Counts, not source fragments or diagnostic codes, for incomplete results."""

    recognized_place_count: int = Field(default=0, ge=0)
    confirmed_place_count: int = Field(default=0, ge=0)
    unresolved_place_count: int = Field(default=0, ge=0)
    unclassified_mention_count: int = Field(default=0, ge=0)
    unprocessed_count: int = Field(default=0, ge=0)
    complete: bool = False


class UserFacingTripResult(StrictModel):
    status: Literal["READY", "PARTIAL_RESULT", "BASIC_ONLY", "LIMITED"]
    assumptions: list[AssumptionChipView]
    days: list[TripDayView]
    map: MapReadinessView
    stay: StaySuggestionView
    available_actions: list[Literal["EDIT_ASSUMPTIONS", "EDIT_CARDS"]]
    can_undo: bool = False
    can_redo: bool = False
    ownership: Literal["ANONYMOUS", "ACCOUNT"] = "ANONYMOUS"
    expires_at: datetime | None = None
    is_demo: bool = False
    updated_at: datetime | None = None
    coverage: TripRecognitionCoverage | None = None
    pending_lodgings: list[PendingLodgingRefView] = Field(default_factory=list)
    lodging_constraints: list[LodgingConstraintView] = Field(default_factory=list)


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
    affected_activity_tokens: list[str] = Field(default_factory=list)
    can_preview: bool = False
    depends_on_routes: bool = False
    basis_status: Literal["CURRENT", "NEEDS_RECHECK"] = "CURRENT"


class PublicTripChecksView(StrictModel):
    status: Literal["READY", "STILL_NEEDS_CONFIRMATION"]
    message: str
    items: list[PublicTripCheckItem] = Field(max_length=3)
    remaining_must_adjust: int = Field(ge=0)
    available_actions: list[Literal["PREVIEW_CHANGE"]] = Field(default_factory=list)


class ChangePreviewRequest(StrictModel):
    check_token: str = Field(min_length=20, max_length=100)


class PublicTimingChange(StrictModel):
    activity_token: str
    day_label: str
    name: str
    before: ActivityTiming
    after: ActivityTiming


class PublicChangePreview(StrictModel):
    change_token: str = Field(min_length=20, max_length=100)
    title: str
    summary: str
    affected_days: list[str] = Field(default_factory=list)
    before: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)
    changes: list[PublicTimingChange] = Field(default_factory=list)
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


FullSourceRequest = TextSourceRequest


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


class TripUnderstandingProgressMetrics(StrictModel):
    day_count: int = Field(default=0, ge=0, le=14)
    card_count: int = Field(default=0, ge=0)
    places_checked: int = Field(default=0, ge=0)
    places_total: int = Field(default=0, ge=0)


class TripUnderstandingProgressView(StrictModel):
    status: Literal["PROCESSING"] = "PROCESSING"
    message: str
    retry_after_ms: int = Field(default=500, ge=100, le=5000)
    phase: Literal["RECEIVED", "CARDS_AVAILABLE", "CHECKING_PLACES"] = "RECEIVED"
    event_cursor: int = Field(default=0, ge=0)
    progress: TripUnderstandingProgressMetrics = Field(
        default_factory=TripUnderstandingProgressMetrics
    )
    snapshot: UserFacingTripResult | None = None


class PipelineProgressUpdate(StrictModel):
    phase: Literal["CARDS_AVAILABLE", "CHECKING_PLACES"]
    message: Literal["日期和卡片已整理", "正在核对地点"]
    progress: TripUnderstandingProgressMetrics
    snapshot: UserFacingTripResult
    internal_binding: dict[str, object] = Field(default_factory=dict)


class PublicEventPayload(StrictModel):
    status: Literal["PROCESSING", "READY", "PARTIAL", "CANCELLED", "FAILED"]
    message: Literal[
        "正在整理每天行程",
        "日期和卡片已整理",
        "正在核对地点",
        "卡片已可用",
        "已停止整理，保留当前卡片",
        "已停止整理，没有可保留的卡片",
        "这次没有整理完成，可以重新尝试",
        "这次整理的内容超过 160 项上限，请分成多份行程后再试。",
        "这次整理的行程超过 14 天上限，请分成多份行程后再试。",
    ]
    phase: Literal["RECEIVED", "CARDS_AVAILABLE", "CHECKING_PLACES"] | None = None
    progress: TripUnderstandingProgressMetrics = Field(
        default_factory=TripUnderstandingProgressMetrics
    )
    snapshot: UserFacingTripResult | None = None


class PublicEventRecord(StrictModel):
    event_id: int = Field(gt=0)
    event_type: Literal["progress", "result_available"]
    payload: PublicEventPayload


class PublicResourceRecord(StrictModel):
    understanding_id: str
    public_resource_id: str
    state: Literal["PROCESSING", "READY", "PARTIAL", "CANCELLED", "FAILED", "DELETED"]
    current_result_id: str | None = None
    ownership: Literal["ANONYMOUS", "ACCOUNT"] = "ANONYMOUS"
    expires_at: datetime | None = None
    # Internal authorization/readback state, never part of the public result.
    failure_category: str | None = Field(default=None, exclude=True)


class StoredResult(StrictModel):
    result: UserFacingTripResult
    opaque_etag: str


class TripUnderstandingCancelView(StrictModel):
    status: Literal["STOPPED_WITH_DRAFT", "STOPPED_EMPTY", "ALREADY_FINISHED"]
    message: str
    has_editable_result: bool


class TripUnderstandingCancelOutcome(StrictModel):
    cancelled: TripUnderstandingCancelView
    opaque_etag: str | None = None
    replayed: bool = False


class CreateOutcome(StrictModel):
    accepted: TripUnderstandingAcceptedView
    replayed: bool = False


class ActivityInsertCommand(ActivityTiming):
    city: str | None = Field(default=None, max_length=40)
    command_type: Literal["ACTIVITY_INSERT"]
    day_index: int = Field(ge=1, le=14)
    position: int = Field(ge=0, le=MAX_TRIP_ACTIVITIES)
    name: str = Field(min_length=1, max_length=40)
    category: str = Field(default="地点", min_length=1, max_length=40)
    area_or_address: str = Field(default="地点待确认", min_length=1, max_length=120)
    time_hint: str | None = Field(default=None, max_length=80)


class AlternativeInsertCommand(StrictModel):
    command_type: Literal["ALTERNATIVE_INSERT"]
    day_index: int = Field(strict=True, ge=1, le=14)
    alternative_token: str = Field(min_length=20, max_length=80)
    position: int = Field(strict=True, ge=0, le=MAX_TRIP_ACTIVITIES)


class ChoiceSelectCommand(StrictModel):
    command_type: Literal["CHOICE_SELECT"]
    day_index: int = Field(ge=1, le=14)
    choice_group_token: str = Field(min_length=20, max_length=80)
    branch_token: str = Field(min_length=20, max_length=80)
    position: int | None = Field(default=None, ge=0, le=MAX_TRIP_ACTIVITIES)


class ChoiceClearCommand(StrictModel):
    command_type: Literal["CHOICE_CLEAR"]
    day_index: int = Field(ge=1, le=14)
    choice_group_token: str = Field(min_length=20, max_length=80)
    preserve_activities: bool = Field(default=False, strict=True)


class ActivityDeleteCommand(StrictModel):
    command_type: Literal["ACTIVITY_DELETE"]
    activity_token: str = Field(min_length=20, max_length=80)


class ActivityMoveCommand(StrictModel):
    command_type: Literal["ACTIVITY_MOVE"]
    activity_token: str = Field(min_length=20, max_length=80)
    target_day_index: int = Field(ge=1, le=14)
    target_position: int = Field(ge=0, le=MAX_TRIP_ACTIVITIES)


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


class ActivityTimeSetCommand(ActivityTiming):
    command_type: Literal["ACTIVITY_TIME_SET"]
    activity_token: str = Field(min_length=20, max_length=80)


class ActivityTimesShiftCommand(StrictModel):
    command_type: Literal["ACTIVITY_TIMES_SHIFT"]
    activity_tokens: list[str] = Field(min_length=1, max_length=MAX_TRIP_ACTIVITIES)
    minutes: int = Field(gt=0, le=1440)


class ActivityTimingUpdate(StrictModel):
    activity_token: str = Field(min_length=20, max_length=80)
    start_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ActivityTimesApplyCommand(StrictModel):
    command_type: Literal["ACTIVITY_TIMES_APPLY"]
    changes: list[ActivityTimingUpdate] = Field(min_length=1, max_length=MAX_TRIP_ACTIVITIES)


class PlaceConfirmCommand(StrictModel):
    command_type: Literal["PLACE_CONFIRM"]
    activity_token: str = Field(min_length=20, max_length=80)
    candidate_token: str = Field(min_length=40, max_length=6000)


class LodgingRecoveryIntent(StrictModel):
    kind: Literal["WHOLE_TRIP", "NIGHTS", "VISIT_ONLY"]
    overnight_days: list[Annotated[int, Field(ge=1, le=13)]] = Field(default_factory=list, max_length=13)
    day_index: int | None = Field(default=None, ge=1, le=14)
    before_activity_token: str | None = Field(default=None, min_length=20, max_length=80)

    @model_validator(mode="after")
    def consistent_scope(self):
        if self.kind == "VISIT_ONLY":
            if self.day_index is None or self.overnight_days:
                raise ValueError("visit recovery requires a day and no overnight scope")
        elif self.day_index is not None or self.before_activity_token is not None:
            raise ValueError("overnight recovery cannot invent a visit position")
        elif (self.kind == "NIGHTS") != bool(self.overnight_days):
            raise ValueError("specific nights require an explicit night selection")
        if len(set(self.overnight_days)) != len(self.overnight_days):
            raise ValueError("night selection cannot contain duplicates")
        self.overnight_days.sort()
        return self


class LodgingRecoverCommand(StrictModel):
    command_type: Literal["LODGING_RECOVER"]
    pending_token: str = Field(min_length=20, max_length=80)
    candidate_token: str = Field(min_length=40, max_length=6000)
    intent: LodgingRecoveryIntent


class UndoCommand(StrictModel):
    command_type: Literal["UNDO"]


class RedoCommand(StrictModel):
    command_type: Literal["REDO"]


class SourceMealRef(StrictModel):
    day_index: int = Field(ge=1, le=14, strict=True)
    slot_index: int = Field(ge=0, strict=True)


class DiningInsertCommand(StrictModel):
    command_type: Literal["DINING_INSERT"]
    after_activity_token: str = Field(min_length=20, max_length=80)
    insert_before: bool = False
    meal_role: Literal["BREAKFAST", "LUNCH", "DINNER", "SNACK"] | None = None
    meal_slot: SourceMealRef | None = None
    candidate_token: str = Field(min_length=40, max_length=8192)


TripUnderstandingCommand = Annotated[
    ActivityInsertCommand
    | AlternativeInsertCommand
    | ChoiceSelectCommand
    | ChoiceClearCommand
    | DiningInsertCommand
    | ActivityDeleteCommand
    | ActivityMoveCommand
    | ActivityTextEditCommand
    | PlaceReplaceCommand
    | PlaceConfirmCommand
    | LodgingRecoverCommand
    | ActivityTimeSetCommand
    | ActivityTimesShiftCommand
    | ActivityTimesApplyCommand
    | UndoCommand
    | RedoCommand
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
    internal_binding: dict[str, object] = Field(default_factory=dict)
    initial_plan: SourceSemanticPlan | None = None


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
