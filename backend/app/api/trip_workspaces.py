from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.audit.repositories import AuditRepository, PostgresAuditRepository
from app.itineraries.command_service import RevisionCommandService
from app.itineraries.incremental import IncrementalWorkspaceEditService
from app.itineraries.route_refresh import (
    AmapRouteEvidenceProvider,
    ChangedRouteEdgeRefreshService,
    RouteEdgeRefreshResult,
    RouteEvidenceProvider,
)
from app.itineraries.errors import ItineraryDomainError
from app.itineraries.models import (
    EditOperation,
    ItineraryEditCommand,
    ItineraryPatchResult,
    ItineraryRevision,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.map_projection import MapProjection, build_map_projection
from app.itineraries.repositories import ItineraryRepository, PostgresItineraryRepository
from app.itineraries.resume_models import WorkspaceResume
from app.itineraries.resume_repository import (
    PostgresWorkspaceResumeRepository,
    WorkspaceResumeNotFound,
    WorkspaceResumeRepository,
    WorkspaceStateInconsistent,
)
from app.itineraries.revision_service import RevisionService
from app.members.repositories import MemberConstraintRepository, PostgresMemberConstraintRepository
from app.operations.http import require_idempotency_key
from app.operations.repositories import CreationCommandRepository, PostgresCreationCommandRepository
from app.audit.service import AuditApplicationService
from app.schemas.itinerary import Itinerary
from app.services.room_access import require_room_member
from app.suggestions.repositories import PostgresSuggestionRepository, SuggestionRepository
from app.suggestions.service import AtomicSuggestionUndoService
from app.utils.auth import get_current_user


router = APIRouter()


def get_itinerary_repository() -> ItineraryRepository:
    return PostgresItineraryRepository()


def get_audit_repository() -> AuditRepository:
    return PostgresAuditRepository()


def get_member_constraint_repository() -> MemberConstraintRepository:
    return PostgresMemberConstraintRepository()


def get_workspace_resume_repository() -> WorkspaceResumeRepository:
    return PostgresWorkspaceResumeRepository()


def get_creation_command_repository() -> CreationCommandRepository:
    return PostgresCreationCommandRepository()


def get_route_evidence_provider() -> RouteEvidenceProvider:
    return AmapRouteEvidenceProvider()


def get_suggestion_repository() -> SuggestionRepository:
    return PostgresSuggestionRepository()


RepositoryDep = Annotated[ItineraryRepository, Depends(get_itinerary_repository)]
AuditRepositoryDep = Annotated[AuditRepository, Depends(get_audit_repository)]
MemberConstraintRepositoryDep = Annotated[
    MemberConstraintRepository,
    Depends(get_member_constraint_repository),
]
ResumeRepositoryDep = Annotated[WorkspaceResumeRepository, Depends(get_workspace_resume_repository)]
CreationCommandRepositoryDep = Annotated[
    CreationCommandRepository, Depends(get_creation_command_repository),
]
RouteEvidenceProviderDep = Annotated[RouteEvidenceProvider, Depends(get_route_evidence_provider)]
SuggestionRepositoryDep = Annotated[SuggestionRepository, Depends(get_suggestion_repository)]
CurrentUserDep = Annotated[str, Depends(get_current_user)]


class CreateWorkspaceRequest(BaseModel):
    workspace_id: str | None = None
    room_id: str = Field(min_length=1)
    city: str
    trip_date_range: TripDateRange
    initial_itinerary: Itinerary | None = None


class EditCommandRequest(BaseModel):
    command_id: str = Field(min_length=1)
    base_revision: int = Field(gt=0)
    operation: EditOperation
    payload: dict[str, Any] = Field(default_factory=dict)
    client_timestamp: datetime | None = None


class UndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1)
    base_revision: int = Field(gt=0)
    target_revision: int = Field(gt=0)
    client_timestamp: datetime | None = None


class ConfirmRequest(BaseModel):
    command_id: str = Field(min_length=1)
    base_revision: int = Field(gt=0)
    client_timestamp: datetime | None = None


