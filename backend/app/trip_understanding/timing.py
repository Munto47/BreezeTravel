"""Structured, same-day activity timing shared by inference and user edits."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActivityTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    visit_duration_minutes: int | None = Field(default=None, ge=0, le=1440)
    timing_source: Literal["TEXT", "USER", "SUGGESTED", "UNSPECIFIED"] = "UNSPECIFIED"
    locked: bool = False
    fixed_commitment: bool = False

    @model_validator(mode="after")
    def same_day_window(self) -> "ActivityTiming":
        if self.start_time and self.end_time and self.end_time < self.start_time:
            raise ValueError("end_time must follow start_time on the same day")
        return self


TIMING_FIELDS = tuple(ActivityTiming.model_fields)


def timing_values(value: ActivityTiming) -> dict:
    return {name: getattr(value, name) for name in TIMING_FIELDS}


def clock_minutes(value: str | None) -> int | None:
    if not value:
        return None
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def shift_clock(value: str | None, minutes: int) -> str | None:
    if value is None:
        return None
    shifted = clock_minutes(value) + minutes
    if not 0 <= shifted < 1440:
        raise ValueError("the proposed shift crosses a day boundary")
    return f"{shifted // 60:02d}:{shifted % 60:02d}"
