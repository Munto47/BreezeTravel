from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.itineraries.adapters import legacy_to_revision
from app.itineraries.models import TripDateRange, TripWorkspace, WorkspaceStatus
from app.itineraries.repositories import ItineraryRepository
from app.schemas.itinerary import Itinerary


class RevisionService:
    def __init__(self, repository: ItineraryRepository):
        self.repository = repository

    async def create_workspace(
        self,
        *,
        room_id: str,
        city: str,
        date_range: TripDateRange,
        created_by: str,
        workspace_id: str | None = None,
        initial_legacy_itinerary: Itinerary | None = None,
    ) -> TripWorkspace:
        now = datetime.now(timezone.utc)
        workspace = TripWorkspace(
            workspace_id=workspace_id or str(uuid4()),
            room_id=room_id,
            city=city,
            trip_date_range=date_range,
            current_itinerary_revision=1 if initial_legacy_itinerary else None,
            status=WorkspaceStatus.DRAFT,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        initial_revision = None
        if initial_legacy_itinerary:
            initial_revision = legacy_to_revision(
                initial_legacy_itinerary,
                workspace_id=workspace.workspace_id,
                date_range=date_range,
                created_by=created_by,
            )
        return await self.repository.create_workspace(workspace, initial_revision)

    async def attach_initial_legacy_itinerary(
        self,
        *,
        workspace: TripWorkspace,
        itinerary: Itinerary,
        created_by: str,
    ) -> TripWorkspace:
        initial = legacy_to_revision(
            itinerary.model_copy(update={"version": 1}),
            workspace_id=workspace.workspace_id,
            date_range=workspace.trip_date_range,
            created_by=created_by,
        )
        return await self.repository.attach_initial_revision(workspace.workspace_id, initial)