class WorkspaceSnapshot(BaseModel):
    """Single read model used by reconnecting stateless clients."""

    workspace: TripWorkspace
    current_revision: ItineraryRevision | None = None


def _domain_http_error(exc: ItineraryDomainError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail())


def _parse_if_match(raw: str | None) -> int:
    if raw is None or not raw.strip():
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail={"code": "IF_MATCH_REQUIRED", "message": "If-Match header is required"},
        )
    candidate = raw.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:].strip()
    candidate = candidate.strip('"')
    try:
        value = int(candidate)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match must contain a revision integer"},
        ) from exc
    if value <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match revision must be positive"},
        )
    return value


def _if_none_match_matches(raw: str | None, etag: str) -> bool:
    if raw is None:
        return False
    target = etag[2:] if etag.startswith("W/") else etag
    for candidate in raw.split(","):
        normalized = candidate.strip()
        if normalized == "*":
            return True
        if normalized.startswith("W/"):
            normalized = normalized[2:].strip()
        if normalized == target:
            return True
    return False


async def _workspace_with_access(
    workspace_id: str,
    user_id: str,
    repository: ItineraryRepository,
) -> TripWorkspace:
    workspace = await repository.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "workspace does not exist"},
        )
    await require_room_member(workspace.room_id, user_id)
    return workspace


@router.post("/trip-workspaces", response_model=TripWorkspace, status_code=status.HTTP_201_CREATED)
async def create_trip_workspace(
    body: CreateWorkspaceRequest,
    current_user: CurrentUserDep,
    repository: RepositoryDep,
):
    await require_room_member(body.room_id, current_user)
    try:
        return await RevisionService(repository).create_workspace(
            room_id=body.room_id,
            city=body.city,
            date_range=body.trip_date_range,
            created_by=current_user,
            workspace_id=body.workspace_id,
            initial_legacy_itinerary=body.initial_itinerary,
        )
    except ItineraryDomainError as exc:
        raise _domain_http_error(exc) from exc


@router.get("/trip-workspaces/{workspace_id}", response_model=TripWorkspace)
async def get_trip_workspace(workspace_id: str, current_user: CurrentUserDep, repository: RepositoryDep):
    return await _workspace_with_access(workspace_id, current_user, repository)


@router.get("/trip-workspaces/{workspace_id}/snapshot", response_model=WorkspaceSnapshot)
async def get_trip_workspace_snapshot(
    workspace_id: str,
    response: Response,
    current_user: CurrentUserDep,
    repository: RepositoryDep,
):
    """Return the resume-critical workspace and canonical revision atomically enough for readback.

    The revision number is also exposed as an ETag so a mobile client can use the
    exact value in its next conditional mutation without interpreting timestamps.
    """
    workspace = await _workspace_with_access(workspace_id, current_user, repository)
    revision = None
    if workspace.current_itinerary_revision is not None:
        revision = await repository.get_revision(workspace_id, workspace.current_itinerary_revision)
        if revision is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "WORKSPACE_REVISION_INCONSISTENT",
                    "message": "workspace points to a missing itinerary revision",
                },
            )
        response.headers["ETag"] = f'"{revision.revision}"'
    response.headers["Cache-Control"] = "no-store"
    return WorkspaceSnapshot(workspace=workspace, current_revision=revision)


@router.get("/trip-workspaces/{workspace_id}/resume", response_model=WorkspaceResume)
async def resume_trip_workspace(
    workspace_id: str,
    current_user: CurrentUserDep,
    repository: ResumeRepositoryDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
):
    try:
        resumed = await repository.get_resume(workspace_id, current_user)
    except WorkspaceResumeNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "workspace does not exist"},
        ) from exc
    except WorkspaceStateInconsistent as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    etag = resumed.strong_etag()
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if _if_none_match_matches(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(
        content=resumed.model_dump_json(by_alias=True),
        media_type="application/json",
        headers=headers,
    )


@router.get("/trip-workspaces/{workspace_id}/revisions", response_model=list[ItineraryRevision])
async def list_trip_revisions(workspace_id: str, current_user: CurrentUserDep, repository: RepositoryDep):
    await _workspace_with_access(workspace_id, current_user, repository)
    return await repository.list_revisions(workspace_id)


@router.get("/trip-workspaces/{workspace_id}/revisions/{revision}", response_model=ItineraryRevision)
async def get_trip_revision(
    workspace_id: str,
    revision: int,
    current_user: CurrentUserDep,
    repository: RepositoryDep,
):
    await _workspace_with_access(workspace_id, current_user, repository)
    result = await repository.get_revision(workspace_id, revision)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "itinerary revision does not exist"},
        )
    return result


