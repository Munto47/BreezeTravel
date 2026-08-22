from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.itineraries.errors import ItineraryDomainError
from app.itineraries.repositories import ItineraryRepository, PostgresItineraryRepository
from app.operations.http import require_idempotency_key
from app.services.room_access import require_room_member
from app.trip_check.briefs import PostgresTripBriefRepository, TripBriefRepository
from app.trip_check.models import RunSpec, TripCheckRun
from app.trip_check.runs import (
    PostgresTripCheckRunRepository,
    TripCheckRunRepository,
    TripCheckRunService,
)
from app.utils.auth import get_current_user


router = APIRouter()


def get_itinerary_repository() -> ItineraryRepository:
    return PostgresItineraryRepository()


def get_trip_brief_repository() -> TripBriefRepository:
    return PostgresTripBriefRepository()


def get_trip_check_run_repository() -> TripCheckRunRepository:
    return PostgresTripCheckRunRepository()


ItineraryRepositoryDep = Annotated[ItineraryRepository, Depends(get_itinerary_repository)]
TripBriefRepositoryDep = Annotated[TripBriefRepository, Depends(get_trip_brief_repository)]
TripCheckRunRepositoryDep = Annotated[TripCheckRunRepository, Depends(get_trip_check_run_repository)]
CurrentUserDep = Annotated[str, Depends(get_current_user)]


class CreateTripCheckRunRequest(BaseModel):
    itinerary_revision: int = Field(gt=0)
    brief_revision: int = Field(gt=0)
    run_spec: RunSpec


class ResumeTripCheckRunRequest(BaseModel):
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _parse_if_match(raw: str | None) -> int:
    if raw is None or not raw.strip():
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail={"code": "IF_MATCH_REQUIRED", "message": "If-Match header is required"},
        )
    value = raw.strip().removeprefix("W/").strip().strip('"')
    try:
        version = int(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match must contain a run version integer"},
        ) from exc
    if version <= 0:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match run version must be positive"},
        )
    return version


def _set_headers(response: Response, run: TripCheckRun, *, replayed: bool = False) -> None:
    response.headers["ETag"] = f'"{run.version}"'
    response.headers["Cache-Control"] = "no-store"
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"


def _raise_domain(exc: ItineraryDomainError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


async def _authorize_workspace(workspace_id: str, user_id: str, repository: ItineraryRepository):
    workspace = await repository.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    await require_room_member(workspace.room_id, user_id)
    return workspace


async def _run_with_access(
    run_id: str,
    user_id: str,
    itinerary_repository: ItineraryRepository,
    run_repository: TripCheckRunRepository,
) -> TripCheckRun:
    run = await run_repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    await _authorize_workspace(run.workspace_id, user_id, itinerary_repository)
    return run


@router.post(
    "/trip-workspaces/{workspace_id}/trip-check-runs",
    response_model=TripCheckRun,
    status_code=status.HTTP_201_CREATED,
)
async def create_trip_check_run(
    workspace_id: str,
    body: CreateTripCheckRunRequest,
    response: Response,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    trip_brief_repository: TripBriefRepositoryDep,
    run_repository: TripCheckRunRepositoryDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _authorize_workspace(workspace_id, current_user, itinerary_repository)
    try:
        run, replayed = await TripCheckRunService(
            run_repository=run_repository,
            itinerary_repository=itinerary_repository,
            brief_repository=trip_brief_repository,
        ).create(
            workspace_id=workspace_id,
            itinerary_revision=body.itinerary_revision,
            brief_revision=body.brief_revision,
            run_spec=body.run_spec,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
        )
    except ItineraryDomainError as exc:
        _raise_domain(exc)
    _set_headers(response, run, replayed=replayed)
    return run


@router.get("/trip-check-runs/{run_id}", response_model=TripCheckRun)
async def get_trip_check_run(
    run_id: str,
    response: Response,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    run_repository: TripCheckRunRepositoryDep,
):
    run = await _run_with_access(run_id, current_user, itinerary_repository, run_repository)
    _set_headers(response, run)
    return run


@router.get("/trip-check-runs/{run_id}/events")
async def get_trip_check_run_events(
    run_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    run_repository: TripCheckRunRepositoryDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
):
    await _run_with_access(run_id, current_user, itinerary_repository, run_repository)
    try:
        after = int(last_event_id or 0)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_LAST_EVENT_ID", "message": "Last-Event-ID must be an integer"},
        ) from exc
    events = await run_repository.list_events(run_id, after_event_id=max(0, after))

    async def stream():
        for event in events:
            payload = event.model_dump(mode="json")
            yield (
                f"id: {event.event_id}\n"
                f"event: {event.event_type}\n"
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/trip-check-runs/{run_id}/resume", response_model=TripCheckRun)
async def resume_trip_check_run(
    run_id: str,
    body: ResumeTripCheckRunRequest,
    response: Response,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    trip_brief_repository: TripBriefRepositoryDep,
    run_repository: TripCheckRunRepositoryDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _run_with_access(run_id, current_user, itinerary_repository, run_repository)
    try:
        run, replayed = await TripCheckRunService(
            run_repository=run_repository,
            itinerary_repository=itinerary_repository,
            brief_repository=trip_brief_repository,
        ).resume(
            run_id=run_id,
            expected_version=_parse_if_match(if_match),
            config_hash=body.config_hash,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
        )
    except ItineraryDomainError as exc:
        _raise_domain(exc)
    _set_headers(response, run, replayed=replayed)
    return run
