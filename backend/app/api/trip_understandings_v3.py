from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.trip_understanding.anonymous import AnonymousDailyLimitError
from app.trip_understanding.failures import (
    INPUT_CAPACITY_EXCEEDED, INPUT_DAY_CAPACITY_EXCEEDED, public_failure_message,
)
from app.trip_understanding.candidates import CandidateSearchRequest, PendingLodgingCandidateRequest, CandidateSearchView, issue_candidate, search_candidates
from app.trip_understanding.lodging_recovery import confirmed_single_destination, recovery_binding, result_cards, validate_recovery_target
from app.trip_understanding.dining import (
    DiningSearchRequest, DiningCandidatesView, DiningCandidateView, dining_binding, search_dining, valid_anchor,
    SourceMealSearchRequest, SourceMealCandidatesView, source_meal_context, source_meal_binding, bind_dining_access,
)
from app.trip_understanding.daily_dining import DailyDiningView
from app.trip_understanding.dining_jobs import read_daily_dining
from app.trip_understanding.relative_route_previews import PublicRelativeRouteOptions
from app.trip_understanding.capability import capability_hash, mint_capability
from app.trip_understanding.errors import (
    CapabilityExpiredError,
    CommandTargetChangedError,
    ConcurrentJobLimitError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    ResourceAccessDeniedError,
    ResourceGoneError,
    ResourceNotReadyError,
    ResourceNotFoundError,
    RevisionConflictError,
    ScreenshotBatchAlreadyUsedError,
    ScreenshotBatchExpiredError,
    ScreenshotBatchNotFoundError,
    ScreenshotBatchNotReadyError,
    ScreenshotBatchUnusableError,
)
from app.trip_understanding.map_render import MapRenderAcceptedView, MapRenderView
from app.trip_understanding.models import (
    AccountTravelDataDeleteRequest,
    ChangeAdoptRequest,
    ChangePreviewRequest,
    ClaimedTripView,
    CreateOutcome,
    CommandAppliedView,
    CreateTripUnderstandingRequest,
    MaterializedTripView,
    PublicChangeAdopted,
    PublicChangePreview,
    PublicTripChecksView,
    StaySelectionAppliedView,
    StaySelectionRequest,
    StaySuggestionView,
    TripUnderstandingAcceptedView,
    TripUnderstandingCancelView,
    TripUnderstandingProgressMetrics,
    TripUnderstandingCommand,
    TripUnderstandingProgressView,
    TravelDataDeletionStatusView,
    UserFacingTripResult,
)
from app.trip_understanding.repository import (
    PostgresTripUnderstandingRepository,
    TripUnderstandingRepository,
)
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.supplement_jobs import SupplementRequest, SupplementStateView, SupplementRejectedError
from app.trip_understanding.errors import SourceUnavailableError
from app.trip_understanding.collaboration_import import (
    CollaborationImportReplay,
    CollaborationRouteUnavailableError,
    load_collaboration_import,
)
from app.trip_understanding.readback import (
    AccountTripListView, InvalidTripCursor, SourceReadView, SupplementaryView,
)
from app.utils.auth import get_current_user, get_optional_user, get_recent_user


router = APIRouter(prefix="/v3/trip-understandings")
account_router = APIRouter(prefix="/v3/me")
logger = logging.getLogger(__name__)


class ServerSentEventResponse(StreamingResponse):
    media_type = "text/event-stream"


_REQUIRED_IDEMPOTENCY_HEADER = {
    "parameters": [
        {
            "name": "Idempotency-Key",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "minLength": 1, "maxLength": 200},
        }
    ]
}


def get_trip_understanding_repository() -> TripUnderstandingRepository:
    return PostgresTripUnderstandingRepository()


RepositoryDep = Annotated[
    TripUnderstandingRepository,
    Depends(get_trip_understanding_repository),
]


def get_place_candidate_search():
    return search_candidates


def get_dining_candidate_search():
    return search_dining


async def get_relative_route_provider():
    settings = get_settings()
    if settings.trip_understanding_provider_mode != "live" or not settings.amap_api_key:
        yield None
        return
    from app.trip_understanding.amap_route import AmapRouteProvider
    # The adapter doesn't own a long-lived client or expose aclose. This request
    # owns one client, shared across its bounded comparisons and always closed.
    async with httpx.AsyncClient(timeout=6.0) as client:
        yield AmapRouteProvider(api_key=settings.amap_api_key, client=client)


OptionalUserDep = Annotated[str | None, Depends(get_optional_user)]
CurrentUserDep = Annotated[str, Depends(get_current_user)]
RecentUserDep = Annotated[str, Depends(get_recent_user)]


def _settings_signing_key() -> str:
    settings = get_settings()
    return settings.trip_understanding_cookie_signing_key or settings.jwt_secret_key


def _require_idempotency_key(raw: str | None) -> str:
    if raw is None or not raw.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "请重新开始这次体验"},
        )
    value = raw.strip()
    if len(value) > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "请求标识过长，请重新开始"},
        )
    return value


def _require_if_match(raw: str | None) -> str:
    if raw is None or not raw.strip():
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail={"code": "IF_MATCH_REQUIRED", "message": "请先刷新到最新卡片"},
        )
    value = raw.strip()
    if value.startswith("W/") or "," in value or len(value) < 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_IF_MATCH", "message": "版本标识无效，请刷新后重试"},
        )
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if not value.startswith("tu3_") or len(value) > 120:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_IF_MATCH", "message": "版本标识无效，请刷新后重试"},
        )
    return value


def _set_capability_cookie(response: Response, cookie_value: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.trip_understanding_cookie_name,
        value=cookie_value,
        max_age=settings.trip_understanding_demo_ttl_hours * 3600,
        httponly=True,
        secure=settings.runtime_profile == "public",
        samesite="lax",
        path="/api/v3/trip-understandings",
    )


def _clear_capability_cookie(response: Response) -> None:
    response.delete_cookie(
        key=get_settings().trip_understanding_cookie_name,
        path="/api/v3/trip-understandings",
        httponly=True,
        samesite="lax",
    )


def _capability_from_cookie(cookie_value: str | None) -> str | None:
    return capability_hash(cookie_value, _settings_signing_key())


