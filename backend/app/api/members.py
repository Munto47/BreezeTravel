"""P7 member constraints and scoped, revocable sharing endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.itineraries.errors import ItineraryDomainError
from app.itineraries.repositories import ItineraryRepository, PostgresItineraryRepository
from app.audit.models import AuditFinding, AuditReport
from app.audit.repositories import AuditRepository, PostgresAuditRepository
from app.members.models import ConstraintConfirmationStatus, ConstraintHardness, ConstraintSource, MemberConstraint, MemberConstraintDraft, MemberConstraintWriteResult, TravelerProfile
from app.members.repositories import MemberConstraintRepository, PostgresMemberConstraintRepository
from app.members.service import MemberConstraintService
from app.members.sharing import IssuedShareLink, PostgresShareLinkRepository, ShareLink, ShareLinkRepository, ShareLinkService, ShareLinkUnavailableError, ShareResponse, ShareResponseAction, ShareScope, ShareScopeDeniedError
from app.services.room_access import require_room_member
from app.utils.auth import get_current_user, get_optional_user

router = APIRouter()


def get_itinerary_repository() -> ItineraryRepository:
    return PostgresItineraryRepository()


def get_member_constraint_repository() -> MemberConstraintRepository:
    return PostgresMemberConstraintRepository()


def get_share_link_repository() -> ShareLinkRepository:
    return PostgresShareLinkRepository()


def get_audit_repository() -> AuditRepository:
    return PostgresAuditRepository()


async def get_room_member_ids(room_id: str) -> list[str]:
    """List actual room members so an absent response remains visibly pending."""
    from app.db.connection import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM room_members WHERE room_id = $1 ORDER BY user_id", room_id)
    return [row["user_id"] for row in rows]


ItineraryRepositoryDep = Annotated[ItineraryRepository, Depends(get_itinerary_repository)]
MemberRepositoryDep = Annotated[MemberConstraintRepository, Depends(get_member_constraint_repository)]
ShareRepositoryDep = Annotated[ShareLinkRepository, Depends(get_share_link_repository)]
AuditRepositoryDep = Annotated[AuditRepository, Depends(get_audit_repository)]
CurrentUserDep = Annotated[str, Depends(get_current_user)]
OptionalUserDep = Annotated[str | None, Depends(get_optional_user)]


class MemberConstraintWriteRequest(BaseModel):
    expected_base_revision: int = Field(ge=0)
    profile: TravelerProfile | None = None
    constraint: MemberConstraintDraft


class MemberView(BaseModel):
    member_id: str
    profile: TravelerProfile | None = None
    constraints: list[MemberConstraint] = Field(default_factory=list)
    confirmed_itinerary_revision: int | None = Field(default=None, ge=1)


class ShareLinkCreateRequest(BaseModel):
    scopes: set[ShareScope] = Field(default_factory=lambda: {ShareScope.REPORT_READ})
    recipient_member_id: str | None = Field(default=None, min_length=1)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_scopes(self) -> "ShareLinkCreateRequest":
        if not self.scopes:
            raise ValueError("at least one share scope is required")
        if self.scopes & {ShareScope.CONSTRAINT_WRITE, ShareScope.ACKNOWLEDGE} and not self.recipient_member_id:
            raise ValueError("input share scopes require recipient_member_id")
        if self.scopes & {ShareScope.CONSTRAINT_WRITE, ShareScope.ACKNOWLEDGE} and ShareScope.REPORT_READ not in self.scopes:
            raise ValueError("input share scopes require REPORT_READ")
        return self


class SharedStopView(BaseModel):
    """The immutable itinerary fields a recipient needs, without workspace metadata."""

    stop_id: str
    place_id: str
    day_index: int
    order_index: int
    start_time: str | None = None
    end_time: str | None = None
    visit_duration_minutes: int | None = None
    raw_name: str | None = None
    fixed_commitment: bool
    locked: bool
    category: str
    notes: str


class SharedDayView(BaseModel):
    day_index: int
    date: str | None = None
    stops: list[SharedStopView] = Field(default_factory=list)


class SharedRevisionView(BaseModel):
    revision: int
    content_hash: str
    city: str
    trip_start_date: str
    trip_end_date: str
    days: list[SharedDayView]


class SharedFindingView(BaseModel):
    """Finding display intentionally excludes internal input/evidence/member IDs."""

    finding_id: str
    rule_id: str
    status: str
    severity: str
    reason_code: str
    message: str
    affected_days: list[int]
    affected_stop_ids: list[str]
    repairable: bool
    confirmation_action: str | None = None


class SharedReportView(BaseModel):
    report_id: str
    itinerary_revision: int
    audit_rule_set_version: str
    overall_status: str
    created_at: datetime
    findings: list[SharedFindingView] = Field(default_factory=list)


class SharedAcknowledgementView(BaseModel):
    required: bool
    acknowledged: bool
    acknowledged_at: datetime | None = None


class SharedConstraintWriteContext(BaseModel):
    """Recipient-only optimistic-concurrency token for a constrained write."""

    expected_base_revision: int = Field(ge=0)


class SharedWorkspaceView(BaseModel):
    """A redacted, captured share projection; never a workspace capability."""

    itinerary: SharedRevisionView
    report: SharedReportView | None = None
    scopes: set[ShareScope]
    recipient_bound: bool
    acknowledgement: SharedAcknowledgementView
    constraint_write_context: SharedConstraintWriteContext | None = None


class ShareResponseRequest(BaseModel):
    action: ShareResponseAction
    expected_base_revision: int | None = Field(default=None, ge=0)
    profile: TravelerProfile | None = None
    constraint: MemberConstraintDraft | None = None

    @model_validator(mode="after")
    def validate_action_payload(self) -> "ShareResponseRequest":
        if self.action == ShareResponseAction.CONSTRAINT:
            if self.constraint is None or self.expected_base_revision is None:
                raise ValueError("CONSTRAINT response requires constraint and expected_base_revision")
        elif self.constraint is not None or self.profile is not None or self.expected_base_revision is not None:
            raise ValueError("ACKNOWLEDGE response cannot modify member constraints")
        return self


async def _workspace_with_access(workspace_id: str, user_id: str, repository: ItineraryRepository):
    workspace = await repository.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    return workspace, await require_room_member(workspace.room_id, user_id)


def _domain_error(exc: ItineraryDomainError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail())


def _share_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ShareLinkUnavailableError):
        return HTTPException(status_code=404, detail={"code": "SHARE_LINK_UNAVAILABLE"})
    return HTTPException(status_code=403, detail={"code": "RESOURCE_SCOPE_DENIED"})


def _assert_member_self(actor: str, member_id: str, draft: MemberConstraintDraft) -> None:
    if actor != member_id or draft.owner_member_id != member_id:
        raise HTTPException(status_code=403, detail={"code": "RESOURCE_SCOPE_DENIED"})
    # Organizers coordinate, but cannot silently overwrite another member's HARD input.
    if draft.source != ConstraintSource.MEMBER_EXPLICIT:
        raise HTTPException(status_code=422, detail={"code": "MEMBER_CONSTRAINT_SOURCE_REQUIRED"})
    if draft.hardness == ConstraintHardness.HARD and draft.confirmation_status != ConstraintConfirmationStatus.CONFIRMED:
        raise HTTPException(status_code=422, detail={"code": "MEMBER_HARD_CONSTRAINT_UNCONFIRMED"})


def _profile_for_member(profile: TravelerProfile | None, workspace_id: str, member_id: str) -> TravelerProfile | None:
    if profile is not None and (profile.workspace_id != workspace_id or profile.member_id != member_id):
        raise HTTPException(status_code=403, detail={"code": "RESOURCE_SCOPE_DENIED"})
    return profile


def _assert_share_recipient(link: ShareLink, current_user: str | None) -> None:
    """A named recipient is an identity binding, never only a UI hint."""

    if link.recipient_member_id is not None and current_user != link.recipient_member_id:
        raise HTTPException(status_code=403, detail={"code": "RESOURCE_SCOPE_DENIED"})


def _shared_revision(revision) -> SharedRevisionView:
    return SharedRevisionView(
        revision=revision.revision,
        content_hash=revision.content_hash,
        city=revision.city,
        trip_start_date=revision.date_range.start.isoformat(),
        trip_end_date=revision.date_range.end.isoformat(),
        days=[
            SharedDayView(
                day_index=day.day_index,
                date=day.date.isoformat() if day.date else None,
                stops=[SharedStopView(
                    stop_id=stop.stop_id,
                    place_id=stop.place_id,
                    day_index=stop.day_index,
                    order_index=stop.order_index,
                    start_time=stop.start_time,
                    end_time=stop.end_time,
                    visit_duration_minutes=stop.visit_duration_minutes,
                    raw_name=stop.raw_name,
                    fixed_commitment=stop.fixed_commitment,
                    locked=stop.locked,
                    category=stop.category,
                    notes=stop.notes,
                ) for stop in day.stops],
            ) for day in revision.days
        ],
    )


def _shared_finding(finding: AuditFinding) -> SharedFindingView:
    return SharedFindingView(
        finding_id=finding.finding_id,
        rule_id=finding.rule_id,
        status=finding.status.value,
        severity=finding.severity.value,
        reason_code=finding.reason_code,
        message=finding.message,
        affected_days=finding.affected_days,
        affected_stop_ids=finding.affected_stop_ids,
        repairable=finding.repairable,
        confirmation_action=finding.confirmation_action,
    )


def _shared_report(report: AuditReport) -> SharedReportView:
    return SharedReportView(
        report_id=report.report_id,
        itinerary_revision=report.itinerary_revision,
        audit_rule_set_version=report.audit_rule_set_version,
        overall_status=report.overall_status.value,
        created_at=report.created_at,
        findings=[_shared_finding(finding) for finding in report.findings],
    )


@router.get("/trip-workspaces/{workspace_id}/members", response_model=list[MemberView])
async def list_members(workspace_id: str, current_user: CurrentUserDep, itinerary_repository: ItineraryRepositoryDep, member_repository: MemberRepositoryDep, share_repository: ShareRepositoryDep):
    workspace, _ = await _workspace_with_access(workspace_id, current_user, itinerary_repository)
    constraints = await member_repository.list_effective_constraints(workspace_id, workspace.current_member_constraint_revision or 0) if workspace.current_member_constraint_revision else []
    profiles = await member_repository.list_profiles(workspace_id)
    confirmations: dict[str, int] = {}
    for response in await share_repository.list_responses(workspace_id):
        if response.action == ShareResponseAction.ACKNOWLEDGE:
            confirmations[response.member_id] = max(confirmations.get(response.member_id, 0), response.itinerary_revision)
    profile_by_member = {item.member_id: item for item in profiles}
    member_ids = set(await get_room_member_ids(workspace.room_id)) | set(profile_by_member) | {item.owner_member_id for item in constraints} | set(confirmations)
    return [MemberView(member_id=member_id, profile=profile_by_member.get(member_id), constraints=[item for item in constraints if item.owner_member_id == member_id], confirmed_itinerary_revision=confirmations.get(member_id)) for member_id in sorted(member_ids)]


@router.put("/trip-workspaces/{workspace_id}/members/{member_id}/constraints", response_model=MemberConstraintWriteResult)
async def write_member_constraint(workspace_id: str, member_id: str, body: MemberConstraintWriteRequest, current_user: CurrentUserDep, itinerary_repository: ItineraryRepositoryDep, member_repository: MemberRepositoryDep):
    await _workspace_with_access(workspace_id, current_user, itinerary_repository)
    _assert_member_self(current_user, member_id, body.constraint)
    profile = _profile_for_member(body.profile, workspace_id, member_id)
    service = MemberConstraintService(member_repository)
    try:
        if profile is not None:
            await service.save_profile(profile)
        return await service.write_constraint(workspace_id, body.constraint, expected_base_revision=body.expected_base_revision)
    except ItineraryDomainError as exc:
        raise _domain_error(exc) from exc


@router.post("/trip-workspaces/{workspace_id}/share-links", response_model=IssuedShareLink, status_code=status.HTTP_201_CREATED)
async def create_share_link(workspace_id: str, body: ShareLinkCreateRequest, current_user: CurrentUserDep, itinerary_repository: ItineraryRepositoryDep, share_repository: ShareRepositoryDep):
    workspace, _ = await _workspace_with_access(workspace_id, current_user, itinerary_repository)
    if workspace.current_itinerary_revision is None:
        raise HTTPException(status_code=409, detail={"code": "WORKSPACE_REVISION_REQUIRED"})
    # The enum/migration preserve forward compatibility, but there is no token
    # edit endpoint.  Issuing this scope now would create a misleading bearer
    # capability, so reject it for every role until that endpoint exists.
    if ShareScope.WORKSPACE_EDIT in body.scopes:
        raise HTTPException(status_code=422, detail={"code": "UNSUPPORTED_SHARE_SCOPE"})
    if body.recipient_member_id:
        await require_room_member(workspace.room_id, body.recipient_member_id)
    now = datetime.now(timezone.utc)
    expires_at = body.expires_at or now + timedelta(days=7)
    if expires_at.tzinfo is None or expires_at <= now or expires_at > now + timedelta(days=30):
        raise HTTPException(status_code=422, detail={"code": "INVALID_SHARE_EXPIRY"})
    return await ShareLinkService(share_repository).issue(workspace_id=workspace_id, itinerary_revision=workspace.current_itinerary_revision, report_id=workspace.current_report_id, scopes=body.scopes, recipient_member_id=body.recipient_member_id, created_by=current_user, expires_at=expires_at)


@router.delete("/trip-workspaces/{workspace_id}/share-links/{share_link_id}", response_model=ShareLink)
async def revoke_share_link(workspace_id: str, share_link_id: str, current_user: CurrentUserDep, itinerary_repository: ItineraryRepositoryDep, share_repository: ShareRepositoryDep):
    _, access = await _workspace_with_access(workspace_id, current_user, itinerary_repository)
    target = next((item for item in await share_repository.list_links(workspace_id) if item.share_link_id == share_link_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    if target.created_by != current_user and access.role != "owner":
        raise HTTPException(status_code=403, detail={"code": "RESOURCE_SCOPE_DENIED"})
    return await ShareLinkService(share_repository).revoke(workspace_id, share_link_id) or target


@router.get("/share/{token}", response_model=SharedWorkspaceView)
async def read_shared_workspace(
    token: str,
    itinerary_repository: ItineraryRepositoryDep,
    audit_repository: AuditRepositoryDep,
    share_repository: ShareRepositoryDep,
    current_user: OptionalUserDep,
):
    try:
        link = await ShareLinkService(share_repository).resolve(token, required_scope=ShareScope.REPORT_READ)
    except (ShareLinkUnavailableError, ShareScopeDeniedError) as exc:
        raise _share_error(exc) from exc
    _assert_share_recipient(link, current_user)
    revision = await itinerary_repository.get_revision(link.workspace_id, link.itinerary_revision)
    if revision is None:
        raise HTTPException(status_code=409, detail={"code": "SHARE_LINK_REVISION_INCONSISTENT"})
    report = None
    if link.report_id is not None:
        report = await audit_repository.get_report(link.report_id)
        if report is None or report.workspace_id != link.workspace_id or report.itinerary_revision != link.itinerary_revision:
            raise HTTPException(status_code=409, detail={"code": "SHARE_LINK_REVISION_INCONSISTENT"})
    acknowledgements = [
        response for response in await share_repository.list_responses(link.workspace_id)
        if response.share_link_id == link.share_link_id and response.action == ShareResponseAction.ACKNOWLEDGE
    ]
    latest_acknowledgement = max(acknowledgements, key=lambda response: response.created_at, default=None)
    constraint_write_context = None
    if ShareScope.CONSTRAINT_WRITE in link.scopes:
        # Recipient identity was verified above.  This is only the append
        # revision needed for an optimistic constrained write, not a workspace
        # read or a workspace-edit capability.
        workspace = await itinerary_repository.get_workspace(link.workspace_id)
        if workspace is None:
            raise HTTPException(status_code=409, detail={"code": "SHARE_LINK_REVISION_INCONSISTENT"})
        constraint_write_context = SharedConstraintWriteContext(
            expected_base_revision=workspace.current_member_constraint_revision or 0,
        )
    return SharedWorkspaceView(
        itinerary=_shared_revision(revision),
        report=_shared_report(report) if report else None,
        scopes=link.scopes,
        recipient_bound=link.recipient_member_id is not None,
        acknowledgement=SharedAcknowledgementView(
            required=ShareScope.ACKNOWLEDGE in link.scopes,
            acknowledged=latest_acknowledgement is not None,
            acknowledged_at=latest_acknowledgement.created_at if latest_acknowledgement else None,
        ),
        constraint_write_context=constraint_write_context,
    )


@router.post("/share/{token}/responses", response_model=ShareResponse, status_code=status.HTTP_201_CREATED)
async def respond_to_share_link(token: str, body: ShareResponseRequest, current_user: CurrentUserDep, member_repository: MemberRepositoryDep, share_repository: ShareRepositoryDep):
    required_scope = ShareScope.ACKNOWLEDGE if body.action == ShareResponseAction.ACKNOWLEDGE else ShareScope.CONSTRAINT_WRITE
    service = ShareLinkService(share_repository)
    try:
        link = await service.resolve(token, required_scope=required_scope)
    except (ShareLinkUnavailableError, ShareScopeDeniedError) as exc:
        raise _share_error(exc) from exc
    if link.recipient_member_id is None:
        raise HTTPException(status_code=403, detail={"code": "RESOURCE_SCOPE_DENIED"})
    _assert_share_recipient(link, current_user)
    constraint_revision = None
    if body.action == ShareResponseAction.CONSTRAINT:
        assert body.constraint is not None and body.expected_base_revision is not None
        _assert_member_self(link.recipient_member_id, link.recipient_member_id, body.constraint)
        profile = _profile_for_member(body.profile, link.workspace_id, link.recipient_member_id)
        member_service = MemberConstraintService(member_repository)
        try:
            if profile is not None:
                await member_service.save_profile(profile)
            result = await member_service.write_constraint(link.workspace_id, body.constraint, expected_base_revision=body.expected_base_revision)
        except ItineraryDomainError as exc:
            raise _domain_error(exc) from exc
        constraint_revision = result.current_workspace_revision
    return await service.record_response(link, action=body.action, member_constraint_revision=constraint_revision)
