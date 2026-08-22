from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.audit.models import AuditReport, EvidenceSnapshot
from app.audit.recheck import PreTripRecheckResult, PreTripRecheckService
from app.audit.repositories import AuditRepository, PostgresAuditRepository
from app.audit.service import AuditApplicationService
from app.itineraries.errors import ItineraryDomainError
from app.itineraries.repositories import ItineraryRepository, PostgresItineraryRepository
from app.members.repositories import MemberConstraintRepository, PostgresMemberConstraintRepository
from app.itineraries.tips_models import FinalTipsArtifact
from app.itineraries.tips_repositories import (
    FinalTipsRepository,
    PostgresFinalTipsRepository,
)
from app.itineraries.tips_service import FinalTipsService
from app.operations.http import require_idempotency_key
from app.operations.models import CreationOperation
from app.operations.repositories import (
    CreationCommandRepository,
    PostgresCreationCommandRepository,
)
from app.services.room_access import require_room_member
from app.utils.auth import get_current_user


router = APIRouter()


def get_audit_repository() -> AuditRepository:
    return PostgresAuditRepository()


def get_itinerary_repository() -> ItineraryRepository:
    return PostgresItineraryRepository()


def get_tips_repository() -> FinalTipsRepository:
    return PostgresFinalTipsRepository()


def get_creation_command_repository() -> CreationCommandRepository:
    return PostgresCreationCommandRepository()


def get_member_constraint_repository() -> MemberConstraintRepository:
    return PostgresMemberConstraintRepository()


AuditRepositoryDep = Annotated[AuditRepository, Depends(get_audit_repository)]
ItineraryRepositoryDep = Annotated[ItineraryRepository, Depends(get_itinerary_repository)]
CurrentUserDep = Annotated[str, Depends(get_current_user)]
FinalTipsRepositoryDep = Annotated[FinalTipsRepository, Depends(get_tips_repository)]
CreationCommandRepositoryDep = Annotated[
    CreationCommandRepository,
    Depends(get_creation_command_repository),
]
MemberConstraintRepositoryDep = Annotated[
    MemberConstraintRepository,
    Depends(get_member_constraint_repository),
]


class CreateAuditRequest(BaseModel):
    task_id: str | None = None


class GenerateTipsRequest(BaseModel):
    preferences: str = Field(default="", max_length=1200)


async def _authorize_workspace(
    workspace_id: str,
    user_id: str,
    itinerary_repository: ItineraryRepository,
):
    workspace = await itinerary_repository.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    await require_room_member(workspace.room_id, user_id)
    return workspace