def _resource_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ResourceGoneError):
        return HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "RESOURCE_GONE", "message": "这份行程已不可用"},
        )
    if isinstance(exc, (ResourceNotFoundError, ResourceAccessDeniedError)):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "没有找到这份行程"},
        )
    raise exc


async def _authorize(
    public_resource_id: str,
    *,
    cookie_value: str | None,
    user_id: str | None,
    repository: TripUnderstandingRepository,
):
    try:
        return await TripUnderstandingApplicationService(repository).authorize(
            public_resource_id,
            capability_hash=_capability_from_cookie(cookie_value),
            user_id=user_id,
        )
    except (ResourceGoneError, ResourceNotFoundError, ResourceAccessDeniedError) as exc:
        raise _resource_error(exc) from exc


@router.post(
    "",
    response_model=TripUnderstandingAcceptedView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_trip_understanding(
    body: CreateTripUnderstandingRequest,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    key = _require_idempotency_key(idempotency_key)
    settings = get_settings()
    service = TripUnderstandingApplicationService(
        repository,
        ttl_hours=settings.trip_understanding_demo_ttl_hours,
        full_retention_days=settings.trip_understanding_full_retention_days,
    )
    try:
        cookie_value = None
        if body.mode == "FULL" and current_user is not None:
            outcome = await service.create_full(body, owner_user_id=current_user, idempotency_key=key)
        else:
            cookie_value = request.cookies.get(settings.trip_understanding_cookie_name)
            digest = _capability_from_cookie(cookie_value)
            if digest is None:
                cookie_value, digest = mint_capability(_settings_signing_key())
            async def create_anonymous():
                if body.mode == "FULL":
                    return await service.create_full(body, owner_user_id=None, capability_hash=digest, idempotency_key=key)
                return await service.create_demo(capability_hash=digest, idempotency_key=key)
            try:
                outcome = await create_anonymous()
            except CapabilityExpiredError:
                cookie_value, digest = mint_capability(_settings_signing_key())
                outcome = await create_anonymous()
    except AnonymousDailyLimitError as exc:
        raise HTTPException(status_code=429, detail={"code": "ANONYMOUS_DAILY_LIMIT", "message": "今天已整理三份行程，登录后可以继续"}) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新开始这次体验"},
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "正在处理同一个请求，请稍后查看"},
        ) from exc
    except ConcurrentJobLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "TOO_MANY_ACTIVE_REQUESTS", "message": "已有行程正在整理，请稍后再试"},
        ) from exc
    except ScreenshotBatchNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SCREENSHOT_BATCH_NOT_FOUND", "message": "没有找到这组截图"},
        ) from exc
    except ScreenshotBatchExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "SCREENSHOT_BATCH_EXPIRED", "message": "这组截图已过期，请重新选择"},
        ) from exc
    except ScreenshotBatchAlreadyUsedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "SCREENSHOT_BATCH_ALREADY_USED", "message": "这组截图已用于另一份行程"},
        ) from exc
    except ScreenshotBatchNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "SCREENSHOT_BATCH_NOT_READY", "message": "截图仍在读取，请稍后再试"},
        ) from exc
    except ScreenshotBatchUnusableError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "SCREENSHOT_BATCH_UNUSABLE", "message": "这组截图无法使用，请重新选择"},
        ) from exc
    if cookie_value is not None:
        _set_capability_cookie(response, cookie_value)
    response.headers["Cache-Control"] = "no-store"
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.accepted


class FromCollaborationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_id: str = Field(min_length=1, max_length=128)
    room_route_version: int | None = Field(default=None, ge=1, strict=True)


@router.post(
    "/from-collaboration",
    response_model=TripUnderstandingAcceptedView,
    status_code=status.HTTP_202_ACCEPTED,
    openapi_extra=_REQUIRED_IDEMPOTENCY_HEADER,
)
async def create_trip_understanding_from_collaboration(
    body: FromCollaborationRequest,
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", include_in_schema=False),
    ] = None,
):
    key = _require_idempotency_key(idempotency_key)
    settings = get_settings()
    try:
        source = await load_collaboration_import(
            user_id=current_user,
            room_id=body.room_id,
            idempotency_key=key,
            **({"room_route_version": body.room_route_version} if body.room_route_version is not None else {}),
        )
        if isinstance(source, CollaborationImportReplay):
            outcome = CreateOutcome(accepted=source.accepted, replayed=True)
        else:
            outcome = await TripUnderstandingApplicationService(
                repository,
                ttl_hours=settings.trip_understanding_demo_ttl_hours,
                full_retention_days=settings.trip_understanding_full_retention_days,
            ).create_from_collaboration(source, owner_user_id=current_user)
    except CollaborationRouteUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": exc.code,
                "message": exc.public_message,
            },
        ) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "路线已经变化，请重新转入"},
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "正在转入这条路线，请稍后查看"},
        ) from exc
    except ConcurrentJobLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "TOO_MANY_ACTIVE_REQUESTS", "message": "已有行程正在整理，请稍后再试"},
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Location"] = outcome.accepted.result_url
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.accepted


