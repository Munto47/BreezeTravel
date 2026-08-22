from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.itinerary import Itinerary


class FinalTipsArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    itinerary_revision: int = Field(gt=0)
    basis_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    itinerary: Itinerary
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_revision_binding(self) -> "FinalTipsArtifact":
        if self.itinerary.version != self.itinerary_revision:
            raise ValueError("tips itinerary version must match itinerary_revision")
        return self