def _raise_domain(exc: ItineraryDomainError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


@router.post("/trip-workspaces/{workspace_id}/audits", response_model=AuditReport)
async def create_audit(
    workspace_id: str,
    body: CreateAuditRequest,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
    member_constraint_repository: MemberConstraintRepositoryDep,
    command_repository: CreationCommandRepositoryDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _authorize_workspace(workspace_id, current_user, itinerary_repository)
    try:
        report, replayed = await AuditApplicationService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
            member_constraint_repository=member_constraint_repository,
        ).run_current_audit_idempotent(
            workspace_id,
            operation=CreationOperation.CREATE_AUDIT,
            target_id=workspace_id,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
            request_body=body.model_dump(mode="json"),
            command_repository=command_repository,
            task_id=body.task_id,
        )
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return report
    except ItineraryDomainError as exc:
        _raise_domain(exc)


async def _report_with_access(
    audit_id: str,
    current_user: str,
    itinerary_repository: ItineraryRepository,
    audit_repository: AuditRepository,
) -> AuditReport:
    report = await audit_repository.get_report(audit_id)
    if report is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    await _authorize_workspace(report.workspace_id, current_user, itinerary_repository)
    return report


@router.get("/audits/{audit_id}", response_model=AuditReport)
async def get_audit(
    audit_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
):
    return await _report_with_access(audit_id, current_user, itinerary_repository, audit_repository)


@router.get("/audits/{audit_id}/evidence", response_model=EvidenceSnapshot)
async def get_audit_evidence(
    audit_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
):
    report = await _report_with_access(audit_id, current_user, itinerary_repository, audit_repository)
    snapshot = await audit_repository.get_snapshot(report.evidence_snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    return snapshot


@router.get("/audits/{audit_id}/events")
async def get_audit_events(
    audit_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
):
    report = await _report_with_access(audit_id, current_user, itinerary_repository, audit_repository)

    async def events():
        payloads = [
            {"event": "evidence_ready", "audit_id": report.report_id, "snapshot_id": report.evidence_snapshot_id},
            {"event": "rules_complete", "audit_id": report.report_id, "finding_count": len(report.findings)},
            {"event": "done", "audit_id": report.report_id, "overall_status": report.overall_status.value},
        ]
        for payload in payloads:
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/audits/{audit_id}/refresh", response_model=AuditReport)
async def refresh_audit(
    audit_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
    member_constraint_repository: MemberConstraintRepositoryDep,
    command_repository: CreationCommandRepositoryDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    report = await _report_with_access(audit_id, current_user, itinerary_repository, audit_repository)
    try:
        refreshed, replayed = await AuditApplicationService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
            member_constraint_repository=member_constraint_repository,
        ).run_current_audit_idempotent(
            report.workspace_id,
            operation=CreationOperation.REFRESH_AUDIT,
            target_id=report.report_id,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
            request_body={},
            command_repository=command_repository,
            task_id=report.task_id,
        )
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return refreshed
    except ItineraryDomainError as exc:
        _raise_domain(exc)


@router.post("/audits/{audit_id}/pre-trip-recheck", response_model=PreTripRecheckResult)
async def pre_trip_recheck(
    audit_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
    member_constraint_repository: MemberConstraintRepositoryDep,
    command_repository: CreationCommandRepositoryDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    """Create a local P8 recheck bundle and explain it against this report.

    This endpoint has no public-release semantics.  It persists a new immutable
    snapshot/report even when one evidence provider degrades, and returns the
    exact old/new difference for the future P8 UI.
    """
    await _report_with_access(audit_id, current_user, itinerary_repository, audit_repository)
    try:
        result, replayed = await PreTripRecheckService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
            audit_service=AuditApplicationService(
                itinerary_repository=itinerary_repository,
                audit_repository=audit_repository,
                member_constraint_repository=member_constraint_repository,
            ),
        ).run_idempotent(
            source_report_id=audit_id,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
            command_repository=command_repository,
        )
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return result
    except ItineraryDomainError as exc:
        _raise_domain(exc)


@router.get("/audits/{audit_id}/pre-trip-recheck-result", response_model=PreTripRecheckResult)
async def get_pre_trip_recheck_result(
    audit_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
):
    """Read a completed P8 diff back from immutable local records."""
    await _report_with_access(audit_id, current_user, itinerary_repository, audit_repository)
    try:
        return await PreTripRecheckService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
        ).read_persisted_result(recheck_report_id=audit_id)
    except ItineraryDomainError as exc:
        _raise_domain(exc)


@router.get("/audits/{audit_id}/tips", response_model=FinalTipsArtifact)
async def get_final_tips(
    audit_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
    tips_repository: FinalTipsRepositoryDep,
):
    await _report_with_access(audit_id, current_user, itinerary_repository, audit_repository)
    artifact = await tips_repository.get_by_report(audit_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    return artifact


@router.post("/audits/{audit_id}/tips", response_model=FinalTipsArtifact)
async def generate_final_tips(
    audit_id: str,
    body: GenerateTipsRequest,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
    tips_repository: FinalTipsRepositoryDep,
    command_repository: CreationCommandRepositoryDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _report_with_access(audit_id, current_user, itinerary_repository, audit_repository)
    try:
        artifact, replayed = await FinalTipsService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
            tips_repository=tips_repository,
        ).generate_for_report_idempotent(
            audit_id,
            preferences=body.preferences,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
            command_repository=command_repository,
        )
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return artifact
    except ItineraryDomainError as exc:
        _raise_domain(exc)