@router.post("/{public_resource_id}/place-candidates", response_model=CandidateSearchView)
async def find_place_candidates(
    public_resource_id: str, body: CandidateSearchRequest | PendingLodgingCandidateRequest, request: Request,
    response: Response, repository: RepositoryDep, current_user: OptionalUserDep,
    search=Depends(get_place_candidate_search),
):
    resource = await _authorize(public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user, repository=repository)
    stored = await repository.get_result(resource)
    if stored is None:
        raise HTTPException(status_code=409, detail={"code": "NOT_READY", "message": "行程还在整理中"})
    if isinstance(body, PendingLodgingCandidateRequest):
        expected = _require_if_match(request.headers.get("If-Match"))
        if expected != stored.opaque_etag:
            raise HTTPException(status_code=409, detail={"code": "REVISION_CONFLICT", "message": "行程已变化，请刷新后重试"})
        try:
            validate_recovery_target(stored.result, body.pending_token, body.intent)
        except CommandTargetChangedError:
            raise HTTPException(status_code=409, detail={"code": "ACTIVITY_CHANGED", "message": "待确认住宿已调整，请刷新后重试"}) from None
        supplementary = await repository.get_supplementary_view(resource, now=datetime.now(timezone.utc), include_pending_lodgings=True)
        pending = next((item for item in supplementary.pending_lodgings if item.pending_token == body.pending_token), None)
        if supplementary.status != "AVAILABLE" or pending is None:
            raise HTTPException(status_code=409, detail={"code": "SOURCE_UNAVAILABLE", "message": "原文已不可用，无法恢复此住宿"})
        city = body.city or pending.city or confirmed_single_destination(stored.result)
        if not city:
            raise HTTPException(status_code=422, detail={"code": "CITY_REQUIRED", "message": "请先选择酒店所在城市"},
                headers={"Cache-Control": "no-store"})
        category = "住宿"
        binding = recovery_binding(body.pending_token, body.intent)
    else:
        card = next((card for card in result_cards(stored.result) if card.activity_token == body.activity_token), None)
        if card is None:
            raise HTTPException(status_code=409, detail={"code": "ACTIVITY_CHANGED", "message": "卡片已调整，请刷新后重试"})
        city = body.city or card.city or next((item.value.removeprefix("暂按 ") for item in stored.result.assumptions if item.key == "destination"), "")
        category, binding = card.category, body.activity_token
    places = await search(city=city, query=body.query, category_hint=category)
    response.headers["Cache-Control"] = "no-store"
    if places is None:
        return CandidateSearchView(status="UNAVAILABLE")
    if not isinstance(body, PendingLodgingCandidateRequest) and any(place.category == "餐饮" for place in places):
        plan, etag = await repository.get_current_place_plan(resource)
        if etag != stored.opaque_etag:
            raise HTTPException(status_code=409, detail={"code": "REVISION_CONFLICT", "message": "行程已变化，请刷新后重试"})
        places = [bind_dining_access(place, stops=plan.stops, activity_token=body.activity_token, before=True)
            if place.category == "餐饮" else place for place in places]
    now = datetime.now(timezone.utc)
    candidates = [issue_candidate(place, public_resource_id=public_resource_id,
        activity_token=binding, expected_etag=stored.opaque_etag, now=now, expires_at=resource.expires_at) for place in places]
    return CandidateSearchView(status="AVAILABLE" if candidates else "EMPTY", candidates=candidates)


@router.post("/{public_resource_id}/dining-candidates", response_model=DiningCandidatesView)
async def find_dining_candidates(
    public_resource_id: str, body: DiningSearchRequest, request: Request,
    response: Response, repository: RepositoryDep, current_user: OptionalUserDep,
    search=Depends(get_dining_candidate_search),
):
    resource = await _authorize(public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user, repository=repository)
    response.headers["Cache-Control"] = "no-store"
    try:
        plan, etag = await repository.get_current_place_plan(resource)
    except ResourceNotReadyError:
        raise HTTPException(status_code=409, detail={"code": "NOT_READY", "message": "行程还在整理中"}) from None
    response.headers["ETag"] = f'"{etag}"'
    anchor = next((stop for stop in plan.stops if stop.activity_token == body.activity_token), None)
    if not valid_anchor(anchor):
        return DiningCandidatesView(status="NEEDS_CONFIRMATION", message="先确认一个地点，再查找附近餐饮。")
    excluded = {stop.canonical_place_id for stop in plan.stops if stop.day_index == anchor.day_index and stop.canonical_place_id}
    places = await search(anchor=anchor, excluded_ids=excluded)
    if places is None:
        return DiningCandidatesView(status="UNAVAILABLE", message="附近餐饮暂时无法查询，可以稍后重试。")
    now = datetime.now(timezone.utc)
    candidates = [DiningCandidateView(**issue_candidate(place, public_resource_id=public_resource_id,
        activity_token=dining_binding(body.activity_token), expected_etag=etag, now=now).model_dump(),
        reason=f"在{anchor.name}附近；营业情况请到店前确认。") for place in
        [bind_dining_access(place, stops=plan.stops, activity_token=body.activity_token) for place in places[:3]]]
    return DiningCandidatesView(status="AVAILABLE" if candidates else "EMPTY",
        message="附近餐饮" if candidates else "暂未找到合适的附近餐饮，可换一站再看看。", candidates=candidates)


@router.post("/{public_resource_id}/source-meal-candidates", response_model=SourceMealCandidatesView)
async def find_source_meal_candidates(public_resource_id: str, body: SourceMealSearchRequest,
    request: Request, response: Response, repository: RepositoryDep, current_user: OptionalUserDep,
    search=Depends(get_dining_candidate_search)):
    resource = await _authorize(public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user, repository=repository)
    expected = _require_if_match(request.headers.get("If-Match"))
    response.headers["Cache-Control"] = "no-store"
    try:
        plan, etag = await repository.get_current_place_plan(resource)
        if etag != expected:
            raise RevisionConflictError("source meal version changed")
        trip = await repository.load_recommendation_trip_view(resource.understanding_id, plan.plan_ref.revision)
        if trip.etag != expected:
            raise RevisionConflictError("source meal version changed")
        view = source_meal_context(trip.result, trip.plan, body.meal_slot, body.position)
    except ResourceNotReadyError:
        raise HTTPException(status_code=409, detail={"code": "NOT_READY", "message": "行程还在整理中"}) from None
    except RevisionConflictError:
        raise HTTPException(status_code=409, detail={"code": "REVISION_CONFLICT", "message": "行程已调整，请刷新后重新查找。"}) from None
    except CommandTargetChangedError:
        raise HTTPException(status_code=422, detail={"code": "MEAL_TARGET_CHANGED", "message": "这餐的位置已变化，请重新打开餐位。"}) from None
    response.headers["ETag"] = f'"{etag}"'
    if view.status != "AVAILABLE":
        return view
    selected = next(stop for stop in plan.stops if stop.activity_token == view.after_activity_token)
    excluded = {stop.canonical_place_id for stop in plan.stops if stop.day_index == body.meal_slot.day_index and stop.canonical_place_id}
    places = await search(anchor=selected, excluded_ids=excluded, meal_only=view.meal_role != "SNACK", query=body.query.strip())
    if places is None:
        return view.model_copy(update={"status": "UNAVAILABLE", "message": "餐厅暂时无法查询，原文安排已保留，可以稍后重试。"})
    now = datetime.now(timezone.utc)
    view.candidates = [DiningCandidateView(**issue_candidate(place, public_resource_id=public_resource_id,
        activity_token=source_meal_binding(view.after_activity_token, before=view.insert_before,
            meal_slot=body.meal_slot, meal_role=view.meal_role), expected_etag=etag, now=now,
        expires_at=resource.expires_at).model_dump(), reason="按你的手动搜索词找到的附近门店；菜品供应、营业与绕路情况仍需确认。") for place in
        [bind_dining_access(place, stops=plan.stops, activity_token=view.after_activity_token,
            before=view.insert_before) for place in places[:3]]]
    if not view.candidates:
        view.status, view.message = "EMPTY", "附近未找到名称或供应商标签与搜索词对应的门店，可以换一个词；没有改选其他餐厅。"
    return view


