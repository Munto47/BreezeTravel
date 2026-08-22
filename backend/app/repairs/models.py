from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.itineraries.models import ItineraryRevision


class RepairOperationType(str, Enum):
    ADJUST_TIME = "ADJUST_TIME"
    MOVE_WITHIN_DAY = "MOVE_WITHIN_DAY"
    MOVE_TO_DAY = "MOVE_TO_DAY"
    REPLACE_STOP = "REPLACE_STOP"
    INSERT_BREAK = "INSERT_BREAK"
    INSERT_MEAL = "INSERT_MEAL"
    CHANGE_HOTEL_AREA = "CHANGE_HOTEL_AREA"
    REMOVE_STOP = "REMOVE_STOP"
    SPLIT_GROUP = "SPLIT_GROUP"


class RepairStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    STALE = "STALE"


class RepairOperation(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation: RepairOperationType
    payload: dict[str, Any]
    rationale: str


class RepairOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    repair_id: str
    source_report_id: str
    base_itinerary_revision: int = Field(gt=0)
    operations: list[RepairOperation] = Field(min_length=1)
    targeted_finding_ids: list[str] = Field(min_length=1)
    edit_cost: float = Field(ge=0)
    risk_cost: float = Field(ge=0)
    route_cost_delta: float | None = None
    new_unknown_count: int = Field(ge=0)
    tradeoffs: list[str] = Field(default_factory=list)
    affected_member_ids: list[str] = Field(default_factory=list)
    result_preview: ItineraryRevision
    postcheck_report_id: str | None
    status: RepairStatus = RepairStatus.PROPOSED
    decided_by: str | None = None
    decision_reason: str | None = None
    decided_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def proposed_option_requires_postcheck(self) -> "RepairOption":
        if self.status in {RepairStatus.PROPOSED, RepairStatus.APPLIED} and not self.postcheck_report_id:
            raise ValueError("a feasible repair option requires postcheck_report_id")
        return self


class RepairApplyResult(BaseModel):
    repair: RepairOption
    new_revision: int
    postcheck_report_id: str
    idempotent_replay: bool = False
