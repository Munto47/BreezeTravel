"""Auditable retrieval contracts shared by nodes, SSE, and evaluators."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.place import RetrievalExecutionMode


class RetrievalAudit(BaseModel):
    slot_id: Optional[str] = None
    query: str
    city: str
    district: Optional[str] = None
    location: Optional[str] = None
    radius_m: Optional[int] = None
    anchor_place: Optional[str] = None
    anchor_place_id: Optional[str] = None
    anchor_location: Optional[str] = None
    anchor_response_hash: Optional[str] = None
    anchor_observed_at: Optional[datetime] = None
    typecodes: list[str] = Field(default_factory=list)
    provider: str
    execution_mode: RetrievalExecutionMode
    retrieved_at: datetime
    response_hash: Optional[str] = None
    result_count: int = Field(ge=0)
    fallback_reason: Optional[str] = None
    status: str = Field(description="ok | empty | error | blocked | configuration_error")
    attempted: bool = True
    error_category: Optional[str] = None
    provider_health_failure: bool = False
