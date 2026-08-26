from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.itineraries.repositories import ItineraryRepository, PostgresItineraryRepository
from app.services.room_access import require_room_member
from app.trip_check.advice import AdviceRepository, PostgresAdviceRepository
from app.trip_check.models import AdviceBundle
from app.utils.auth import get_current_user


router = APIRouter()


def get_itinerary_repository() -> ItineraryRepository:
    return PostgresItineraryRepository()


def get_advice_repository() -> AdviceRepository:
    return PostgresAdviceRepository()


ItineraryRepositoryDep = Annotated[ItineraryRepository, Depends(get_itinerary_repository)]
AdviceRepositoryDep = Annotated[AdviceRepository, Depends(get_advice_repository)]
CurrentUserDep = Annotated[str, Depends(get_current_user)]


@router.get(
    "/trip-workspaces/{workspace_id}/reports/{report_id}/advice",
    response_model=AdviceBundle,
)
async def get_report_advice(
    workspace_id: str,
    report_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    advice_repository: AdviceRepositoryDep,
):
    workspace = await itinerary_repository.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    await require_room_member(workspace.room_id, current_user)
    bundle = await advice_repository.get_bundle_for_report(workspace_id, report_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    return bundle