@router.get("/{public_resource_id}/daily-dining", response_model=DailyDiningView)
async def get_daily_dining(public_resource_id: str, request: Request, response: Response,
                           repository: RepositoryDep, current_user: OptionalUserDep):
    resource = await _authorize(public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user, repository=repository)
    try:
        view, etag = await read_daily_dining(repository, resource)
    except ResourceNotReadyError:
        raise HTTPException(status_code=409, detail={"code": "NOT_READY", "message": "行程还在整理中"}) from None
    response.headers["ETag"] = f'"{etag}"'
    response.headers["Cache-Control"] = "no-store"
    return view


@router.post("/{public_resource_id}/daily-dining", response_model=DailyDiningView)
async def refresh_daily_dining(public_resource_id: str, request: Request, response: Response,
                               repository: RepositoryDep, current_user: OptionalUserDep):
    resource = await _authorize(public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user, repository=repository)
    expected = _require_if_match(request.headers.get("If-Match"))
    key = request.headers.get("Idempotency-Key", "")
    if not key or len(key) > 200:
        raise HTTPException(status_code=400, detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "请重新更新建议"})
    replay_info = {}
    try:
        view, etag = await read_daily_dining(repository, resource, request_key=key, expected_etag=expected, replay_info=replay_info)
    except IdempotencyConflictError:
        raise HTTPException(status_code=409, detail={"code":"IDEMPOTENCY_CONFLICT", "message":"这次更新请求已用于其他版本，请重新更新"}) from None
    except RevisionConflictError:
        raise HTTPException(status_code=409, detail={"code": "REVISION_CONFLICT", "message": "行程已调整，请刷新后重试"}) from None
    except ResourceNotReadyError:
        raise HTTPException(status_code=409, detail={"code": "NOT_READY", "message": "行程还在整理中"}) from None
    response.headers["ETag"] = f'"{etag}"'
    response.headers["Cache-Control"] = "no-store"
    if replay_info.get("replayed"):
        response.headers["Idempotency-Replayed"] = "true"
    return view


@router.get("/{public_resource_id}/inspector")
async def get_trip_inspector(public_resource_id: str, request: Request, response: Response,
                             repository: RepositoryDep, current_user: OptionalUserDep):
    response.headers['Cache-Control'] = 'no-store'
    resource = await _authorize(public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user, repository=repository)
    stored = await repository.get_result(resource)
    if stored is None:
        raise HTTPException(status_code=409, detail="RESULT_NOT_READY")
    try:
        checks = await repository.get_trip_checks(resource)
    except ResourceNotReadyError:
        checks = None
    current_resource = await _authorize(public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user, repository=repository)
    latest = await repository.get_result(current_resource)
    if latest is None or latest.opaque_etag != stored.opaque_etag:
        raise HTTPException(status_code=409, detail="RESULT_CHANGED")
    from app.trip_understanding.inspector import build_issues
    from app.trip_understanding.change_history import read_changes
    return {"input_version": stored.opaque_etag,
        "issues": build_issues(stored.result, checks, input_version=stored.opaque_etag),
        "changes": await read_changes(repository, resource),
        "checks_updating": checks is None,
        "routes_updating": stored.result.map.status == "PREPARING"}


@router.get("/{public_resource_id}/photo")
async def get_trip_place_photo(public_resource_id: str, activity_token: str, request: Request,
                               repository: RepositoryDep, current_user: OptionalUserDep):
    resource = await _authorize(public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user, repository=repository)
    stored = await repository.get_result(resource)
    card = next((card for card in result_cards(stored.result) if card.activity_token == activity_token), None) if stored else None
    from app.trip_understanding.models import safe_poi_photo_url
    url = safe_poi_photo_url(card.photo_url) if card else None
    if not url:
        raise HTTPException(status_code=404, detail="PHOTO_UNAVAILABLE")
    try:
        # The URL comes only from this owner's validated current POI record.
        # Redirects and arbitrary client-supplied URLs are never fetched.
        async with httpx.AsyncClient(timeout=6, follow_redirects=False) as client:
            async with client.stream('GET', url) as upstream:
                content_type = upstream.headers.get('content-type', '').split(';')[0]
                if upstream.status_code != 200 or content_type not in {'image/jpeg', 'image/png', 'image/webp'}:
                    raise HTTPException(status_code=502, detail="PHOTO_UNAVAILABLE")
                data = bytearray()
                async for chunk in upstream.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 4 * 1024 * 1024:
                        raise HTTPException(status_code=502, detail="PHOTO_TOO_LARGE")
                return Response(bytes(data), media_type=content_type,
                    headers={'Cache-Control': 'private, max-age=300', 'X-Content-Type-Options': 'nosniff'})
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="PHOTO_UNAVAILABLE") from None