@router.get(
    "/trip-workspaces/{workspace_id}/revisions/{revision}/map-projection",
    response_model=MapProjection,
)
async def get_trip_revision_map_projection(
    workspace_id: str,
    revision: int,
    current_user: CurrentUserDep,
    repository: RepositoryDep,
):
    """Expose only authoritative, explicit coordinates for the exact revision.

    This deliberately has no provider/geocoder fallback.  A missing projection
    is an explicit unavailable state rather than a plausible-looking marker.
    """
    await _workspace_with_access(workspace_id, current_user, repository)
    current = await repository.get_revision(workspace_id, revision)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "itinerary revision does not exist"},
        )
    lineage = [current]
    seen = {current.revision}
    parent = current.parent_revision
    while parent is not None and parent not in seen:
        ancestor = await repository.get_revision(workspace_id, parent)
        if ancestor is None:
            # A broken ancestry must not be replaced with an inferred point.
            break
        lineage.append(ancestor)
        seen.add(ancestor.revision)
        parent = ancestor.parent_revision
    return build_map_projection(current, lineage=lineage)


@router.post(
    "/trip-workspaces/{workspace_id}/revisions/{revision}/changed-route-edges/refresh",
    response_model=RouteEdgeRefreshResult,
)
async def refresh_changed_route_edges(
    workspace_id: str,
    revision: int,
    current_user: CurrentUserDep,
    repository: RepositoryDep,
    audit_repository: AuditRepositoryDep,
    member_constraint_repository: MemberConstraintRepositoryDep,
    command_repository: CreationCommandRepositoryDep,
    route_evidence_provider: RouteEvidenceProviderDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    """Refresh only this current revision's changed route edges.

    The response is a new immutable evidence/report bundle.  It is not a
    mutation of either the parent revision or its earlier evidence snapshot.
    Missing canonical coordinates and disabled/degraded providers are returned
    as explicit unavailable evidence rather than inferred travel times.
    """
    await _workspace_with_access(workspace_id, current_user, repository)
    try:
        result, replayed = await ChangedRouteEdgeRefreshService(
            itinerary_repository=repository,
            audit_repository=audit_repository,
            audit_service=AuditApplicationService(
                itinerary_repository=repository,
                audit_repository=audit_repository,
                member_constraint_repository=member_constraint_repository,
            ),
            provider=route_evidence_provider,
        ).run_idempotent(
            workspace_id=workspace_id,
            itinerary_revision=revision,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
            command_repository=command_repository,
        )
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return result
    except ItineraryDomainError as exc:
        raise _domain_http_error(exc) from exc


async def _execute(
    *,
    workspace_id: str,
    body: EditCommandRequest,
    if_match: str | None,
    idempotency_key: str | None,
    current_user: str,
    repository: ItineraryRepository,
    audit_repository: AuditRepository | None = None,
    member_constraint_repository: MemberConstraintRepository | None = None,
) -> ItineraryPatchResult:
    await _workspace_with_access(workspace_id, current_user, repository)
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key header is required"},
        )
    match_revision = _parse_if_match(if_match)
    if match_revision != body.base_revision:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "IF_MATCH_BODY_MISMATCH",
                "message": "If-Match and base_revision must match",
            },
        )
    command = ItineraryEditCommand(
        command_id=body.command_id,
        workspace_id=workspace_id,
        base_revision=body.base_revision,
        actor_user_id=current_user,
        operation=body.operation,
        payload=body.payload,
        client_timestamp=body.client_timestamp,
    )
    try:
        service = (
            RevisionCommandService(repository, audit_repository=audit_repository)
            if command.operation is EditOperation.CONFIRM
            else IncrementalWorkspaceEditService(
                repository,
                audit_repository=audit_repository,
                member_constraint_repository=member_constraint_repository,
            )
        )
        return await service.apply(
            command,
            if_match_revision=match_revision,
            idempotency_key=idempotency_key,
        )
    except ItineraryDomainError as exc:
        raise _domain_http_error(exc) from exc


