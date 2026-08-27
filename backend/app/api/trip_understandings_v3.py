from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.trip_understanding.capability import capability_hash, mint_capability
from app.trip_understanding.errors import (
    CapabilityExpiredError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    ResourceAccessDeniedError,
    ResourceGoneError,
    ResourceNotFoundError,
)
from app.trip_understanding.models import (
    CreateDemoRequest,
    TripUnderstandingAcceptedView,
    TripUnderstandingProgressView,
    UserFacingTripResult,
)
from app.trip_understanding.repository import (
    PostgresTripUnderstandingRepository,
    TripUnderstandingRepository,
)
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.utils.auth import get_optional_user


router = APIRouter(prefix="/v3/trip-understandings")


def get_trip_understanding_repository() -> TripUnderstandingRepository:
    return PostgresTripUnderstandingRepository()


RepositoryDep = Annotated[
    TripUnderstandingRepository,
    Depends(get_trip_understanding_repository),
]
OptionalUserDep = Annotated[str | None, Depends(get_optional_user)]


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
    body: CreateDemoRequest,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    del body
    key = _require_idempotency_key(idempotency_key)
    settings = get_settings()
    cookie_value = request.cookies.get(settings.trip_understanding_cookie_name)
    digest = _capability_from_cookie(cookie_value)
    if digest is None:
        cookie_value, digest = mint_capability(_settings_signing_key())
    service = TripUnderstandingApplicationService(
        repository,
        ttl_hours=settings.trip_understanding_demo_ttl_hours,
    )
    try:
        outcome = await service.create_demo(capability_hash=digest, idempotency_key=key)
    except CapabilityExpiredError:
        cookie_value, digest = mint_capability(_settings_signing_key())
        outcome = await service.create_demo(capability_hash=digest, idempotency_key=key)
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
    _set_capability_cookie(response, cookie_value)
    response.headers["Cache-Control"] = "no-store"
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.accepted


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
    response.headers["Cache-Control"] = "no-store"
    if stored is None:
        response.status_code = status.HTTP_202_ACCEPTED
        return TripUnderstandingProgressView(message="正在整理每天行程")
    response.headers["ETag"] = f'"{stored.opaque_etag}"'
    return stored.result


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
    if value < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_EVENT_CURSOR", "message": "事件游标无效"},
        )
    return value


@router.get("/{public_resource_id}/events")
async def stream_trip_understanding_events(
    public_resource_id: str,
    request: Request,
    repository: RepositoryDep,
    current_user: OptionalUserDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
):
    resource = await _authorize(
        public_resource_id,
        cookie_value=request.cookies.get(get_settings().trip_understanding_cookie_name),
        user_id=current_user,
        repository=repository,
    )
    cursor = _parse_last_event_id(last_event_id)
    settings = get_settings()

    async def generate():
        nonlocal cursor
        deadline = asyncio.get_running_loop().time() + settings.trip_understanding_sse_max_seconds
        while asyncio.get_running_loop().time() < deadline:
            events = await repository.list_events(resource, after_event_id=cursor)
            if events:
                for event in events:
                    cursor = event.event_id
                    payload = json.dumps(event.payload.model_dump(mode="json"), ensure_ascii=False)
                    yield f"id: {event.event_id}\nevent: {event.event_type}\ndata: {payload}\n\n"
                    if event.event_type == "result_available":
                        return
            if await request.is_disconnected():
                return
            await asyncio.sleep(settings.trip_understanding_sse_poll_seconds)
        yield ": keep-alive\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )
