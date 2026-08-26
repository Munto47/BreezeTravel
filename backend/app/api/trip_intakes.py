from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from datetime import date

from pydantic import BaseModel, Field, model_validator

from app.itineraries.errors import ItineraryDomainError
from app.importing.screenshots import (
    OcrEngine,
    PaddleOcrEngine,
    ScreenshotUpload,
    validate_screenshot_batch,
)
from app.operations.http import require_idempotency_key
from app.services.room_access import require_room_member
from app.trip_intake.materialization import (
    MaterializationResult,
    PostgresTripIntakeMaterializationRepository,
    TripIntakeMaterializationRepository,
    TripIntakeMaterializationService,
)
from app.trip_intake.models import IntakeSourceType, TripIntakeExtraction, TripIntakeRevision
from app.trip_intake.repository import (
    PostgresTripIntakeRepository,
    TripIntakeRepository,
)
from app.trip_intake.service import TripIntakeApplicationService
from app.utils.auth import get_current_user


router = APIRouter()


def get_trip_intake_repository() -> TripIntakeRepository:
    return PostgresTripIntakeRepository()


def get_trip_intake_materialization_repository() -> TripIntakeMaterializationRepository:
    return PostgresTripIntakeMaterializationRepository()


def get_trip_intake_ocr_engine() -> OcrEngine:
    return PaddleOcrEngine()


TripIntakeRepositoryDep = Annotated[TripIntakeRepository, Depends(get_trip_intake_repository)]
MaterializationRepositoryDep = Annotated[
    TripIntakeMaterializationRepository,
    Depends(get_trip_intake_materialization_repository),
]
CurrentUserDep = Annotated[str, Depends(get_current_user)]
OcrEngineDep = Annotated[OcrEngine, Depends(get_trip_intake_ocr_engine)]


class CreateTripIntakeRequest(BaseModel):
    source_type: IntakeSourceType = IntakeSourceType.MANUAL_TEXT
    raw_text: str = Field(min_length=1, max_length=12000)


class CreateScreenshotIntakeRequest(BaseModel):
    """OCR text boxes after temporary screenshot processing.

    Raw screenshot bytes stay in the existing temporary-asset boundary and are
    never accepted into the immutable Intake revision or PostgreSQL JSON.
    """

    ocr_texts: list[str] = Field(min_length=1, max_length=6)


class PatchTripIntakeRequest(BaseModel):
    extraction: TripIntakeExtraction | None = None
    confirmed_values: "ConfirmedIntakeValues | None" = None

    @model_validator(mode="after")
    def exactly_one_patch_shape(self) -> "PatchTripIntakeRequest":
        if (self.extraction is None) == (self.confirmed_values is None):
            raise ValueError("provide exactly one of extraction or confirmed_values")
        return self


class ConfirmedIntakeValues(BaseModel):
    city: str = Field(min_length=1, max_length=80)
    start_date: date
    end_date: date
    party_size: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_dates(self) -> "ConfirmedIntakeValues":
        if self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date")
        return self


PatchTripIntakeRequest.model_rebuild()


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
            status_code=400,
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match must contain a revision integer"},
        ) from exc
    if revision <= 0:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match revision must be positive"},
        )
    return revision


def _set_revision_headers(
    response: Response,
    intake: TripIntakeRevision,
    *,
    replayed: bool = False,
) -> None:
    response.headers["ETag"] = f'"{intake.revision}"'
    response.headers["Cache-Control"] = "no-store"
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"


def _raise_domain(exc: ItineraryDomainError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


async def _authorize_intake(
    intake_id: str,
    revision: int,
    user_id: str,
    repository: TripIntakeRepository,
) -> TripIntakeRevision:
    intake = await repository.get_revision(intake_id, revision)
    if intake is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    await require_room_member(intake.room_id, user_id)
    return intake


@router.post(
    "/rooms/{room_id}/trip-intakes",
    response_model=TripIntakeRevision,
    status_code=status.HTTP_201_CREATED,
)
async def create_trip_intake(
    room_id: str,
    body: CreateTripIntakeRequest,
    response: Response,
    current_user: CurrentUserDep,
    repository: TripIntakeRepositoryDep,
):
    await require_room_member(room_id, current_user)
    try:
        intake = await TripIntakeApplicationService(repository).create(
            room_id=room_id,
            source_type=body.source_type,
            source_texts=[body.raw_text],
            actor_user_id=current_user,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_TRIP_INTAKE", "message": str(exc)},
        ) from exc
    _set_revision_headers(response, intake)
    return intake


@router.post(
    "/rooms/{room_id}/trip-intakes/screenshots",
    response_model=TripIntakeRevision,
    status_code=status.HTTP_201_CREATED,
)
async def create_screenshot_trip_intake(
    room_id: str,
    request: Request,
    response: Response,
    current_user: CurrentUserDep,
    repository: TripIntakeRepositoryDep,
    ocr_engine: OcrEngineDep,
):
    await require_room_member(room_id, current_user)
    try:
        content_type = request.headers.get("content-type", "")
        source_metadata: list[dict[str, Any]] | None = None
        if content_type.startswith("application/json"):
            body = CreateScreenshotIntakeRequest.model_validate(await request.json())
            ocr_texts = body.ocr_texts
        else:
            form = await request.form()
            files = form.getlist("screenshots")
            uploads = []
            for file in files:
                media_type = getattr(file, "content_type", None) or "application/octet-stream"
                uploads.append(ScreenshotUpload(media_type=media_type, content=await file.read()))
            validate_screenshot_batch(uploads)
            ocr_texts = []
            source_metadata = []
            with tempfile.TemporaryDirectory(prefix="breezetravel-intake-ocr-") as temp_dir:
                for index, upload in enumerate(uploads):
                    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[
                        upload.media_type
                    ]
                    path = Path(temp_dir) / f"{uuid4()}{suffix}"
                    path.write_bytes(upload.content)
                    lines = await ocr_engine.recognize(path)
                    text = "\n".join(line.text for line in lines if line.text.strip())
                    if not text:
                        raise ValueError(f"screenshot {index + 1} OCR produced no text")
                    ocr_texts.append(text)
                    source_metadata.append(
                        {
                            "asset_sha256": hashlib.sha256(upload.content).hexdigest(),
                            "media_type": upload.media_type,
                            "byte_size": len(upload.content),
                            "ocr_engine": ocr_engine.name,
                            "ocr_engine_version": ocr_engine.version,
                            "lines": [line.model_dump(mode="json") for line in lines],
                            "raw_asset_retained": False,
                        }
                    )
        intake = await TripIntakeApplicationService(repository).create(
            room_id=room_id,
            source_type=IntakeSourceType.SCREENSHOT_OCR,
            source_texts=ocr_texts,
            actor_user_id=current_user,
            source_metadata=source_metadata,
        )
    except ItineraryDomainError as exc:
        _raise_domain(exc)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SCREENSHOT_INTAKE", "message": str(exc)},
        ) from exc
    _set_revision_headers(response, intake)
    return intake