@router.get(
    "/{public_resource_id}/result",
    responses={
        200: {"model": UserFacingTripResult},
        202: {"model": TripUnderstandingProgressView},
    },
)
async def get_trip_understanding_result(
    public_resource_id: str,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
):
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    stored = await repository.get_result(resource)
    if stored is None:
        # Completion, cancellation and anonymous-to-account claiming may race
        # the first authorization read. Refresh the aggregate before returning
        # a transient 202 based on a stale current_result_id/state projection.
        resource = await _authorize(
            public_resource_id,
            cookie_value=request.cookies.get(
                get_settings().trip_understanding_cookie_name
            ),
            user_id=current_user,
            repository=repository,
        )
        stored = await repository.get_result(resource)
    response.headers["Cache-Control"] = "no-store"
    if resource.state == "FAILED":
        if resource.failure_category in {INPUT_CAPACITY_EXCEEDED, INPUT_DAY_CAPACITY_EXCEEDED}:
            raise HTTPException(status_code=409, detail={"code": resource.failure_category,
                "message": public_failure_message(resource.failure_category)})
        raise HTTPException(status_code=409, detail={"code": "UNDERSTANDING_FAILED", "message": "这次没有整理完成，可以重新尝试"})
    if resource.state == "CANCELLED":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "UNDERSTANDING_CANCELLED",
                "message": "整理已停止，请返回首页重新开始",
            },
        )
    if stored is None:
        events = await repository.list_events(resource, after_event_id=0)
        latest = events[-1] if events else None
        response.status_code = status.HTTP_202_ACCEPTED
        return TripUnderstandingProgressView(
            message=(latest.payload.message if latest else "正在整理每天行程"),
            phase=(latest.payload.phase or "RECEIVED") if latest else "RECEIVED",
            event_cursor=latest.event_id if latest else 0,
            progress=(
                latest.payload.progress
                if latest
                else TripUnderstandingProgressMetrics()
            ),
            snapshot=(latest.payload.snapshot if latest else None),
        )
    response.headers["ETag"] = f'"{stored.opaque_etag}"'
    try:
        return await repository.project_current_knowledge(
            resource,
            stored.result,
            now=datetime.now(timezone.utc),
        )
    except Exception:
        # Knowledge is optional advice. A retrieval outage must not hide the
        # authoritative cards, places, map state or audit result.
        logger.exception("optional knowledge projection unavailable")
        return stored.result


@router.post(
    "/{public_resource_id}/cancel",
    response_model=TripUnderstandingCancelView,
    openapi_extra=_REQUIRED_IDEMPOTENCY_HEADER,
)
async def cancel_trip_understanding(
    public_resource_id: str,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", include_in_schema=False),
    ] = None,
):
    key = _require_idempotency_key(idempotency_key)
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(
            get_settings().trip_understanding_cookie_name
        ),
        user_id=current_user,
        repository=repository,
    )
    try:
        outcome = await TripUnderstandingApplicationService(
            repository
        ).cancel_understanding(
            resource,
            idempotency_key=key,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENCY_KEY_REUSED",
                "message": "请重新开始这次停止操作",
            },
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REQUEST_IN_PROGRESS",
                "message": "正在停止整理，请稍后查看",
            },
        ) from exc
    except (ResourceGoneError, ResourceNotFoundError, ResourceAccessDeniedError) as exc:
        raise _resource_error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    if outcome.opaque_etag:
        response.headers["ETag"] = f'"{outcome.opaque_etag}"'
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.cancelled


@router.get(
    "/{public_resource_id}/map-renders/latest",
    response_model=MapRenderView,
)
async def get_latest_map_render(
    public_resource_id: str,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
):
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    response.headers["Cache-Control"] = "no-store"
    return await repository.get_map_view(resource)


@router.post(
    "/{public_resource_id}/map-renders",
    response_model=MapRenderAcceptedView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_map_render(
    public_resource_id: str,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    expected_etag = _require_if_match(if_match)
    key = _require_idempotency_key(idempotency_key)
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    try:
        outcome = await TripUnderstandingApplicationService(repository).request_map_render(
            resource,
            expected_etag=expected_etag,
            idempotency_key=key,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REVISION_CONFLICT", "message": "行程已经更新，请刷新后再试"},
        ) from exc
    except ResourceNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CARDS_NOT_READY", "message": "卡片还在整理，请稍后再试"},
        ) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新准备路线"},
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "路线正在准备，请稍后查看"},
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.accepted


@router.get(
    "/{public_resource_id}/stay-suggestions",
    response_model=StaySuggestionView,
)
async def get_stay_suggestions(
    public_resource_id: str,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
):
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    response.headers["Cache-Control"] = "no-store"
    return await repository.get_stay_view(resource)


@router.post(
    "/{public_resource_id}/stay-suggestions", response_model=StaySuggestionView,
)
async def refresh_stay_suggestions(public_resource_id: str, request: Request, response: Response,
    repository: RepositoryDep, current_user: OptionalUserDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    expected, key = _require_if_match(if_match), _require_idempotency_key(idempotency_key)
    resource = await _authorize(public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user, repository=repository)
    try:
        view, etag, replayed = await repository.refresh_stay_suggestions(resource, expected_etag=expected,
            idempotency_key=key, now=datetime.now(timezone.utc))
    except RevisionConflictError:
        raise HTTPException(status_code=409, detail={"code": "REVISION_CONFLICT", "message": "行程已调整，请刷新后再试"}) from None
    except ResourceNotReadyError:
        raise HTTPException(status_code=409, detail={"code": "STAY_NOT_READY", "message": "行程地点尚未准备好"}) from None
    except IdempotencyConflictError:
        raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新更新住宿建议"}) from None
    except IdempotencyInProgressError:
        raise HTTPException(status_code=409, detail={"code": "REQUEST_IN_PROGRESS", "message": "住宿建议正在更新"}) from None
    except (ResourceGoneError, ResourceNotFoundError, ResourceAccessDeniedError) as exc:
        raise _resource_error(exc) from exc
    response.headers["ETag"] = f'"{etag}"'
    response.headers["Cache-Control"] = "no-store"
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return view


@router.post(
    "/{public_resource_id}/stay-selection",
    response_model=StaySelectionAppliedView,
)
async def select_stay(
    public_resource_id: str,
    body: StaySelectionRequest,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    expected_etag = _require_if_match(if_match)
    key = _require_idempotency_key(idempotency_key)
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    try:
        outcome = await TripUnderstandingApplicationService(repository).select_stay(
            resource,
            candidate_token=body.candidate_token,
            expected_etag=expected_etag,
            idempotency_key=key,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REVISION_CONFLICT", "message": "行程已经更新，请刷新后再试"},
        ) from exc
    except ResourceNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "STAY_NOT_READY", "message": "住宿候选已变化，请刷新后重试"},
        ) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新选择住宿"},
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "正在保存住宿，请稍后查看"},
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = f'"{outcome.opaque_etag}"'
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.applied


