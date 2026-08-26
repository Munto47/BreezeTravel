"""Three-state constraint verification contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class ConstraintStatus(str, Enum):
    SATISFIED = "SATISFIED"
    VIOLATED = "VIOLATED"
    UNKNOWN = "UNKNOWN"


class ConstraintCheck(BaseModel):
    constraint_id: str
    status: ConstraintStatus
    reason_code: str
    message: str
    day_index: Optional[int] = None
    place_id: Optional[str] = None
    evidence_refs: list[str] = Field(default_factory=list)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    repairable: bool = False


class VerificationReport(BaseModel):
    report_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    task_revision: int
    itinerary_id: str
    itinerary_version: int
    planning_input_hash: str
    overall_status: ConstraintStatus
    checks: list[ConstraintCheck] = Field(default_factory=list)
    verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    repair_rounds: int = 0
    unresolved_reasons: list[str] = Field(default_factory=list)

    @classmethod
    def from_checks(
        cls,
        *,
        task_id: str,
        task_revision: int,
        itinerary_id: str,
        itinerary_version: int,
        planning_input_hash: str,
        checks: list[ConstraintCheck],
        repair_rounds: int = 0,
        unresolved_reasons: Optional[list[str]] = None,
    ) -> "VerificationReport":
        if any(item.status == ConstraintStatus.VIOLATED for item in checks):
            overall = ConstraintStatus.VIOLATED
        elif any(item.status == ConstraintStatus.UNKNOWN for item in checks):
            overall = ConstraintStatus.UNKNOWN
        else:
            overall = ConstraintStatus.SATISFIED
        return cls(
            task_id=task_id,
            task_revision=task_revision,
            itinerary_id=itinerary_id,
            itinerary_version=itinerary_version,
            planning_input_hash=planning_input_hash,
            overall_status=overall,
            checks=checks,
            repair_rounds=repair_rounds,
            unresolved_reasons=unresolved_reasons or [],
        )