@router.get("/rooms/{room_id}/trip-intakes/latest", response_model=TripIntakeRevision)
async def get_latest_room_trip_intake(
    room_id: str,
    response: Response,
    current_user: CurrentUserDep,
    repository: TripIntakeRepositoryDep,
):
    await require_room_member(room_id, current_user)
    intake = await repository.get_latest_for_room(room_id)
    if intake is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    _set_revision_headers(response, intake)
    return intake


@router.get("/trip-intakes/{intake_id}/revisions/{revision}", response_model=TripIntakeRevision)
async def get_trip_intake(
    intake_id: str,
    revision: int,
    response: Response,
    current_user: CurrentUserDep,
    repository: TripIntakeRepositoryDep,
):
    intake = await _authorize_intake(intake_id, revision, current_user, repository)
    _set_revision_headers(response, intake)
    return intake


@router.patch("/trip-intakes/{intake_id}/revisions/{revision}", response_model=TripIntakeRevision)
async def patch_trip_intake(
    intake_id: str,
    revision: int,
    body: PatchTripIntakeRequest,
    response: Response,
    current_user: CurrentUserDep,
    repository: TripIntakeRepositoryDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _authorize_intake(intake_id, revision, current_user, repository)
    if _parse_if_match(if_match) != revision:
        raise HTTPException(status_code=400, detail={"code": "IF_MATCH_PATH_MISMATCH"})
    try:
        service = TripIntakeApplicationService(repository)
        if body.confirmed_values is not None:
            intake, replayed = await service.patch_confirmed_values(
                intake_id=intake_id,
                revision=revision,
                city=body.confirmed_values.city,
                start_date=body.confirmed_values.start_date,
                end_date=body.confirmed_values.end_date,
                party_size=body.confirmed_values.party_size,
                actor_user_id=current_user,
                idempotency_key=require_idempotency_key(idempotency_key),
            )
        else:
            assert body.extraction is not None
            intake, replayed = await service.patch(
                intake_id=intake_id,
                revision=revision,
                extraction=body.extraction,
                actor_user_id=current_user,
                idempotency_key=require_idempotency_key(idempotency_key),
            )
    except ItineraryDomainError as exc:
        _raise_domain(exc)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_TRIP_INTAKE_PATCH", "message": str(exc)},
        ) from exc
    _set_revision_headers(response, intake, replayed=replayed)
    return intake


@router.post(
    "/trip-intakes/{intake_id}/revisions/{revision}/confirm",
    response_model=TripIntakeRevision,
)
async def confirm_trip_intake(
    intake_id: str,
    revision: int,
    response: Response,
    current_user: CurrentUserDep,
    repository: TripIntakeRepositoryDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _authorize_intake(intake_id, revision, current_user, repository)
    if _parse_if_match(if_match) != revision:
        raise HTTPException(status_code=400, detail={"code": "IF_MATCH_PATH_MISMATCH"})
    try:
        intake, replayed = await TripIntakeApplicationService(repository).confirm(
            intake_id=intake_id,
            revision=revision,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
        )
    except ItineraryDomainError as exc:
        _raise_domain(exc)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "TRIP_INTAKE_NOT_READY", "message": str(exc)},
        ) from exc
    _set_revision_headers(response, intake, replayed=replayed)
    return intake


@router.post(
    "/trip-intakes/{intake_id}/revisions/{revision}/materialize",
    response_model=MaterializationResult,
)
async def materialize_trip_intake(
    intake_id: str,
    revision: int,
    response: Response,
    current_user: CurrentUserDep,
    repository: TripIntakeRepositoryDep,
    materialization_repository: MaterializationRepositoryDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _authorize_intake(intake_id, revision, current_user, repository)
    if _parse_if_match(if_match) != revision:
        raise HTTPException(status_code=400, detail={"code": "IF_MATCH_PATH_MISMATCH"})
    try:
        result = await TripIntakeMaterializationService(
            intake_repository=repository,
            materialization_repository=materialization_repository,
        ).materialize(
            intake_id=intake_id,
            revision=revision,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
        )
    except ItineraryDomainError as exc:
        _raise_domain(exc)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "TRIP_INTAKE_NOT_MATERIALIZABLE", "message": str(exc)},
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    if result.idempotent_replay:
        response.headers["Idempotency-Replayed"] = "true"
    return result