@router.post(
    "/{public_resource_id}/materialize",
    response_model=MaterializedTripView,
)
async def materialize_trip_understanding(
    public_resource_id: str,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    expected_etag = _require_if_match(if_match)
    key = _require_idempotency_key(idempotency_key)
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    try:
        outcome = await TripUnderstandingApplicationService(
            repository
        ).materialize_trip(
            resource,
            expected_etag=expected_etag,
            idempotency_key=key,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "TRIP_UPDATED", "message": "行程已经更新，请刷新后再试"},
        ) from exc
    except ResourceNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CARDS_NOT_READY", "message": "卡片还在整理，请稍后再试"},
        ) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_CHANGED", "message": "请重新准备这份行程"},
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "行程正在准备，请稍后查看"},
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = f'"{outcome.opaque_etag}"'
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.view


@router.get(
    "/{public_resource_id}/checks",
    response_model=PublicTripChecksView,
)
async def get_trip_understanding_checks(
    public_resource_id: str,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
):
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    try:
        view = await TripUnderstandingApplicationService(repository).get_trip_checks(
            resource
        )
    except ResourceNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CHECKS_NOT_READY", "message": "检查结果正在准备，请稍后查看"},
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    return view


@router.post(
    "/{public_resource_id}/changes/preview",
    response_model=PublicChangePreview | PublicRelativeRouteOptions,
)
async def preview_trip_understanding_change(
    public_resource_id: str,
    body: ChangePreviewRequest,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    route_provider=Depends(get_relative_route_provider),
):
    key = _require_idempotency_key(idempotency_key)
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    try:
        if body.day_index is not None:
            from app.trip_understanding.pipeline import canonical_sha256
            expected = _require_if_match(request.headers.get("If-Match"))
            view, replayed = await repository.preview_relative_routes(resource,
                expected_etag=expected, day_index=body.day_index, idempotency_key=key,
                request_hash=canonical_sha256({"kind":"RELATIVE_ORDER","day":body.day_index,"etag":expected}),
                provider=route_provider)
            response.headers["Cache-Control"] = "no-store"
            response.headers["ETag"] = f'"{expected}"'
            if replayed:
                response.headers["Idempotency-Replayed"] = "true"
            return view
        outcome = await TripUnderstandingApplicationService(repository).preview_trip_change(
            resource,
            check_token=body.check_token,
            idempotency_key=key,
        )
    except (ResourceNotReadyError, RevisionConflictError, CommandTargetChangedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CHECK_CHANGED", "message": "这项检查已经变化，请刷新后再试"},
        ) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_CHANGED", "message": "请重新预览这次调整"},
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "正在准备改动预览，请稍后查看"},
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.preview


@router.post(
    "/{public_resource_id}/changes/adopt",
    response_model=PublicChangeAdopted,
)
async def adopt_trip_understanding_change(
    public_resource_id: str,
    body: ChangeAdoptRequest,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    expected_etag = _require_if_match(if_match)
    key = _require_idempotency_key(idempotency_key)
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    try:
        outcome = await TripUnderstandingApplicationService(
            repository
        ).adopt_trip_change(
            resource,
            change_token=body.change_token,
            expected_etag=expected_etag,
            idempotency_key=key,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "TRIP_UPDATED", "message": "行程已经更新，请刷新后再试"},
        ) from exc
    except (ResourceNotReadyError, CommandTargetChangedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CHANGE_CHANGED", "message": "这次改动已经变化，请重新预览"},
        ) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_CHANGED", "message": "请重新采纳这次调整"},
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "正在保存改动，请稍后查看"},
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = f'"{outcome.opaque_etag}"'
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.adopted


@router.post(
    "/{public_resource_id}/commands",
    response_model=CommandAppliedView,
)
async def apply_trip_understanding_command(
    public_resource_id: str,
    body: TripUnderstandingCommand,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    expected_etag = _require_if_match(if_match)
    key = _require_idempotency_key(idempotency_key)
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    # Retain historical DTOs and stored records, but do not accept new calendar
    # or clock edits through the current relative-order product entry point.
    values = body.model_dump(exclude_unset=True)
    timing_edit = body.command_type in {
        "ACTIVITY_TIME_SET", "ACTIVITY_TIMES_SHIFT", "ACTIVITY_TIMES_APPLY",
    } or (body.command_type == "ASSUMPTION_SET" and values.get("key") == "calendar")
    timing_edit = timing_edit or any(values.get(field) not in (None, "", False, "UNSPECIFIED")
        for field in ("start_time", "end_time", "visit_duration_minutes", "time_hint", "locked", "fixed_commitment"))
    if timing_edit:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "TIMING_EDIT_UNSUPPORTED", "message": "当前行程只安排第几天和地点先后，不设置日期、时刻或游玩时长。"})
    try:
        outcome = await TripUnderstandingApplicationService(repository).apply_command(
            resource,
            body,
            expected_etag=expected_etag,
            idempotency_key=key,
        )
    except (ResourceGoneError, ResourceNotFoundError, ResourceAccessDeniedError) as exc:
        raise _resource_error(exc) from exc
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REVISION_CONFLICT", "message": "卡片已经更新，请刷新后再试"},
        ) from exc
    except ResourceNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CARDS_NOT_READY", "message": "卡片还在整理，请稍后再试"},
        ) from exc
    except CommandTargetChangedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "COMMAND_TARGET_CHANGED", "message": "这张卡片已经变化，请刷新后再试"},
        ) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新执行这次调整"},
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "正在处理同一个调整，请稍后查看"},
        ) from exc
    response.headers["ETag"] = f'"{outcome.opaque_etag}"'
    response.headers["Cache-Control"] = "no-store"
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.applied


