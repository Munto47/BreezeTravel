from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import TripDateRange


NO_PREFERENCE = "NO_PREFERENCE"
UNSPECIFIED = "UNSPECIFIED"


class TripBriefStatus(str, Enum):
    DRAFT = "DRAFT"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    CONFIRMED = "CONFIRMED"


class BriefFieldOrigin(str, Enum):
    USER_TEXT = "USER_TEXT"
    PARSER = "PARSER"
    USER_CONFIRMED = "USER_CONFIRMED"
    INFERRED = "INFERRED"
    DEFAULT_NO_PREFERENCE = "DEFAULT_NO_PREFERENCE"
    UNSPECIFIED = "UNSPECIFIED"


class BriefFieldConfirmation(str, Enum):
    UNCONFIRMED = "UNCONFIRMED"
    CONFIRMED = "CONFIRMED"


class BriefHardness(str, Enum):
    HARD = "HARD"
    SOFT = "SOFT"
    NO_PREFERENCE = "NO_PREFERENCE"


class TransportMode(str, Enum):
    WALKING = "WALKING"
    TRANSIT = "TRANSIT"
    BICYCLING = "BICYCLING"
    DRIVING = "DRIVING"


class BriefSourceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> "BriefSourceSpan":
        if self.end <= self.start:
            raise ValueError("source span end must be after start")
        return self


class BriefFieldProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_spans: list[BriefSourceSpan] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    origin: BriefFieldOrigin
    confirmation: BriefFieldConfirmation = BriefFieldConfirmation.UNCONFIRMED
    hardness: BriefHardness = BriefHardness.SOFT

    @model_validator(mode="after")
    def inferred_values_cannot_be_hard(self) -> "BriefFieldProvenance":
        if self.origin == BriefFieldOrigin.INFERRED and self.hardness == BriefHardness.HARD:
            raise ValueError("INFERRED brief fields cannot be HARD")
        if self.origin == BriefFieldOrigin.DEFAULT_NO_PREFERENCE and self.hardness != BriefHardness.NO_PREFERENCE:
            raise ValueError("default no-preference fields must use NO_PREFERENCE hardness")
        return self


class ArrivalDeparture(BaseModel):
    model_config = ConfigDict(frozen=True)

    location: str | None = Field(default=None, max_length=200)
    at: datetime | None = None
    notes: str | None = Field(default=None, max_length=500)


class AccommodationBrief(BaseModel):
    model_config = ConfigDict(frozen=True)

    hotel_name: str | None = Field(default=None, max_length=200)
    area: str | None = Field(default=None, max_length=200)


class BriefRequirement(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str = Field(min_length=1, max_length=80)
    operator: str = Field(min_length=1, max_length=40)
    value: Any = None
    unit: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=8)
    applies_to: str | None = Field(default=None, max_length=120)
    source_spans: list[BriefSourceSpan] = Field(default_factory=list)


_REQUIRED_PROVENANCE_FIELDS = frozenset(
    {
        "city",
        "date_range",
        "traveler_count",
        "arrival",
        "departure",
        "accommodation",
        "transport_modes",
        "transport_restrictions",
        "budget",
        "dining_style",
        "lodging_style",
        "dietary_restrictions",
        "daily_pace",
        "activity_intensity",
    }
)