@router.post("/trip-workspaces/{workspace_id}/edits", response_model=ItineraryPatchResult)
async def edit_trip_workspace(
    workspace_id: str,
    body: EditCommandRequest,
    current_user: CurrentUserDep,
    repository: RepositoryDep,
    audit_repository: AuditRepositoryDep,
    member_constraint_repository: MemberConstraintRepositoryDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    result = await _execute(
        workspace_id=workspace_id,
        body=body,
        if_match=if_match,
        idempotency_key=idempotency_key,
        current_user=current_user,
        repository=repository,
        audit_repository=audit_repository,
        member_constraint_repository=member_constraint_repository,
    )
    if result.new_revision is not None:
        response.headers["ETag"] = f'"{result.new_revision}"'
    return result


@router.post("/trip-workspaces/{workspace_id}/undo", response_model=ItineraryPatchResult)
async def undo_trip_workspace(
    workspace_id: str,
    body: UndoRequest,
    current_user: CurrentUserDep,
    repository: RepositoryDep,
    audit_repository: AuditRepositoryDep,
    member_constraint_repository: MemberConstraintRepositoryDep,
    suggestion_repository: SuggestionRepositoryDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _workspace_with_access(workspace_id, current_user, repository)
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key header is required"},
        )
    match_revision = _parse_if_match(if_match)
    if match_revision != body.base_revision:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "IF_MATCH_BODY_MISMATCH", "message": "If-Match and base_revision must match"},
        )
    command = ItineraryEditCommand(
        command_id=body.command_id,
        workspace_id=workspace_id,
        base_revision=body.base_revision,
        actor_user_id=current_user,
        operation=EditOperation.UNDO,
        payload={"target_revision": body.target_revision},
        client_timestamp=body.client_timestamp,
    )
    try:
        result = await AtomicSuggestionUndoService(suggestion_repository).apply_if_accepted_suggestion(
            command,
            if_match_revision=match_revision,
            idempotency_key=idempotency_key,
        )
        if result is None:
            result = await IncrementalWorkspaceEditService(
                repository,
                audit_repository=audit_repository,
                member_constraint_repository=member_constraint_repository,
            ).apply(
                command,
                if_match_revision=match_revision,
                idempotency_key=idempotency_key,
            )
        else:
            result = await IncrementalWorkspaceEditService(
                repository,
                audit_repository=audit_repository,
                member_constraint_repository=member_constraint_repository,
            ).enrich_committed_result(command, result)
    except ItineraryDomainError as exc:
        raise _domain_http_error(exc) from exc
    if result.new_revision is not None:
        response.headers["ETag"] = f'"{result.new_revision}"'
    if result.idempotent_replay:
        response.headers["Idempotency-Replayed"] = "true"
    return result


@router.post("/trip-workspaces/{workspace_id}/confirm", response_model=ItineraryPatchResult)
async def confirm_trip_workspace(
    workspace_id: str,
    body: ConfirmRequest,
    response: Response,
    current_user: CurrentUserDep,
    repository: RepositoryDep,
    audit_repository: AuditRepositoryDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    result = await _execute(
        workspace_id=workspace_id,
        body=EditCommandRequest(
            command_id=body.command_id,
            base_revision=body.base_revision,
            operation=EditOperation.CONFIRM,
            client_timestamp=body.client_timestamp,
        ),
        if_match=if_match,
        idempotency_key=idempotency_key,
        current_user=current_user,
        repository=repository,
        audit_repository=audit_repository,
    )
    if result.new_revision is not None:
        response.headers["ETag"] = f'"{result.new_revision}"'
    return result
