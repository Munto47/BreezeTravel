from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.itineraries.errors import ItineraryDomainError
from app.itineraries.repositories import ItineraryRepository, PostgresItineraryRepository
from app.operations.http import require_idempotency_key
from app.services.room_access import require_room_member
from app.trip_check.briefs import PostgresTripBriefRepository, TripBriefApplicationService, TripBriefRepository
from app.trip_check.models import TripBriefRevision
from app.utils.auth import get_current_user


router = APIRouter()


def get_itinerary_repository() -> ItineraryRepository:
    return PostgresItineraryRepository()


def get_trip_brief_repository() -> TripBriefRepository:
    return PostgresTripBriefRepository()


ItineraryRepositoryDep = Annotated[ItineraryRepository, Depends(get_itinerary_repository)]
TripBriefRepositoryDep = Annotated[TripBriefRepository, Depends(get_trip_brief_repository)]
CurrentUserDep = Annotated[str, Depends(get_current_user)]


class PatchTripBriefRequest(BaseModel):
    updates: dict[str, Any] = Field(min_length=1, max_length=14)


def _parse_if_match(raw: str | None) -> int:
    if raw is None or not raw.strip():
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail={"code": "IF_MATCH_REQUIRED", "message": "If-Match header is required"},
        )
    value = raw.strip().removeprefix("W/").strip().strip('"')
    try:
        revision = int(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match must contain a brief revision integer"},
        ) from exc
    if revision <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match brief revision must be positive"},
        )
    return revision


def _set_headers(response: Response, brief: TripBriefRevision, *, replayed: bool = False) -> None:
    response.headers["ETag"] = f'"{brief.revision}"'
    response.headers["Cache-Control"] = "no-store"
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"


def _raise_domain(exc: ItineraryDomainError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


async def _authorize(workspace_id: str, user_id: str, repository: ItineraryRepository):
    workspace = await repository.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    await require_room_member(workspace.room_id, user_id)
    return workspace


@router.get(
    "/trip-workspaces/{workspace_id}/trip-briefs/{revision}",
    response_model=TripBriefRevision,
)
async def get_trip_brief(
    workspace_id: str,
    revision: int,
    response: Response,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    trip_brief_repository: TripBriefRepositoryDep,
):
    await _authorize(workspace_id, current_user, itinerary_repository)
    brief = await trip_brief_repository.get_brief(workspace_id, revision)
    if brief is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    _set_headers(response, brief)
    return brief


@router.patch(
    "/trip-workspaces/{workspace_id}/trip-briefs/{revision}",
    response_model=TripBriefRevision,
)
async def patch_trip_brief(
    workspace_id: str,
    revision: int,
    body: PatchTripBriefRequest,
    response: Response,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    trip_brief_repository: TripBriefRepositoryDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _authorize(workspace_id, current_user, itinerary_repository)
    matched_revision = _parse_if_match(if_match)
    if matched_revision != revision:
        raise HTTPException(
            status_code=400,
            detail={"code": "IF_MATCH_PATH_MISMATCH", "message": "If-Match and path revision must match"},
        )
    try:
        brief, replayed = await TripBriefApplicationService(trip_brief_repository).patch(
            workspace_id=workspace_id,
            revision=revision,
            updates=body.updates,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
        )
    except ItineraryDomainError as exc:
        _raise_domain(exc)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_TRIP_BRIEF_PATCH", "message": str(exc)},
        ) from exc
    _set_headers(response, brief, replayed=replayed)
    return brief


@router.post(
    "/trip-workspaces/{workspace_id}/trip-briefs/{revision}/confirm",
    response_model=TripBriefRevision,
)
async def confirm_trip_brief(
    workspace_id: str,
    revision: int,
    response: Response,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    trip_brief_repository: TripBriefRepositoryDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _authorize(workspace_id, current_user, itinerary_repository)
    matched_revision = _parse_if_match(if_match)
    if matched_revision != revision:
        raise HTTPException(
            status_code=400,
            detail={"code": "IF_MATCH_PATH_MISMATCH", "message": "If-Match and path revision must match"},
        )
    try:
        brief, replayed = await TripBriefApplicationService(trip_brief_repository).confirm(
            workspace_id=workspace_id,
            revision=revision,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
        )
    except ItineraryDomainError as exc:
        _raise_domain(exc)
    _set_headers(response, brief, replayed=replayed)
    return brief
