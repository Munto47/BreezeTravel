from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class MemoryRecord(BaseModel):
    id: UUID
    content: str
    category: str
    confidence: float
    source_message_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    expires_at: Optional[datetime] = None


class MemoryUpdateRequest(BaseModel):
    content: Optional[str] = Field(default=None, min_length=1, max_length=300)
    category: Optional[str] = Field(default=None, min_length=1, max_length=40)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    expires_at: Optional[datetime] = None

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else None


class MemorySettingsRequest(BaseModel):
    enabled: bool


class MemorySettingsResponse(BaseModel):
    enabled: bool
    updated_at: Optional[datetime] = None
