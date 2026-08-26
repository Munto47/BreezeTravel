"""Machine-checkable travel task contract.

The schema deliberately keeps explicit user requirements separate from memory
and inferred preferences.  Only explicit/consensus constraints are eligible to
be hard constraints; memory is always injected as a soft preference.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class ConstraintSource(str, Enum):
    USER_EXPLICIT = "user_explicit"
    ROOM_CONSENSUS = "room_consensus"
    MEMORY = "memory"
    INFERRED = "inferred"


class DateRange(BaseModel):
    start: Optional[date] = None
    days: int = Field(default=0, ge=0, le=30)


class Travelers(BaseModel):
    adults: int = Field(default=1, ge=0, le=50)
    children: int = Field(default=0, ge=0, le=50)
    seniors: int = Field(default=0, ge=0, le=50)

    @property
    def total(self) -> int:
        return self.adults + self.children + self.seniors


class BudgetSpec(BaseModel):
    amount: float = Field(gt=0)
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    scope: Literal["total", "per_person", "per_day", "per_person_per_day"]
    include_transport: bool = True
    include_hotel: bool = True


class NamedRequirement(BaseModel):
    kind: Literal["place", "activity", "category", "area"] = "place"
    value: str = Field(min_length=1, max_length=120)
    source: ConstraintSource = ConstraintSource.USER_EXPLICIT


class HardConstraint(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    type: str = Field(min_length=1, max_length=80)
    operator: Literal["eq", "neq", "lt", "lte", "gt", "gte", "in", "not_in"] = "eq"
    value: Any
    unit: Optional[str] = Field(default=None, max_length=32)
    scope: Optional[str] = Field(default=None, max_length=80)
    source: ConstraintSource = ConstraintSource.USER_EXPLICIT

    @model_validator(mode="after")
    def memory_cannot_be_hard(self) -> "HardConstraint":
        if self.source == ConstraintSource.MEMORY:
            raise ValueError("memory preferences cannot become hard constraints without confirmation")
        return self


class SoftPreference(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    type: str = Field(min_length=1, max_length=80)
    value: Any = True
    weight: float = Field(default=0.5, ge=0, le=1)
    source: ConstraintSource = ConstraintSource.INFERRED


class TripTaskSpec(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    room_id: str = Field(min_length=1, max_length=160)
    task_revision: int = Field(default=1, ge=1)
    city: str = Field(default="", max_length=80)
    date_range: DateRange = Field(default_factory=DateRange)
    travelers: Travelers = Field(default_factory=Travelers)
    budget: Optional[BudgetSpec] = None
    must_include: list[NamedRequirement] = Field(default_factory=list)
    exclude: list[NamedRequirement] = Field(default_factory=list)
    hard_constraints: list[HardConstraint] = Field(default_factory=list)
    soft_preferences: list[SoftPreference] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_contract(self) -> "TripTaskSpec":
        if self.travelers.total <= 0:
            raise ValueError("at least one traveler is required")

        explicit = {
            _normalise_requirement(item.value)
            for item in self.must_include
            if item.source in {ConstraintSource.USER_EXPLICIT, ConstraintSource.ROOM_CONSENSUS}
        }
        excluded = {_normalise_requirement(item.value) for item in self.exclude}
        overlap = sorted(explicit & excluded)
        for item in overlap:
            conflict = f"must_include conflicts with exclude: {item}"
            if conflict not in self.conflicts:
                self.conflicts.append(conflict)

        if not self.city and "city" not in self.missing_fields:
            self.missing_fields.append("city")
        if self.date_range.days <= 0 and "date_range.days" not in self.missing_fields:
            self.missing_fields.append("date_range.days")
        return self

    @property
    def needs_clarification(self) -> bool:
        return bool(self.missing_fields or self.conflicts)


class TaskParseResult(BaseModel):
    task_spec: TripTaskSpec
    needs_clarification: bool
    clarification_fields: list[str] = Field(default_factory=list)
    clarification_message: Optional[str] = None


def _normalise_requirement(value: str) -> str:
    return "".join(value.lower().split())
