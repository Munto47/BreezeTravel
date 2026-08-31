from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from app.config import get_settings
from app.trip_understanding.errors import (
    IdempotencyConflictError,
    ResourceAccessDeniedError,
    ResourceNotFoundError,
)
from app.trip_understanding.memory_share import (
    ConsentUpdateRequest,
    DataConsentView,
    FeedbackAcceptedView,
    FeedbackRequest,
    MemoryShareRepository,
    PreferenceMemoryView,
    ShareCreateRequest,
    ShareCreatedView,
    ShareExchangeRequest,
    ShareListItemView,
    ShareProjectionView,
)
from app.trip_understanding.repository import TripUnderstandingRepository
from app.api.trip_understandings_v3 import (
    _authorize,
    _require_idempotency_key,
    _settings_signing_key,
    get_trip_understanding_repository,
)
from app.utils.auth import get_current_user


router = APIRouter(prefix="/v3")
RepositoryDep = Annotated[
    TripUnderstandingRepository,
    Depends(get_trip_understanding_repository),
]
CurrentUserDep = Annotated[str, Depends(get_current_user)]
SHARE_COOKIE_NAME = "bt_g06_share"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _memory_share_repository(repository: TripUnderstandingRepository) -> MemoryShareRepository:
    return repository


def _share_unavailable(exc: Exception | None = None) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "SHARE_UNAVAILABLE", "message": "这份分享已不可用"},
    )


@router.get("/me/data-consents", response_model=DataConsentView)
async def get_data_consents(
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
):
    response.headers["Cache-Control"] = "no-store"
    return await _memory_share_repository(repository).get_data_consents(current_user)


@router.put("/me/data-consents/{purpose}", response_model=DataConsentView)
async def set_data_consent(
    purpose: Literal["memory", "feedback", "training-eval"],
    body: ConsentUpdateRequest,
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
):
    response.headers["Cache-Control"] = "no-store"
    return await _memory_share_repository(repository).set_data_consent(
        current_user,
        purpose,
        body.enabled,
        now=_utcnow(),
    )


@router.get("/me/travel-preferences", response_model=PreferenceMemoryView | None)
async def get_travel_preferences(
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
):
    response.headers["Cache-Control"] = "no-store"
    return await _memory_share_repository(repository).get_preference_memory(current_user)


@router.put("/me/travel-preferences", response_model=PreferenceMemoryView)
async def save_travel_preferences(
    body: PreferenceMemoryView,
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
):
    try:
        value = await _memory_share_repository(repository).save_preference_memory(
            current_user,
            body,
            now=_utcnow(),
        )
    except ResourceAccessDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "MEMORY_NOT_ENABLED", "message": "请先主动开启旅行偏好记忆"},
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    return value


@router.delete("/me/travel-preferences", status_code=status.HTTP_204_NO_CONTENT)
async def clear_travel_preferences(
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
):
    await _memory_share_repository(repository).clear_preference_memory(current_user)
    response.headers["Cache-Control"] = "no-store"
    return None


@router.post(
    "/trip-understandings/{public_resource_id}/feedback",
    response_model=FeedbackAcceptedView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def record_trip_feedback(
    public_resource_id: str,
    body: FeedbackRequest,
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    key = _require_idempotency_key(idempotency_key)
    resource = await _authorize(
        public_resource_id,
        cookie_value=None,
        user_id=current_user,
        repository=repository,
    )
    try:
        replayed = await _memory_share_repository(repository).record_feedback(
            resource,
            current_user,
            body,
            idempotency_key=key,
            now=_utcnow(),
        )
    except ResourceAccessDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "FEEDBACK_NOT_ENABLED", "message": "请先主动开启产品反馈"},
        ) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新提交这次反馈"},
        ) from exc
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    response.headers["Cache-Control"] = "no-store"
    return FeedbackAcceptedView()


@router.post(
    "/trip-understandings/{public_resource_id}/shares",
    response_model=ShareCreatedView,
    status_code=status.HTTP_201_CREATED,
)
async def create_trip_share(
    public_resource_id: str,
    body: ShareCreateRequest,
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    key = _require_idempotency_key(idempotency_key)
    resource = await _authorize(
        public_resource_id,
        cookie_value=None,
        user_id=current_user,
        repository=repository,
    )
    stored = await repository.get_result(resource)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "TRIP_NOT_READY", "message": "行程还在整理，请稍后分享"},
        )
    try:
        view, replayed = await _memory_share_repository(repository).create_share(
            resource,
            current_user,
            stored.result,
            idempotency_key=key,
            expires_in_days=body.expires_in_days,
            signing_key=_settings_signing_key(),
            now=_utcnow(),
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新创建分享"},
        ) from exc
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    response.headers["Cache-Control"] = "no-store"
    return view


@router.get("/me/shares", response_model=list[ShareListItemView])
async def list_my_shares(
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
):
    response.headers["Cache-Control"] = "no-store"
    return await _memory_share_repository(repository).list_shares(
        current_user, now=_utcnow()
    )


@router.delete("/me/shares/{share_ref}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_my_share(
    share_ref: str,
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
):
    revoked = await _memory_share_repository(repository).revoke_share(
        share_ref,
        current_user,
        now=_utcnow(),
    )
    if not revoked:
        raise _share_unavailable()
    response.headers["Cache-Control"] = "no-store"
    return None


@router.post("/shares/{share_ref}/exchange", status_code=status.HTTP_204_NO_CONTENT)
async def exchange_share_secret(
    share_ref: str,
    body: ShareExchangeRequest,
    response: Response,
    repository: RepositoryDep,
):
    try:
        outcome = await _memory_share_repository(repository).exchange_share_secret(
            share_ref,
            body.secret,
            now=_utcnow(),
        )
    except ResourceNotFoundError as exc:
        raise _share_unavailable(exc) from exc
    settings = get_settings()
    max_age = max(1, int((outcome.expires_at - _utcnow()).total_seconds()))
    response.set_cookie(
        key=SHARE_COOKIE_NAME,
        value=outcome.capability,
        max_age=max_age,
        httponly=True,
        secure=settings.runtime_profile == "public",
        samesite="lax",
        path="/api/v3/shares",
    )
    response.headers["Cache-Control"] = "no-store"
    return None


@router.get("/shares/{share_ref}", response_model=ShareProjectionView)
async def read_shared_trip(
    share_ref: str,
    request: Request,
    response: Response,
    repository: RepositoryDep,
):
    try:
        view = await _memory_share_repository(repository).read_share(
            share_ref,
            request.cookies.get(SHARE_COOKIE_NAME),
            now=_utcnow(),
        )
    except ResourceNotFoundError as exc:
        raise _share_unavailable(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    return view