@router.post(
    "/{public_resource_id}/claim",
    response_model=ClaimedTripView,
)
async def claim_trip_understanding(
    public_resource_id: str,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    key = _require_idempotency_key(idempotency_key)
    capability = _capability_from_cookie(
        request.cookies.get(get_settings().trip_understanding_cookie_name)
    )
    settings = get_settings()
    service = TripUnderstandingApplicationService(
        repository,
        full_retention_days=settings.trip_understanding_full_retention_days,
    )
    try:
        outcome = await service.claim_demo(
            public_resource_id,
            # A replay after a successful claim no longer has the anonymous
            # cookie.  The repository authenticates that narrow replay with
            # user + old public id + idempotency key; a first claim still
            # requires the real capability and fails closed on this sentinel.
            capability_hash=capability or "",
            user_id=current_user,
            idempotency_key=key,
        )
    except (ResourceGoneError, ResourceNotFoundError, ResourceAccessDeniedError) as exc:
        raise _resource_error(exc) from exc
    except ResourceNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CARDS_NOT_READY", "message": "卡片还在整理，请稍后再领取"},
        ) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新领取这份行程"},
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "正在领取这份行程，请稍后查看"},
        ) from exc
    response.headers["ETag"] = f'"{outcome.opaque_etag}"'
    response.headers["Location"] = (
        f"/api/v3/trip-understandings/{outcome.claimed.public_resource_id}/result"
    )
    response.headers["Cache-Control"] = "no-store"
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.claimed


async def _private_import_view(public_resource_id, request, repository, current_user, *, supplementary=False, include_pending_lodgings=False):
    try:
        resource = await _authorize(public_resource_id,
            cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
            user_id=current_user, repository=repository)
        reader = repository.get_supplementary_view if supplementary else repository.get_source_view
        options = {"include_pending_lodgings": True} if supplementary and include_pending_lodgings else {}
        return await reader(resource, now=datetime.now(timezone.utc), **options)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except (ResourceGoneError, ResourceNotFoundError, ResourceAccessDeniedError) as exc:
        error = _resource_error(exc)
        error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
        raise error from exc


@router.get("/{public_resource_id}/source", response_model=SourceReadView)
async def read_trip_understanding_source(public_resource_id: str, request: Request,
    response: Response, repository: RepositoryDep, current_user: OptionalUserDep):
    response.headers["Cache-Control"] = "no-store"
    return await _private_import_view(public_resource_id, request, repository, current_user)


def _supplement_error(exc):
    messages = {
        "SOURCE_CONTEXT_UNAVAILABLE": "这份行程暂不能自动补全，仍可对照原文手动补充",
        "MODEL_CALL_DEADLINE_EXCEEDED": "本次整理已超过补全期限，仍可手动补充",
        "MODEL_CALL_BUDGET_EXHAUSTED": "本次整理的自动补全次数已用完",
        "RETRY_REQUIRED": "本次已尝试补全，请明确选择再试一次",
        "SUPPLEMENT_LIMIT": "本次补全及重试次数已用完",
        "SUPPLEMENT_RUNNING": "正在补全，请稍后查看或停止本次补全",
    }
    if isinstance(exc, SupplementRejectedError):
        code, message = exc.reason, messages.get(exc.reason, "暂不能补全，请刷新后查看")
    elif isinstance(exc, RevisionConflictError):
        code, message = "BASE_VERSION_CHANGED", "行程已变化，请刷新后基于当前内容补全"
    elif isinstance(exc, SourceUnavailableError):
        code, message = "SOURCE_UNAVAILABLE", "原文已不可用，仍可手动补充行程"
    elif isinstance(exc, ResourceNotReadyError):
        code, message = "CARDS_NOT_READY", "请先等待卡片整理完成"
    elif isinstance(exc, IdempotencyConflictError):
        code, message = "IDEMPOTENCY_KEY_REUSED", "请求内容已变化，请刷新后重试"
    else:
        return _resource_error(exc)
    return HTTPException(status_code=409, detail={"code": code, "message": message}, headers={"Cache-Control": "no-store"})


@router.get("/{public_resource_id}/supplements", response_model=SupplementStateView)
async def read_trip_supplement(public_resource_id: str, request: Request, response: Response,
    repository: RepositoryDep, current_user: OptionalUserDep):
    response.headers["Cache-Control"] = "no-store"
    resource = await _authorize(public_resource_id, cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user, repository=repository)
    try:
        return await repository.get_supplement_state(resource, now=datetime.now(timezone.utc))
    except (ResourceGoneError, ResourceAccessDeniedError, ResourceNotFoundError) as exc:
        raise _resource_error(exc) from exc


