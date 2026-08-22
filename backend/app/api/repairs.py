from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.audit.repositories import AuditRepository, PostgresAuditRepository
from app.itineraries.errors import ItineraryDomainError
from app.itineraries.repositories import ItineraryRepository, PostgresItineraryRepository
from app.itineraries.route_refresh import AmapRouteEvidenceProvider, RouteEvidenceProvider
from app.repairs.models import RepairApplyResult, RepairOption
from app.repairs.repositories import PostgresRepairRepository, RepairRepository
from app.repairs.search import BoundedRepairSearch, ProviderRepairRouteEvidenceRefresher
from app.operations.http import require_idempotency_key
from app.operations.repositories import (
    CreationCommandRepository,
    PostgresCreationCommandRepository,
)
from app.services.room_access import require_room_member
from app.utils.auth import get_current_user


router = APIRouter()


def get_itinerary_repository() -> ItineraryRepository:
    return PostgresItineraryRepository()


def get_audit_repository() -> AuditRepository:
    return PostgresAuditRepository()


def get_repair_repository() -> RepairRepository:
    return PostgresRepairRepository()


def get_creation_command_repository() -> CreationCommandRepository:
    return PostgresCreationCommandRepository()


def get_route_evidence_provider() -> RouteEvidenceProvider:
    return AmapRouteEvidenceProvider()


ItineraryRepositoryDep = Annotated[ItineraryRepository, Depends(get_itinerary_repository)]
AuditRepositoryDep = Annotated[AuditRepository, Depends(get_audit_repository)]
RepairRepositoryDep = Annotated[RepairRepository, Depends(get_repair_repository)]
CurrentUserDep = Annotated[str, Depends(get_current_user)]
CreationCommandRepositoryDep = Annotated[
    CreationCommandRepository,
    Depends(get_creation_command_repository),
]
RouteEvidenceProviderDep = Annotated[RouteEvidenceProvider, Depends(get_route_evidence_provider)]


class ApplyRepairRequest(BaseModel):
    base_revision: int = Field(gt=0)


class RejectRepairRequest(BaseModel):
    reason: str = Field(max_length=500)


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
        revision = int(candidate)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match must contain a revision integer"},
        ) from exc
    if revision <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match revision must be positive"},
        )
    return revision


def _raise_domain(exc: ItineraryDomainError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


async def _report_with_access(
    audit_id: str,
    user_id: str,
    itinerary_repository: ItineraryRepository,
    audit_repository: AuditRepository,
):
    report = await audit_repository.get_report(audit_id)
    if report is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    workspace = await itinerary_repository.get_workspace(report.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    await require_room_member(workspace.room_id, user_id)
    return report


async def _option_with_access(
    audit_id: str,
    repair_id: str,
    user_id: str,
    itinerary_repository: ItineraryRepository,
    audit_repository: AuditRepository,
    repair_repository: RepairRepository,
) -> RepairOption:
    await _report_with_access(audit_id, user_id, itinerary_repository, audit_repository)
    option = await repair_repository.get_option(repair_id)
    if option is None or option.source_report_id != audit_id:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    return option


@router.post("/audits/{audit_id}/repairs", response_model=list[RepairOption], status_code=status.HTTP_201_CREATED)
async def propose_repairs(
    audit_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
    repair_repository: RepairRepositoryDep,
    command_repository: CreationCommandRepositoryDep,
    route_evidence_provider: RouteEvidenceProviderDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _report_with_access(audit_id, current_user, itinerary_repository, audit_repository)
    try:
        options, replayed = await BoundedRepairSearch(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
            repair_repository=repair_repository,
            route_refresher=ProviderRepairRouteEvidenceRefresher(route_evidence_provider),
        ).propose_idempotent(
            audit_id,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
            command_repository=command_repository,
        )
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return options
    except ItineraryDomainError as exc:
        _raise_domain(exc)


@router.get("/audits/{audit_id}/repairs", response_model=list[RepairOption])
async def list_repairs(
    audit_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
    repair_repository: RepairRepositoryDep,
):
    await _report_with_access(audit_id, current_user, itinerary_repository, audit_repository)
    return await repair_repository.list_options(audit_id)


@router.get("/audits/{audit_id}/repairs/{repair_id}", response_model=RepairOption)
async def get_repair(
    audit_id: str,
    repair_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
    repair_repository: RepairRepositoryDep,
):
    return await _option_with_access(
        audit_id,
        repair_id,
        current_user,
        itinerary_repository,
        audit_repository,
        repair_repository,
    )


@router.post("/audits/{audit_id}/repairs/{repair_id}/apply", response_model=RepairApplyResult)
async def apply_repair(
    audit_id: str,
    repair_id: str,
    body: ApplyRepairRequest,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
    repair_repository: RepairRepositoryDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _option_with_access(
        audit_id,
        repair_id,
        current_user,
        itinerary_repository,
        audit_repository,
        repair_repository,
    )
    revision = _parse_if_match(if_match)
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key header is required"},
        )
    if revision != body.base_revision:
        raise HTTPException(
            status_code=400,
            detail={"code": "IF_MATCH_BODY_MISMATCH", "message": "If-Match and base_revision must match"},
        )
    try:
        result = await repair_repository.apply_option(
            repair_id,
            actor_user_id=current_user,
            if_match_revision=revision,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{result.new_revision}"'
        return result
    except ItineraryDomainError as exc:
        _raise_domain(exc)


@router.post("/audits/{audit_id}/repairs/{repair_id}/reject", response_model=RepairOption)
async def reject_repair(
    audit_id: str,
    repair_id: str,
    body: RejectRepairRequest,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
    repair_repository: RepairRepositoryDep,
):
    await _option_with_access(
        audit_id,
        repair_id,
        current_user,
        itinerary_repository,
        audit_repository,
        repair_repository,
    )
    normalized_reason = body.reason.strip()
    if not normalized_reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "INVALID_REPAIR_REJECTION_REASON",
                "message": "repair rejection reason is required",
            },
        )
    try:
        return await repair_repository.reject_option(
            repair_id,
            actor_user_id=current_user,
            reason=normalized_reason,
        )
    except ItineraryDomainError as exc:
        _raise_domain(exc)
