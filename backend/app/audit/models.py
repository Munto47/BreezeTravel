from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.members.models import MemberConstraint


class AuditStatus(str, Enum):
    SATISFIED = "SATISFIED"
    VIOLATED = "VIOLATED"
    UNKNOWN = "UNKNOWN"


class AuditSeverity(str, Enum):
    BLOCKER = "BLOCKER"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class EvidenceFreshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICTING = "CONFLICTING"


class AuditDependency(str, Enum):
    DAY_ORDER = "DAY_ORDER"
    TIME_WINDOW = "TIME_WINDOW"
    ROUTE_EDGE = "ROUTE_EDGE"
    HOTEL = "HOTEL"
    WEATHER = "WEATHER"
    MEMBER_CONSTRAINT = "MEMBER_CONSTRAINT"
    EVIDENCE_FRESHNESS = "EVIDENCE_FRESHNESS"
    GLOBAL_BUDGET = "GLOBAL_BUDGET"


class EvidenceFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    fact_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    subject_type: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    fact_type: str = Field(min_length=1)
    value: Any = None
    provider: str = Field(min_length=1)
    source_url: str | None = None
    observed_at: datetime
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    response_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confidence: float = Field(ge=0, le=1)
    freshness_status: EvidenceFreshness


class ProviderFailure(BaseModel):
    provider: str
    error_category: str
    retryable: bool = False
    detail: str | None = None


class EvidenceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    itinerary_revision: int = Field(gt=0)
    provider_set: list[str] = Field(default_factory=list)
    policy_version: str = Field(min_length=1)
    facts: list[EvidenceFact] = Field(default_factory=list)
    provider_failures: list[ProviderFailure] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    supersedes_snapshot_id: str | None = None

    @model_validator(mode="after")
    def validate_fact_binding(self) -> "EvidenceSnapshot":
        if any(fact.snapshot_id != self.snapshot_id for fact in self.facts):
            raise ValueError("all evidence facts must belong to the snapshot")
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("evidence fact ids must be unique")
        return self


class AuditFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    status: AuditStatus
    severity: AuditSeverity
    reason_code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    input_values: dict[str, Any] = Field(default_factory=dict)
    affected_days: list[int] = Field(default_factory=list)
    affected_stop_ids: list[str] = Field(default_factory=list)
    affected_member_ids: list[str] = Field(default_factory=list)
    evidence_fact_ids: list[str] = Field(default_factory=list)
    repairable: bool = False
    confirmation_action: str | None = None


class AuditReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    itinerary_id: str = Field(min_length=1)
    itinerary_revision: int = Field(gt=0)
    task_id: str = Field(min_length=1)
    task_revision: int = Field(gt=0)
    member_constraint_revision_set: dict[str, int] = Field(default_factory=dict)
    evidence_snapshot_id: str = Field(min_length=1)
    audit_rule_set_version: str = Field(min_length=1)
    report_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    overall_status: AuditStatus
    findings: list[AuditFinding] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    supersedes_report_id: str | None = None


class AuditRunInput(BaseModel):
    workspace_id: str
    itinerary_revision: int = Field(gt=0)
    task_id: str
    task_revision: int = Field(gt=0)
    member_constraint_revision_set: dict[str, int] = Field(default_factory=dict)
    # The immutable, effective constraints used by this audit run.  Keeping the
    # values in the run input lets audit rules evaluate the exact member state
    # whose revisions are bound into ``member_constraint_revision_set``.
    member_constraints: list[MemberConstraint] = Field(default_factory=list)
    place_resolution_versions: dict[str, int] = Field(default_factory=dict)
