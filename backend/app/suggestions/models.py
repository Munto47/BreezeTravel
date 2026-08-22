from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.importing.models import ResolvedPlaceReceipt
from app.audit.models import AuditSeverity, AuditStatus
from app.itineraries.models import ItineraryRevision
from app.schemas.place import Coordinates, RetrievalExecutionMode


class SuggestionIntent(str, Enum):
    NEARBY = "NEARBY"
    POPULAR = "POPULAR"
    FUN = "FUN"
    FOOD = "FOOD"


class SuggestionClassification(str, Enum):
    ON_ROUTE = "ON_ROUTE"
    ACCEPTABLE_DETOUR = "ACCEPTABLE_DETOUR"
    DEFER_TO_OTHER_DAY = "DEFER_TO_OTHER_DAY"
    INFEASIBLE = "INFEASIBLE"


class FreshnessStatus(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class HardGate(BaseModel):
    model_config = ConfigDict(frozen=True)

    passed: bool
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_reasons(self) -> "HardGate":
        if not self.passed and not self.reason_codes:
            raise ValueError("blocked hard gate requires a reason code")
        return self


class CandidateCurrentFact(BaseModel):
    """Provider-bound current fact which may participate in direct-accept audit.

    Route/community/LLM priors are intentionally impossible to promote through
    this model.  They remain ranking signals only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_type: str = Field(pattern=r"^(OPENING_HOURS|RESERVATION_POLICY|ACCESSIBILITY_POLICY|DIETARY_SUPPORT)$")
    value: Any
    provider: str = Field(min_length=1)
    observed_at: datetime
    valid_until: datetime | None = None
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_mode: RetrievalExecutionMode
    source_url: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_authority(self) -> "CandidateCurrentFact":
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("candidate current fact observed_at must be timezone-aware")
        if self.valid_until is not None and (
            self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None
        ):
            raise ValueError("candidate current fact valid_until must be timezone-aware")
        non_fact_sources = ("ugc", "wikivoyage", "community", "route_prior", "llm")
        if any(token in self.provider.casefold() for token in non_fact_sources):
            raise ValueError("community, route-prior and LLM sources cannot prove current facts")
        return self


class SuggestionGateFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    status: AuditStatus
    severity: AuditSeverity
    reason_code: str = Field(min_length=1)
    affected_stop_ids: tuple[str, ...] = ()
    evidence_fact_ids: tuple[str, ...] = ()


class SuggestionAuditGateReceipt(BaseModel):
    """Frozen proof that one candidate was evaluated by the Audit authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: AuditStatus
    task_id: str = Field(min_length=1)
    task_revision: int = Field(gt=0)
    task_workspace_revision: int | None = Field(default=None, ge=1)
    member_constraint_workspace_revision: int | None = Field(default=None, ge=1)
    member_constraint_revision_set: dict[str, int] = Field(default_factory=dict)
    evidence_snapshot_id: str = Field(min_length=1)
    evidence_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_report_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_rule_set_version: str = Field(min_length=1)
    slot_policy_version: str = Field(min_length=1)
    authority_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    findings: tuple[SuggestionGateFinding, ...] = ()
    evaluated_at: datetime

    @model_validator(mode="after")
    def validate_time(self) -> "SuggestionAuditGateReceipt":
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("suggestion audit gate evaluated_at must be timezone-aware")
        return self


class RouteReceiptLeg(str, Enum):
    PREVIOUS_TO_CANDIDATE = "PREVIOUS_TO_CANDIDATE"
    CANDIDATE_TO_NEXT = "CANDIDATE_TO_NEXT"
    PREVIOUS_TO_NEXT = "PREVIOUS_TO_NEXT"


class RouteReceipt(BaseModel):
    """Immutable evidence for one Provider-computed route leg."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leg: RouteReceiptLeg
    transport_mode: str = Field(pattern=r"^(walking|driving|transit)$")
    origin_place_id: str = Field(min_length=1)
    origin_coords: Coordinates
    destination_place_id: str = Field(min_length=1)
    destination_coords: Coordinates
    duration_minutes: int = Field(ge=0)
    provider: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    snapshot_id: str = Field(min_length=1)
    execution_mode: RetrievalExecutionMode
    max_age_seconds: int = Field(gt=0)
    source_url: str | None = None

    @model_validator(mode="after")
    def validate_receipt(self) -> "RouteReceipt":
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("route receipt observed_at must be timezone-aware")
        if self.snapshot_id.strip().casefold() in {"live", "latest", "current", "unknown"}:
            raise ValueError("route receipt snapshot_id must bind a concrete response")
        return self


class RouteDelta(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str = Field(pattern=r"^(AVAILABLE|UNAVAILABLE|UNKNOWN)$")
    # Insertion can improve an existing edge, so the delta may be negative.
    delta_route_minutes: int | None = None
    previous_to_candidate_minutes: int | None = Field(default=None, ge=0)
    candidate_to_next_minutes: int | None = Field(default=None, ge=0)
    previous_to_next_minutes: int | None = Field(default=None, ge=0)
    route_receipts: tuple[RouteReceipt, ...] = ()
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> "RouteDelta":
        if self.status == "AVAILABLE" and self.delta_route_minutes is None:
            raise ValueError("available route delta requires delta_route_minutes")
        if self.status != "AVAILABLE" and not self.reason_code:
            raise ValueError("unavailable route delta requires reason_code")
        if self.status != "AVAILABLE" and self.route_receipts:
            raise ValueError("unavailable route delta cannot carry applicable route receipts")
        if self.status != "AVAILABLE":
            if any(value is not None for value in (
                self.delta_route_minutes,
                self.previous_to_candidate_minutes,
                self.candidate_to_next_minutes,
                self.previous_to_next_minutes,
            )):
                raise ValueError("unavailable route delta cannot carry applicable route timings")
            return self

        values = (
            self.previous_to_candidate_minutes,
            self.candidate_to_next_minutes,
            self.previous_to_next_minutes,
        )
        append_shape = values[0] is not None and values[1:] == (None, None)
        prepend_shape = values[1] is not None and values[0] is None and values[2] is None
        edge_shape = all(value is not None for value in values)
        if not (append_shape or prepend_shape or edge_shape):
            raise ValueError("available route delta requires one anchor leg or one complete insertion edge")
        expected_delta = (
            values[0]
            if append_shape
            else values[1]
            if prepend_shape
            else int(values[0] or 0) + int(values[1] or 0) - int(values[2] or 0)
        )
        if self.delta_route_minutes != expected_delta:
            raise ValueError("route delta arithmetic is inconsistent with its legs")
        expected_legs = (
            {RouteReceiptLeg.PREVIOUS_TO_CANDIDATE}
            if append_shape
            else {RouteReceiptLeg.CANDIDATE_TO_NEXT}
            if prepend_shape
            else {
                RouteReceiptLeg.PREVIOUS_TO_CANDIDATE,
                RouteReceiptLeg.CANDIDATE_TO_NEXT,
                RouteReceiptLeg.PREVIOUS_TO_NEXT,
            }
        )
        receipts = {receipt.leg: receipt for receipt in self.route_receipts}
        if len(receipts) != len(self.route_receipts) or set(receipts) != expected_legs:
            raise ValueError("available route delta requires exactly one receipt for every required leg")
        duration_by_leg = {
            RouteReceiptLeg.PREVIOUS_TO_CANDIDATE: self.previous_to_candidate_minutes,
            RouteReceiptLeg.CANDIDATE_TO_NEXT: self.candidate_to_next_minutes,
            RouteReceiptLeg.PREVIOUS_TO_NEXT: self.previous_to_next_minutes,
        }
        if any(receipt.duration_minutes != duration_by_leg[leg] for leg, receipt in receipts.items()):
            raise ValueError("route receipt duration differs from route delta")
        if edge_shape:
            previous = receipts[RouteReceiptLeg.PREVIOUS_TO_CANDIDATE]
            candidate_next = receipts[RouteReceiptLeg.CANDIDATE_TO_NEXT]
            baseline = receipts[RouteReceiptLeg.PREVIOUS_TO_NEXT]
            if (
                previous.destination_place_id != candidate_next.origin_place_id
                or previous.origin_place_id != baseline.origin_place_id
                or candidate_next.destination_place_id != baseline.destination_place_id
                or previous.destination_coords != candidate_next.origin_coords
                or previous.origin_coords != baseline.origin_coords
                or candidate_next.destination_coords != baseline.destination_coords
            ):
                raise ValueError("route receipt endpoints do not form one insertion edge")
        return self


class EvidenceFreshness(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: FreshnessStatus
    observed_at: datetime | None = None
    max_age_seconds: int | None = Field(default=None, ge=0)
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_observation(self) -> "EvidenceFreshness":
        if self.observed_at is not None and (
            self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None
        ):
            raise ValueError("evidence freshness observed_at must be timezone-aware")
        if self.status is FreshnessStatus.FRESH and self.observed_at is None:
            raise ValueError("fresh evidence requires observed_at")
        if self.status is not FreshnessStatus.FRESH and not self.reason_code:
            raise ValueError("non-fresh evidence requires reason_code")
        return self


class FrozenCanonicalPlace(BaseModel):
    """Provider-backed canonical POI frozen at suggestion creation time."""

    model_config = ConfigDict(frozen=True)

    place_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    city: str = Field(min_length=1)
    district: str | None = None
    address: str | None = None
    category: str = Field(min_length=1)
    coords: Coordinates


class SuggestionCandidateDraft(BaseModel):
    """Internal ranking output; never accepted as authority from the client."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    canonical_place: FrozenCanonicalPlace
    provider_receipt: ResolvedPlaceReceipt
    provider_receipt_id: str = Field(min_length=1)
    rank_position: int = Field(gt=0)
    classification: SuggestionClassification
    source_prior_refs: list[str] = Field(default_factory=list)
    score_components: dict[str, float] = Field(min_length=1)
    total_score: float
    hard_gate: HardGate
    route_delta: RouteDelta
    evidence_freshness: EvidenceFreshness
    explanation_codes: list[str] = Field(min_length=1)
    current_facts: tuple[CandidateCurrentFact, ...] = ()
    audit_gate: SuggestionAuditGateReceipt | None = None

    @model_validator(mode="after")
    def validate_frozen_authority(self) -> "SuggestionCandidateDraft":
        receipt = self.provider_receipt
        place = self.canonical_place
        if receipt.canonical_place_id != place.place_id:
            raise ValueError("provider receipt and canonical place identities differ")
        if receipt.name != place.name or receipt.city != place.city:
            raise ValueError("provider receipt and canonical place facts differ")
        if receipt.longitude != place.coords.lng or receipt.latitude != place.coords.lat:
            raise ValueError("provider receipt and canonical coordinates differ")
        if receipt.category is not None and receipt.category != place.category:
            raise ValueError("provider receipt and canonical category differ")
        if (receipt.district or "").strip() != (place.district or "").strip():
            raise ValueError("provider receipt and canonical district differ")
        if (receipt.address or "").strip() != (place.address or "").strip():
            raise ValueError("provider receipt and canonical address differ")
        if self.evidence_freshness.observed_at != receipt.observed_at:
            raise ValueError("evidence freshness must bind the provider receipt observation")
        if (
            self.evidence_freshness.status is FreshnessStatus.FRESH
            and self.evidence_freshness.max_age_seconds is None
        ):
            raise ValueError("fresh provider evidence requires a finite max age")
        if self.hard_gate.passed == (self.classification is SuggestionClassification.INFEASIBLE):
            raise ValueError("INFEASIBLE classification and hard gate must agree")
        if not isfinite(self.total_score) or any(not isfinite(value) for value in self.score_components.values()):
            raise ValueError("suggestion scores must be finite")
        if self.audit_gate is not None:
            gate_passed = self.audit_gate.status is AuditStatus.SATISFIED
            if gate_passed != self.hard_gate.passed:
                raise ValueError("frozen hard gate must agree with authoritative audit gate")
        return self


class SuggestionCandidate(SuggestionCandidateDraft):
    suggestion_set_id: str = Field(min_length=1)


class SuggestionSetCreateInput(BaseModel):
    """Trusted input produced by the ranking/provider layer."""

    model_config = ConfigDict(frozen=True)

    suggestion_set_id: str | None = None
    workspace_id: str = Field(min_length=1)
    base_revision: int = Field(gt=0)
    day_index: int = Field(ge=0, le=4)
    insert_after_stop_id: str | None = None
    insert_before_stop_id: str | None = None
    intents: list[SuggestionIntent] = Field(min_length=1)
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1)
    provider_snapshot_id: str = Field(min_length=1)
    expires_at: datetime
    candidates: list[SuggestionCandidateDraft] = Field(min_length=1)
    session_id: str = Field(min_length=1)
    created_by: str = Field(min_length=1)
    result_status: str = Field(default="COMPLETE", pattern=r"^(COMPLETE|PARTIAL)$")
    shortage_reason_codes: list[str] = Field(default_factory=list)
    excluded_counts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_set(self) -> "SuggestionSetCreateInput":
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("suggestion set expiry must be timezone-aware")
        if self.provider_snapshot_id.strip().casefold() in {"live", "latest", "current", "unknown"}:
            raise ValueError("provider_snapshot_id must bind a concrete snapshot or run receipt")
        candidate_ids = [item.candidate_id for item in self.candidates]
        ranks = [item.rank_position for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate_id must be unique within a suggestion set")
        canonical_place_ids = [item.canonical_place.place_id for item in self.candidates]
        receipt_ids = [item.provider_receipt_id for item in self.candidates]
        if len(canonical_place_ids) != len(set(canonical_place_ids)):
            raise ValueError("canonical place must be unique within a suggestion set")
        if len(receipt_ids) != len(set(receipt_ids)):
            raise ValueError("provider receipt id must be unique within a suggestion set")
        if sorted(ranks) != list(range(1, len(ranks) + 1)):
            raise ValueError("candidate ranks must be unique and contiguous from one")
        return self


class SuggestionSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    suggestion_set_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    base_revision: int = Field(gt=0)
    day_index: int = Field(ge=0, le=4)
    insert_after_stop_id: str | None = None
    insert_before_stop_id: str | None = None
    intents: list[SuggestionIntent]
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1)
    provider_snapshot_id: str = Field(min_length=1)
    expires_at: datetime
    session_id: str = Field(min_length=1)
    candidates: list[SuggestionCandidate]
    created_by: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    result_status: str = Field(default="COMPLETE", pattern=r"^(COMPLETE|PARTIAL)$")
    shortage_reason_codes: list[str] = Field(default_factory=list)
    excluded_counts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_frozen_set(self) -> "SuggestionSet":
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("suggestion set expiry must be timezone-aware")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("suggestion set created_at must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("suggestion set expiry must be after its creation time")
        candidate_ids = [item.candidate_id for item in self.candidates]
        canonical_ids = [item.canonical_place.place_id for item in self.candidates]
        receipt_ids = [item.provider_receipt_id for item in self.candidates]
        ranks = [item.rank_position for item in self.candidates]
        if any(item.suggestion_set_id != self.suggestion_set_id for item in self.candidates):
            raise ValueError("candidate suggestion_set_id differs from its frozen set")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate_id must be unique within a suggestion set")
        if len(canonical_ids) != len(set(canonical_ids)):
            raise ValueError("canonical place must be unique within a suggestion set")
        if len(receipt_ids) != len(set(receipt_ids)):
            raise ValueError("provider receipt id must be unique within a suggestion set")
        if sorted(ranks) != list(range(1, len(ranks) + 1)):
            raise ValueError("candidate ranks must be unique and contiguous from one")
        return self


class RecommendationEventType(str, Enum):
    SUGGESTIONS_SHOWN = "suggestions_shown"
    CANDIDATE_PREVIEWED = "candidate_previewed"
    CANDIDATE_ACCEPTED = "candidate_accepted"
    CANDIDATE_DISMISSED = "candidate_dismissed"
    STOP_UNDONE = "stop_undone"
    LINE_COMPLETED = "line_completed"
    SUGGESTION_FAILED = "suggestion_failed"
    REVISION_CONFLICT = "revision_conflict"


class RecommendationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    event_type: RecommendationEventType
    revision_before: int | None = Field(default=None, gt=0)
    revision_after: int | None = Field(default=None, gt=0)
    suggestion_set_id: str | None = None
    candidate_id: str | None = None
    context_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    policy_version: str | None = None
    provider_snapshot_id: str | None = None
    rank_position: int | None = Field(default=None, gt=0)
    latency_ms: int | None = Field(default=None, ge=0)
    reason_code: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_occurred_at(self) -> "RecommendationEvent":
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("recommendation event occurred_at must be timezone-aware")
        return self


class RecommendationEventCommandResult(BaseModel):
    """Stable receipt returned by an idempotent event command."""

    event: RecommendationEvent
    idempotent_replay: bool = False


class AcceptSuggestionResult(BaseModel):
    accepted: bool = True
    suggestion_set_id: str
    candidate_id: str
    new_revision: int
    stop_id: str
    revision: ItineraryRevision
    event: RecommendationEvent
    idempotent_replay: bool = False