class TripBriefRevision(BaseModel):
    model_config = ConfigDict(frozen=True)

    brief_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    revision: int = Field(gt=0)
    parent_revision: int | None = Field(default=None, gt=0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    city: str = Field(min_length=1)
    date_range: TripDateRange
    traveler_count: int = Field(gt=0)
    arrival: ArrivalDeparture
    departure: ArrivalDeparture
    accommodation: AccommodationBrief
    transport_modes: list[TransportMode] = Field(default_factory=list)
    transport_restrictions: list[str] | str = UNSPECIFIED
    budget: dict[str, Any] | str = UNSPECIFIED
    dining_style: list[str] | str = UNSPECIFIED
    lodging_style: list[str] | str = UNSPECIFIED
    dietary_restrictions: list[str] | str = UNSPECIFIED
    daily_pace: str = UNSPECIFIED
    activity_intensity: str = UNSPECIFIED
    requirements: list[BriefRequirement] = Field(default_factory=list)
    source_intake_id: str | None = Field(default=None, min_length=1)
    source_intake_revision: int | None = Field(default=None, gt=0)
    field_provenance: dict[str, BriefFieldProvenance]
    status: TripBriefStatus
    created_by: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_revision_contract(self) -> "TripBriefRevision":
        if not self.city.strip():
            raise ValueError("city must not be blank")
        if (self.source_intake_id is None) != (self.source_intake_revision is None):
            raise ValueError("intake lineage id and revision must be present together")
        if self.parent_revision is not None and self.parent_revision >= self.revision:
            raise ValueError("parent brief revision must be older than revision")
        if set(self.field_provenance) != _REQUIRED_PROVENANCE_FIELDS:
            missing = sorted(_REQUIRED_PROVENANCE_FIELDS - set(self.field_provenance))
            extra = sorted(set(self.field_provenance) - _REQUIRED_PROVENANCE_FIELDS)
            raise ValueError(f"brief field provenance mismatch: missing={missing}, extra={extra}")
        if len(self.transport_modes) != len(set(self.transport_modes)):
            raise ValueError("transport modes must be unique")
        if self.status == TripBriefStatus.CONFIRMED:
            if not self.confirmed_by or self.confirmed_at is None:
                raise ValueError("confirmed brief requires confirmer and timestamp")
            required_confirmations = {"city", "date_range", "traveler_count"}
            if any(
                self.field_provenance[field_name].confirmation
                != BriefFieldConfirmation.CONFIRMED
                for field_name in required_confirmations
            ):
                raise ValueError("confirmed brief requires city, date range, and traveler count confirmation")
        elif self.confirmed_by is not None or self.confirmed_at is not None:
            raise ValueError("only confirmed briefs may contain confirmation receipt")
        return self


class TripCheckStage(str, Enum):
    PARSE = "PARSE"
    WAIT_BRIEF_CONFIRMATION = "WAIT_BRIEF_CONFIRMATION"
    RESOLVE_PLACES = "RESOLVE_PLACES"
    COLLECT_EVIDENCE = "COLLECT_EVIDENCE"
    AUDIT = "AUDIT"
    BUILD_ADVICE = "BUILD_ADVICE"
    WAIT_ADOPTION = "WAIT_ADOPTION"
    POSTCHECK = "POSTCHECK"


class TripCheckRunStatus(str, Enum):
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PRIVACY_BLOCKED = "PRIVACY_BLOCKED"
    CANCELLED = "CANCELLED"


class RunBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_tokens: int = Field(default=0, ge=0)
    max_provider_queries: int = Field(default=0, ge=0)
    max_retries: int = Field(default=0, ge=0)
    timeout_seconds: int = Field(gt=0)
    max_cost_usd: float = Field(default=0, ge=0)


class RunSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "trip-check-run-spec-v1"
    commit_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    prompt_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    rule_set_version: str = Field(min_length=1)
    execution_mode: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    fault_profile: str = Field(min_length=1)
    random_seed: int
    budget: RunBudget


class RunPartialFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: TripCheckStage
    provider: str | None = None
    category: str = Field(min_length=1)
    affected_fields: list[str] = Field(default_factory=list)
    retryable: bool = False


class TripCheckRun(BaseModel):
    run_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    itinerary_revision: int = Field(gt=0)
    brief_id: str = Field(min_length=1)
    brief_revision: int = Field(gt=0)
    stage: TripCheckStage
    stage_attempt: int = Field(default=1, gt=0)
    lease_owner: str | None = None
    lease_until: datetime | None = None
    run_spec: RunSpec
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_stages: list[TripCheckStage] = Field(default_factory=list)
    partial_failures: list[RunPartialFailure] = Field(default_factory=list)
    status: TripCheckRunStatus
    evidence_snapshot_id: str | None = None
    report_id: str | None = None
    advice_bundle_id: str | None = None
    version: int = Field(default=1, gt=0)
    created_by: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_run_state(self) -> "TripCheckRun":
        expected_config_hash = sha256_canonical(self.run_spec.model_dump(mode="json"))
        if self.config_hash != expected_config_hash:
            raise ValueError("config hash must match the immutable RunSpec")
        if len(self.completed_stages) != len(set(self.completed_stages)):
            raise ValueError("completed stages must be unique")
        if (self.lease_owner is None) != (self.lease_until is None):
            raise ValueError("lease owner and expiry must be present together")
        if self.status == TripCheckRunStatus.PARTIAL and not self.partial_failures:
            raise ValueError("partial runs require at least one partial failure")
        return self


class TripCheckRunEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: int = Field(gt=0)
    run_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    stage: TripCheckStage
    run_version: int = Field(gt=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TripCheckStageAttemptRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    stage: TripCheckStage
    attempt: int = Field(gt=0)
    state: str = Field(pattern=r"^(STARTED|SUCCEEDED|FAILED_RETRYABLE|FAILED_FINAL)$")
    stage_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_output_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    failure_category: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class SideEffectReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    receipt_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    stage: TripCheckStage
    side_effect_key: str = Field(min_length=1)
    effect_type: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider: str | None = None
    status: str = Field(pattern=r"^(SUCCEEDED|PARTIAL|FAILED)$")
    receipt: dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TripCheckDomainTraceRecord(BaseModel):
    """Redacted, deterministic read model assembled from PostgreSQL authority."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = "trip-check-domain-trace-v1"
    sequence: int = Field(gt=0)
    record_type: str = Field(pattern=r"^(RUN_EVENT|STAGE_ATTEMPT|SIDE_EFFECT_RECEIPT)$")
    run_id: str = Field(min_length=1)
    itinerary_revision: int = Field(gt=0)
    brief_revision: int = Field(gt=0)
    evidence_snapshot_id: str | None = None
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rule_set_version: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    execution_mode: str = Field(min_length=1)
    stage: TripCheckStage
    stage_attempt: int = Field(gt=0)
    event_id: int | None = Field(default=None, gt=0)
    event_type: str | None = None
    run_version: int | None = Field(default=None, gt=0)
    attempt_state: str | None = None
    receipt_id: str | None = None
    effect_type: str | None = None
    receipt_status: str | None = None
    failure_category: str | None = None
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: datetime


class AdviceAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    advice_id: str = Field(min_length=1)
    finding_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    expected_impact: str = Field(min_length=1)
    uncertainty: str = Field(min_length=1)
    candidate_set_id: str | None = None
    evidence_fact_ids: list[str] = Field(default_factory=list)
    provider_receipt_ids: list[str] = Field(default_factory=list)
    route_delta: dict[str, Any] | None = None
    repair_id: str | None = None
    tradeoffs: list[str] = Field(default_factory=list)


class AdviceBundle(BaseModel):
    model_config = ConfigDict(frozen=True)

    advice_bundle_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    report_id: str = Field(min_length=1)
    itinerary_revision: int = Field(gt=0)
    brief_revision: int = Field(gt=0)
    evidence_snapshot_id: str = Field(min_length=1)
    actions: list[AdviceAction] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def advice_ids_must_be_unique(self) -> "AdviceBundle":
        ids = [item.advice_id for item in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("advice ids must be unique within a bundle")
        return self


class TripCheckPostcheckLineage(BaseModel):
    model_config = ConfigDict(frozen=True)

    lineage_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    advice_bundle_id: str = Field(min_length=1)
    repair_id: str = Field(min_length=1)
    source_report_id: str = Field(min_length=1)
    source_itinerary_revision: int = Field(gt=0)
    result_itinerary_revision: int = Field(gt=0)
    postcheck_report_id: str = Field(min_length=1)
    postcheck_snapshot_id: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def result_revision_must_advance(self) -> "TripCheckPostcheckLineage":
        if self.result_itinerary_revision <= self.source_itinerary_revision:
            raise ValueError("postcheck lineage must advance the itinerary revision")
        return self
