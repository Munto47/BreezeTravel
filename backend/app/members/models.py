from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConstraintHardness(str, Enum):
    HARD = "HARD"
    SOFT = "SOFT"


class ConstraintSource(str, Enum):
    MEMBER_EXPLICIT = "MEMBER_EXPLICIT"
    ORGANIZER = "ORGANIZER"
    ROOM_CONSENSUS = "ROOM_CONSENSUS"
    MEMORY = "MEMORY"
    INFERRED = "INFERRED"


class ConstraintConfirmationStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class TravelerProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: str = Field(min_length=1)
    member_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1, max_length=120)
    age_group: str = Field(min_length=1, max_length=40)
    child_age: int | None = Field(default=None, ge=0, le=17)
    child_height_cm: int | None = Field(default=None, ge=40, le=220)
    walking_limit_minutes: int | None = Field(default=None, ge=0, le=1440)
    requires_nap: bool = False
    wheelchair_or_stroller: bool = False
    dietary_restrictions: list[str] = Field(default_factory=list, max_length=30)
    medication_times: list[str] = Field(default_factory=list, max_length=30)
    latest_return_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    confirmed_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_child_fields(self) -> "TravelerProfile":
        if self.child_age is not None and self.age_group.lower() not in {"child", "儿童", "minor"}:
            raise ValueError("child_age requires a child age_group")
        return self


class MemberConstraintDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    constraint_id: str = Field(min_length=1)
    owner_member_id: str = Field(min_length=1)
    type: str = Field(min_length=1, max_length=80)
    operator: str = Field(min_length=1, max_length=40)
    value: Any
    hardness: ConstraintHardness
    priority: int = Field(default=0, ge=0, le=100)
    source: ConstraintSource
    confirmation_status: ConstraintConfirmationStatus = ConstraintConfirmationStatus.PENDING
    waivable_by: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def enforce_hard_constraint_provenance(self) -> "MemberConstraintDraft":
        if self.source in {ConstraintSource.MEMORY, ConstraintSource.INFERRED} and self.hardness == ConstraintHardness.HARD:
            raise ValueError("MEMORY and INFERRED constraints must remain SOFT")
        if self.hardness == ConstraintHardness.HARD and self.confirmation_status != ConstraintConfirmationStatus.CONFIRMED:
            raise ValueError("HARD constraints require explicit member confirmation")
        if len(self.waivable_by) != len(set(self.waivable_by)):
            raise ValueError("waivable_by members must be unique")
        return self


class MemberConstraint(MemberConstraintDraft):
    workspace_id: str = Field(min_length=1)
    revision: int = Field(gt=0)


class MemberConstraintWriteResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    constraint: MemberConstraint
    previous_workspace_revision: int = Field(ge=0)
    current_workspace_revision: int = Field(gt=0)
    stale_report_id: str | None = None
