from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.audit.models import AuditReport, EvidenceSnapshot
from app.importing.models import ItineraryImport
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import ItineraryRevision, TripWorkspace
from app.itineraries.tips_models import FinalTipsArtifact
from app.repairs.models import RepairOption
from app.trip_check.models import AdviceBundle, TripBriefRevision, TripCheckRun


class TipsState(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INELIGIBLE = "INELIGIBLE"
    NOT_GENERATED = "NOT_GENERATED"
    READY = "READY"


class WorkspaceWriteETags(BaseModel):
    """Independent compare-and-set tokens for the two mutable write domains."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    itinerary: str | None = None
    import_: str | None = Field(default=None, alias="import")


class WorkspaceResume(BaseModel):
    """Complete, bounded state required to resume the P1-P4 mobile workflow."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = "1.0"
    workspace: TripWorkspace
    current_revision: ItineraryRevision | None = None
    current_import: ItineraryImport | None = None
    current_brief: TripBriefRevision | None = None
    current_trip_check_run: TripCheckRun | None = None
    current_advice: AdviceBundle | None = None
    current_report: AuditReport | None = None
    current_evidence: EvidenceSnapshot | None = None
    proposed_repairs: list[RepairOption] = Field(default_factory=list)
    applied_repair: RepairOption | None = None
    current_tips: FinalTipsArtifact | None = None
    tips_state: TipsState
    write_etags: WorkspaceWriteETags

    def strong_etag(self) -> str:
        digest = sha256_canonical(self.model_dump(mode="json", by_alias=True))
        return f'"{digest}"'
