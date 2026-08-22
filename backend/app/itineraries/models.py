from __future__ import annotations

from datetime import date as Date
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


SUPPORTED_CITIES = frozenset({"北京", "上海", "杭州"})


class WorkspaceStatus(str, Enum):
    DRAFT = "DRAFT"
    AUDITING = "AUDITING"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    CONFIRMED = "CONFIRMED"


class RevisionSource(str, Enum):
    IMPORT = "IMPORT"
    TEMPLATE = "TEMPLATE"
    MANUAL = "MANUAL"
    REPAIR = "REPAIR"
    PLANNER = "PLANNER"


class ResolutionStatus(str, Enum):
    AUTO_MATCHED = "AUTO_MATCHED"
    USER_CONFIRMED = "USER_CONFIRMED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


class CommitmentKind(str, Enum):
    """Semantic role of a time-bound stop imported from an itinerary."""

    ARRIVAL = "ARRIVAL"
    FIXED_VISIT = "FIXED_VISIT"
    RETURN_DEPARTURE = "RETURN_DEPARTURE"


class EditOperation(str, Enum):
    ADD_STOP = "ADD_STOP"
    MOVE_STOP = "MOVE_STOP"
    MOVE_TO_DAY = "MOVE_TO_DAY"
    REORDER_STOP = "REORDER_STOP"
    ADJUST_TIME = "ADJUST_TIME"
    REPLACE_STOP = "REPLACE_STOP"
    REMOVE_STOP = "REMOVE_STOP"
    LOCK_STOP = "LOCK_STOP"
    UNLOCK_STOP = "UNLOCK_STOP"
    APPLY_REPAIR = "APPLY_REPAIR"
    UNDO = "UNDO"
    CONFIRM = "CONFIRM"


class TripDateRange(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: Date
    end: Date

    @model_validator(mode="after")
    def validate_scope(self) -> "TripDateRange":
        days = (self.end - self.start).days + 1
        if days < 2 or days > 5:
            raise ValueError("trip date range must contain 2 to 5 days")
        return self


class RevisionTransport(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: str = "driving"
    duration_minutes: int | None = Field(default=None, ge=0)
    distance_meters: int | None = Field(default=None, ge=0)


class ItineraryStop(BaseModel):
    model_config = ConfigDict(frozen=True)

    stop_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    day_index: int = Field(ge=0, le=4)
    order_index: int = Field(ge=0)
    start_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    visit_duration_minutes: int | None = Field(default=None, ge=0, le=1440)
    transport_to_next: RevisionTransport | None = None
    raw_name: str | None = None
    source_raw_stop_id: str | None = None
    resolution_status: ResolutionStatus = ResolutionStatus.USER_CONFIRMED
    commitment_kind: CommitmentKind | None = None
    fixed_commitment: bool = False
    locked: bool = False
    category: str = "attraction"
    notes: str = ""


class ItineraryDay(BaseModel):
    model_config = ConfigDict(frozen=True)

    day_index: int = Field(ge=0, le=4)
    date: Date | None = None
    stops: list[ItineraryStop] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_stop_order(self) -> "ItineraryDay":
        stop_ids = [stop.stop_id for stop in self.stops]
        if len(stop_ids) != len(set(stop_ids)):
            raise ValueError("stop_id must be unique within a day")
        expected_order = list(range(len(self.stops)))
        if [stop.order_index for stop in self.stops] != expected_order:
            raise ValueError("stop order_index must be contiguous and ordered")
        if any(stop.day_index != self.day_index for stop in self.stops):
            raise ValueError("stop day_index must match its containing day")
        return self


class ItineraryRevisionContent(BaseModel):
    model_config = ConfigDict(frozen=True)

    itinerary_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    revision: int = Field(gt=0)
    parent_revision: int | None = Field(default=None, ge=1)
    source_type: RevisionSource
    city: str
    date_range: TripDateRange
    days: list[ItineraryDay]
    locked_commitments: list[str] = Field(default_factory=list)
    change_summary: dict[str, Any] = Field(default_factory=dict)
    created_by: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_revision(self) -> "ItineraryRevisionContent":
        if self.city not in SUPPORTED_CITIES:
            raise ValueError("CITY_NOT_SUPPORTED")
        if self.parent_revision is not None and self.parent_revision >= self.revision:
            raise ValueError("parent revision must be older than revision")
        if len(self.days) < 2 or len(self.days) > 5:
            raise ValueError("itinerary must contain 2 to 5 days")
        if [day.day_index for day in self.days] != list(range(len(self.days))):
            raise ValueError("day_index must be contiguous and ordered")
        all_stop_ids = [stop.stop_id for day in self.days for stop in day.stops]
        if len(all_stop_ids) != len(set(all_stop_ids)):
            raise ValueError("stop_id must be unique within an itinerary revision")
        return self


class ItineraryRevision(ItineraryRevisionContent):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class TripWorkspace(BaseModel):
    workspace_id: str = Field(min_length=1)
    room_id: str = Field(min_length=1)
    city: str
    trip_date_range: TripDateRange
    current_itinerary_revision: int | None = Field(default=None, ge=1)
    current_task_spec_revision: int | None = Field(default=None, ge=1)
    current_member_constraint_revision: int | None = Field(default=None, ge=1)
    current_report_id: str | None = None
    current_import_id: str | None = None
    status: WorkspaceStatus = WorkspaceStatus.DRAFT
    created_by: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_city(self) -> "TripWorkspace":
        if self.city not in SUPPORTED_CITIES:
            raise ValueError("CITY_NOT_SUPPORTED")
        return self


class ItineraryEditCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    base_revision: int = Field(gt=0)
    actor_user_id: str = Field(min_length=1)
    operation: EditOperation
    payload: dict[str, Any] = Field(default_factory=dict)
    client_timestamp: datetime | None = None


class RevisionConflict(BaseModel):
    code: str = "ITINERARY_REVISION_CONFLICT"
    expected_revision: int
    actual_revision: int | None


class ItineraryPatchResult(BaseModel):
    accepted: bool
    command_id: str
    new_revision: int | None = None
    changed_days: list[int] = Field(default_factory=list)
    changed_route_edges: list[str] = Field(default_factory=list)
    route_delta: dict[str, Any] | None = None
    incremental_findings: list[dict[str, Any]] = Field(default_factory=list)
    affected_rule_ids: list[str] = Field(default_factory=list)
    audit_mode: str = "NONE"
    llm_calls: int = Field(default=0, ge=0)
    report_stale: bool = True
    conflict: RevisionConflict | None = None
    idempotent_replay: bool = False