@router.post("/{public_resource_id}/supplements", response_model=SupplementStateView, status_code=202)
async def request_trip_supplement(public_resource_id: str, body: SupplementRequest, request: Request, response: Response,
    repository: RepositoryDep, current_user: OptionalUserDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    etag, key = _require_if_match(if_match), _require_idempotency_key(idempotency_key)
    response.headers["Cache-Control"] = "no-store"
    resource = await _authorize(public_resource_id, cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user, repository=repository)
    try:
        return await repository.request_supplement(resource, expected_etag=etag, idempotency_key=key,
            retry=body.retry, now=datetime.now(timezone.utc))
    except (SupplementRejectedError, RevisionConflictError, SourceUnavailableError, ResourceNotReadyError,
            IdempotencyConflictError, ResourceGoneError, ResourceAccessDeniedError, ResourceNotFoundError) as exc:
        raise _supplement_error(exc) from exc


@router.post("/{public_resource_id}/supplements/{job_id}/cancel", response_model=SupplementStateView)
async def cancel_trip_supplement(public_resource_id: str, job_id: str, request: Request, response: Response,
    repository: RepositoryDep, current_user: OptionalUserDep):
    response.headers["Cache-Control"] = "no-store"
    resource = await _authorize(public_resource_id, cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user, repository=repository)
    try:
        return await repository.cancel_supplement(resource, job_id, now=datetime.now(timezone.utc))
    except (ResourceGoneError, ResourceAccessDeniedError, ResourceNotFoundError) as exc:
        raise _resource_error(exc) from exc


@router.get("/{public_resource_id}/supplementary", response_model=SupplementaryView)
async def read_trip_understanding_supplementary(public_resource_id: str, request: Request,
    response: Response, repository: RepositoryDep, current_user: OptionalUserDep, include_pending_lodgings: bool = False):
    response.headers["Cache-Control"] = "no-store"
    return await _private_import_view(public_resource_id, request, repository, current_user, supplementary=True,
        include_pending_lodgings=include_pending_lodgings)


@router.delete("/{public_resource_id}/source", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip_understanding_source(
    public_resource_id: str,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    key = _require_idempotency_key(idempotency_key)
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    try:
        outcome = await TripUnderstandingApplicationService(repository).delete_source(
            resource,
            user_id=current_user,
            idempotency_key=key,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新执行删除原文"},
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "正在删除原文，请稍后查看"},
        ) from exc
    except (ResourceGoneError, ResourceNotFoundError, ResourceAccessDeniedError) as exc:
        raise _resource_error(exc) from exc
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    response.headers["Cache-Control"] = "no-store"
    return None


@router.delete("/{public_resource_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip_understanding(
    public_resource_id: str,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    key = _require_idempotency_key(idempotency_key)
    capability = _capability_from_cookie(
        request.cookies.get(get_settings().trip_understanding_cookie_name)
    )
    service = TripUnderstandingApplicationService(repository)
    tombstone_reason = await repository.tombstone_reason(public_resource_id)
    if tombstone_reason == "DELETED":
        try:
            replayed = await service.replay_trip_deletion(
                public_resource_id,
                capability_hash=capability,
                user_id=current_user,
                idempotency_key=key,
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新执行删除行程"},
            ) from exc
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
            response.headers["Cache-Control"] = "no-store"
            return None
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "RESOURCE_GONE", "message": "这份行程已不可用"},
        )
    if tombstone_reason is not None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "RESOURCE_GONE", "message": "这份行程已不可用"},
        )
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    try:
        outcome = await service.delete_trip(
            resource,
            capability_hash=capability,
            user_id=current_user,
            idempotency_key=key,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新执行删除行程"},
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "正在删除行程，请稍后查看"},
        ) from exc
    except (ResourceGoneError, ResourceNotFoundError, ResourceAccessDeniedError) as exc:
        raise _resource_error(exc) from exc
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    response.headers["Cache-Control"] = "no-store"
    return None


@account_router.delete(
    "/travel-data",
    response_model=TravelDataDeletionStatusView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_account_travel_data(
    body: AccountTravelDataDeleteRequest,
    response: Response,
    repository: RepositoryDep,
    current_user: RecentUserDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    del body
    key = _require_idempotency_key(idempotency_key)
    try:
        outcome = await TripUnderstandingApplicationService(
            repository
        ).delete_account_travel_data(
            user_id=current_user,
            idempotency_key=key,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新执行旅行数据清理"},
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "正在清理旅行数据，请稍后查看"},
        ) from exc
    response.headers["Location"] = "/api/v3/me/travel-data-deletion"
    response.headers["Cache-Control"] = "no-store"
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.view


@account_router.get("/trips", response_model=AccountTripListView)
async def list_my_trips(response: Response, repository: RepositoryDep, current_user: CurrentUserDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=4096)] = None):
    response.headers["Cache-Control"] = "no-store"
    try:
        return await repository.list_account_trips(user_id=current_user, limit=limit, cursor=cursor, now=datetime.now(timezone.utc))
    except InvalidTripCursor as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_TRIP_CURSOR", "message": "列表已更新，请从头重新加载"},
            headers={"Cache-Control": "no-store"}) from exc


@account_router.get(
    "/travel-data-deletion",
    response_model=TravelDataDeletionStatusView,
)
async def get_account_travel_data_deletion(
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
):
    response.headers["Cache-Control"] = "no-store"
    return await TripUnderstandingApplicationService(
        repository
    ).get_account_travel_data_deletion(user_id=current_user)


def _parse_last_event_id(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_EVENT_CURSOR", "message": "事件游标无效"},
        ) from exc
    if value < 0 or value > 2**63 - 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_EVENT_CURSOR", "message": "事件游标无效"},
        )
    return value


@router.get(
    "/{public_resource_id}/events",
    response_class=ServerSentEventResponse,
)
async def stream_trip_understanding_events(
    public_resource_id: str,
    request: Request,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
):
    cookie_value = request.cookies.get(
        get_settings().trip_understanding_cookie_name
    )
    resource = await _authorize(
        public_resource_id,
        cookie_value=cookie_value,
        user_id=current_user,
        repository=repository,
    )
    cursor = _parse_last_event_id(last_event_id)
    settings = get_settings()

    async def generate():
        nonlocal cursor
        deadline = asyncio.get_running_loop().time() + settings.trip_understanding_sse_max_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                current_resource = await TripUnderstandingApplicationService(
                    repository
                ).authorize(
                    public_resource_id,
                    capability_hash=_capability_from_cookie(cookie_value),
                    user_id=current_user,
                )
            except (ResourceGoneError, ResourceNotFoundError, ResourceAccessDeniedError):
                return
            if current_resource.understanding_id != resource.understanding_id:
                return
            events = await repository.list_events(
                current_resource, after_event_id=cursor
            )
            if events:
                for event in events:
                    # A claim rotates the public resource id and revokes the
                    # anonymous capability. Re-authorize immediately before
                    # exposing each potentially card-bearing snapshot.
                    try:
                        latest_resource = await TripUnderstandingApplicationService(
                            repository
                        ).authorize(
                            public_resource_id,
                            capability_hash=_capability_from_cookie(cookie_value),
                            user_id=current_user,
                        )
                    except (
                        ResourceGoneError,
                        ResourceNotFoundError,
                        ResourceAccessDeniedError,
                    ):
                        return
                    if latest_resource.understanding_id != resource.understanding_id:
                        return
                    cursor = event.event_id
                    payload = json.dumps(event.payload.model_dump(mode="json"), ensure_ascii=False)
                    yield f"id: {event.event_id}\nevent: {event.event_type}\ndata: {payload}\n\n"
                    if (
                        event.event_type == "result_available"
                        or event.payload.status in {"FAILED", "CANCELLED"}
                    ):
                        return
            if await request.is_disconnected():
                return
            await asyncio.sleep(settings.trip_understanding_sse_poll_seconds)
        yield ": keep-alive\n\n"

    return ServerSentEventResponse(
        generate(),
        headers={
            "Cache-Control": "no-cache, no-store, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
