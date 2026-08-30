from __future__ import annotations

import hmac
from email import policy
from email.parser import BytesParser
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from app.config import get_settings
from app.importing.entity_resolver import AmapEntityCandidateProvider, EntityCandidateProvider, EntityResolver
from app.importing.models import ImportApplyResult, ImportSourceType, ItineraryImport
from app.importing.repositories import ImportRepository, PostgresImportRepository
from app.importing.service import ImportApplicationService
from app.importing.screenshots import (
    MAX_MULTIPART_BYTES,
    OcrEngine,
    PaddleOcrEngine,
    PostgresScreenshotAssetRepository,
    ScreenshotAssetRepository,
    ScreenshotImportResult,
    ScreenshotImportService,
    ScreenshotUpload,
)
from app.importing.upload_batches import (
    BatchCommandReplay,
    PostgresScreenshotUploadBatchRepository,
    ScreenshotUploadBatch,
    ScreenshotUploadBatchCancelResult,
    ScreenshotUploadBatchCommitResult,
    ScreenshotUploadBatchRepository,
    ScreenshotUploadBatchService,
)
from app.itineraries.errors import ItineraryDomainError
from app.itineraries.repositories import ItineraryRepository, PostgresItineraryRepository
from app.operations.http import require_idempotency_key
from app.operations.repositories import (
    CreationCommandRepository,
    PostgresCreationCommandRepository,
)
from app.services.room_access import require_room_member
from app.trip_check.briefs import PostgresTripBriefRepository, TripBriefRepository
from app.utils.auth import get_current_user


def require_legacy_import_diagnostic_access(
    diagnostic_key: Annotated[
        str | None,
        Header(alias="X-Breeze-Diagnostic-Key"),
    ] = None,
) -> None:
    settings = get_settings()
    if settings.runtime_profile != "public":
        return
    expected = settings.legacy_import_diagnostics_key.strip()
    supplied = (diagnostic_key or "").strip()
    if (
        not settings.legacy_import_diagnostics_enabled
        or len(expected) < 32
        or not supplied
        or not hmac.compare_digest(expected, supplied)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


router = APIRouter(dependencies=[Depends(require_legacy_import_diagnostic_access)])


def get_import_repository() -> ImportRepository:
    return PostgresImportRepository()


def get_itinerary_repository() -> ItineraryRepository:
    return PostgresItineraryRepository()


def get_entity_candidate_provider() -> EntityCandidateProvider:
    return AmapEntityCandidateProvider()


def get_creation_command_repository() -> CreationCommandRepository:
    return PostgresCreationCommandRepository()


def get_trip_brief_repository() -> TripBriefRepository:
    return PostgresTripBriefRepository()


def get_screenshot_asset_repository() -> ScreenshotAssetRepository:
    return PostgresScreenshotAssetRepository()


def get_ocr_engine():
    return PaddleOcrEngine()


def get_screenshot_upload_batch_repository() -> ScreenshotUploadBatchRepository:
    return PostgresScreenshotUploadBatchRepository()


ImportRepositoryDep = Annotated[ImportRepository, Depends(get_import_repository)]
ItineraryRepositoryDep = Annotated[ItineraryRepository, Depends(get_itinerary_repository)]
EntityProviderDep = Annotated[EntityCandidateProvider, Depends(get_entity_candidate_provider)]
CurrentUserDep = Annotated[str, Depends(get_current_user)]
CreationCommandRepositoryDep = Annotated[
    CreationCommandRepository,
    Depends(get_creation_command_repository),
]
TripBriefRepositoryDep = Annotated[TripBriefRepository, Depends(get_trip_brief_repository)]
ScreenshotAssetRepositoryDep = Annotated[
    ScreenshotAssetRepository,
    Depends(get_screenshot_asset_repository),
]
OcrEngineDep = Annotated[OcrEngine, Depends(get_ocr_engine)]
ScreenshotUploadBatchRepositoryDep = Annotated[
    ScreenshotUploadBatchRepository,
    Depends(get_screenshot_upload_batch_repository),
]


class CreateImportRequest(BaseModel):
    source_type: ImportSourceType = ImportSourceType.AI_TEXT
    raw_text: str = Field(min_length=1, max_length=12000)


class ResolutionConfirmation(BaseModel):
    raw_stop_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)


class ConfirmResolutionsRequest(BaseModel):
    confirmations: list[ResolutionConfirmation] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_raw_stops(self) -> "ConfirmResolutionsRequest":
        raw_stop_ids = [item.raw_stop_id for item in self.confirmations]
        if len(raw_stop_ids) != len(set(raw_stop_ids)):
            raise ValueError("raw_stop_id confirmations must be unique")
        return self


class RetryResolutionRequest(BaseModel):
    query: str = Field(min_length=1, max_length=160)


class ImportListResponse(BaseModel):
    items: list[ItineraryImport]
    limit: int
    unfinished_only: bool


class CreateScreenshotUploadBatchRequest(BaseModel):
    expected_count: int = Field(ge=1, le=6)


def _domain_error(exc: ItineraryDomainError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


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
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match must contain an import state integer"},
        ) from exc
    if value <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match import state must be positive"},
        )
    return value


def _set_import_headers(response: Response, itinerary_import: ItineraryImport) -> None:
    response.headers["ETag"] = f'"{itinerary_import.state_version}"'
    response.headers["Cache-Control"] = "no-store"


def _set_batch_headers(response: Response, batch: ScreenshotUploadBatch) -> None:
    response.headers["ETag"] = f'"{batch.version}"'
    response.headers["Cache-Control"] = "no-store"


def _batch_service(
    *,
    batch_repository: ScreenshotUploadBatchRepository,
    import_repository: ImportRepository,
    itinerary_repository: ItineraryRepository,
    trip_brief_repository: TripBriefRepository,
    entity_provider: EntityCandidateProvider,
    command_repository: CreationCommandRepository,
    asset_repository: ScreenshotAssetRepository,
    ocr_engine: OcrEngine,
) -> ScreenshotUploadBatchService:
    return ScreenshotUploadBatchService(
        repository=batch_repository,
        asset_repository=asset_repository,
        import_service=ScreenshotImportService(
            import_repository=import_repository,
            itinerary_repository=itinerary_repository,
            trip_brief_repository=trip_brief_repository,
            entity_resolver=EntityResolver(entity_provider),
            command_repository=command_repository,
            asset_repository=asset_repository,
            ocr_engine=ocr_engine,
        ),
    )


def _idempotency_key(raw: str | None, *, import_id: str) -> str:
    if raw is None or not raw.strip():
        return f"legacy-apply:{import_id}"
    value = raw.strip()
    if len(value) > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "Idempotency-Key exceeds 200 characters"},
        )
    return value


async def _workspace_access(workspace_id: str, user_id: str, repository: ItineraryRepository):
    workspace = await repository.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    await require_room_member(workspace.room_id, user_id)
    return workspace


async def _read_screenshot_multipart(request: Request) -> list[ScreenshotUpload]:
    content_type = request.headers.get("content-type", "")
    if not content_type.casefold().startswith("multipart/form-data"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "MULTIPART_REQUIRED", "message": "multipart/form-data is required"},
        )
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_CONTENT_LENGTH", "message": "Content-Length must be an integer"},
            ) from exc
        if content_length > MAX_MULTIPART_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={"code": "SCREENSHOT_BATCH_TOO_LARGE", "message": "screenshot batch exceeds 61MB"},
            )
    body = await request.body()
    if len(body) > MAX_MULTIPART_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "SCREENSHOT_BATCH_TOO_LARGE", "message": "screenshot batch exceeds 61MB"},
        )
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body
    )
    if not message.is_multipart():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_MULTIPART", "message": "multipart body is malformed"},
        )
    uploads: list[ScreenshotUpload] = []
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        if part.get_param("name", header="content-disposition") not in {"screenshot", "screenshots", "file", "files"}:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b""
        uploads.append(
            ScreenshotUpload(
                media_type=part.get_content_type().casefold(),
                content=payload,
            )
        )
    return uploads


async def _import_access(
    workspace_id: str,
    import_id: str,
    user_id: str,
    itinerary_repository: ItineraryRepository,
    import_repository: ImportRepository,
) -> ItineraryImport:
    await _workspace_access(workspace_id, user_id, itinerary_repository)
    itinerary_import = await import_repository.get_import(import_id)
    if itinerary_import is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    if itinerary_import.workspace_id != workspace_id:
        raise HTTPException(status_code=403, detail={"code": "RESOURCE_SCOPE_DENIED"})
    return itinerary_import


@router.post(
    "/trip-workspaces/{workspace_id}/imports",
    response_model=ItineraryImport,
    status_code=status.HTTP_201_CREATED,
)
async def create_itinerary_import(
    workspace_id: str,
    body: CreateImportRequest,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    import_repository: ImportRepositoryDep,
    entity_provider: EntityProviderDep,
    command_repository: CreationCommandRepositoryDep,
    trip_brief_repository: TripBriefRepositoryDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    try:
        result, replayed = await ImportApplicationService(
            import_repository=import_repository,
            itinerary_repository=itinerary_repository,
            entity_resolver=EntityResolver(entity_provider),
            trip_brief_repository=trip_brief_repository,
        ).create_import_idempotent(
            workspace_id=workspace_id,
            source_type=body.source_type,
            raw_text=body.raw_text,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
            command_repository=command_repository,
        )
        _set_import_headers(response, result)
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return result
    except ItineraryDomainError as exc:
        _domain_error(exc)


@router.post(
    "/trip-workspaces/{workspace_id}/imports/screenshots",
    response_model=ScreenshotImportResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_screenshot_import(
    workspace_id: str,
    request: Request,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    import_repository: ImportRepositoryDep,
    entity_provider: EntityProviderDep,
    command_repository: CreationCommandRepositoryDep,
    trip_brief_repository: TripBriefRepositoryDep,
    asset_repository: ScreenshotAssetRepositoryDep,
    ocr_engine: OcrEngineDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    required_key = require_idempotency_key(idempotency_key)
    uploads = await _read_screenshot_multipart(request)
    try:
        result, replayed = await ScreenshotImportService(
            import_repository=import_repository,
            itinerary_repository=itinerary_repository,
            trip_brief_repository=trip_brief_repository,
            entity_resolver=EntityResolver(entity_provider),
            command_repository=command_repository,
            asset_repository=asset_repository,
            ocr_engine=ocr_engine,
        ).create_import(
            workspace_id=workspace_id,
            uploads=uploads,
            actor_user_id=current_user,
            idempotency_key=required_key,
        )
        _set_import_headers(response, result.itinerary_import)
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return result
    except ItineraryDomainError as exc:
        _domain_error(exc)


@router.post(
    "/trip-workspaces/{workspace_id}/screenshot-upload-batches",
    response_model=ScreenshotUploadBatch,
    status_code=status.HTTP_201_CREATED,
)
async def create_screenshot_upload_batch(
    workspace_id: str,
    body: CreateScreenshotUploadBatchRequest,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    import_repository: ImportRepositoryDep,
    entity_provider: EntityProviderDep,
    command_repository: CreationCommandRepositoryDep,
    trip_brief_repository: TripBriefRepositoryDep,
    asset_repository: ScreenshotAssetRepositoryDep,
    batch_repository: ScreenshotUploadBatchRepositoryDep,
    ocr_engine: OcrEngineDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    try:
        batch, replayed = await _batch_service(
            batch_repository=batch_repository,
            import_repository=import_repository,
            itinerary_repository=itinerary_repository,
            trip_brief_repository=trip_brief_repository,
            entity_provider=entity_provider,
            command_repository=command_repository,
            asset_repository=asset_repository,
            ocr_engine=ocr_engine,
        ).create_batch(
            workspace_id=workspace_id,
            actor_user_id=current_user,
            expected_count=body.expected_count,
            idempotency_key=require_idempotency_key(idempotency_key),
        )
        _set_batch_headers(response, batch)
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return batch
    except ItineraryDomainError as exc:
        _domain_error(exc)


@router.post(
    "/trip-workspaces/{workspace_id}/screenshot-upload-batches/{batch_id}/files/{position}",
    response_model=ScreenshotUploadBatch,
)
async def upload_screenshot_batch_file(
    workspace_id: str,
    batch_id: str,
    position: int,
    request: Request,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    import_repository: ImportRepositoryDep,
    entity_provider: EntityProviderDep,
    command_repository: CreationCommandRepositoryDep,
    trip_brief_repository: TripBriefRepositoryDep,
    asset_repository: ScreenshotAssetRepositoryDep,
    batch_repository: ScreenshotUploadBatchRepositoryDep,
    ocr_engine: OcrEngineDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    if not 0 <= position < 6:
        raise HTTPException(
            status_code=422,
            detail={"code": "SCREENSHOT_POSITION_INVALID", "message": "position must be between 0 and 5"},
        )
    uploads = await _read_screenshot_multipart(request)
    if len(uploads) != 1:
        raise HTTPException(
            status_code=422,
            detail={"code": "SCREENSHOT_BATCH_INVALID", "message": "exactly one screenshot file is required"},
        )
    try:
        batch, replayed = await _batch_service(
            batch_repository=batch_repository,
            import_repository=import_repository,
            itinerary_repository=itinerary_repository,
            trip_brief_repository=trip_brief_repository,
            entity_provider=entity_provider,
            command_repository=command_repository,
            asset_repository=asset_repository,
            ocr_engine=ocr_engine,
        ).upload_file(
            batch_id=batch_id,
            workspace_id=workspace_id,
            actor_user_id=current_user,
            position=position,
            expected_version=_parse_if_match(if_match),
            upload=uploads[0],
            idempotency_key=require_idempotency_key(idempotency_key),
        )
        _set_batch_headers(response, batch)
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return batch
    except ItineraryDomainError as exc:
        _domain_error(exc)


@router.post(
    "/trip-workspaces/{workspace_id}/screenshot-upload-batches/{batch_id}/commit",
    response_model=ScreenshotUploadBatchCommitResult,
    status_code=status.HTTP_201_CREATED,
)
async def commit_screenshot_upload_batch(
    workspace_id: str,
    batch_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    import_repository: ImportRepositoryDep,
    entity_provider: EntityProviderDep,
    command_repository: CreationCommandRepositoryDep,
    trip_brief_repository: TripBriefRepositoryDep,
    asset_repository: ScreenshotAssetRepositoryDep,
    batch_repository: ScreenshotUploadBatchRepositoryDep,
    ocr_engine: OcrEngineDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    try:
        result, replayed = await _batch_service(
            batch_repository=batch_repository,
            import_repository=import_repository,
            itinerary_repository=itinerary_repository,
            trip_brief_repository=trip_brief_repository,
            entity_provider=entity_provider,
            command_repository=command_repository,
            asset_repository=asset_repository,
            ocr_engine=ocr_engine,
        ).commit(
            batch_id=batch_id,
            workspace_id=workspace_id,
            actor_user_id=current_user,
            expected_version=_parse_if_match(if_match),
            idempotency_key=require_idempotency_key(idempotency_key),
        )
        if isinstance(result, BatchCommandReplay):
            if result.status_code >= 400:
                raise HTTPException(status_code=result.status_code, detail=result.body["detail"])
            parsed = ScreenshotUploadBatchCommitResult.model_validate(result.body)
        else:
            parsed = result
        _set_batch_headers(response, parsed.batch)
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return parsed
    except ItineraryDomainError as exc:
        _domain_error(exc)


@router.delete(
    "/trip-workspaces/{workspace_id}/screenshot-upload-batches/{batch_id}",
    response_model=ScreenshotUploadBatchCancelResult,
)
async def cancel_screenshot_upload_batch(
    workspace_id: str,
    batch_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    import_repository: ImportRepositoryDep,
    entity_provider: EntityProviderDep,
    command_repository: CreationCommandRepositoryDep,
    trip_brief_repository: TripBriefRepositoryDep,
    asset_repository: ScreenshotAssetRepositoryDep,
    batch_repository: ScreenshotUploadBatchRepositoryDep,
    ocr_engine: OcrEngineDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    try:
        result, replayed = await _batch_service(
            batch_repository=batch_repository,
            import_repository=import_repository,
            itinerary_repository=itinerary_repository,
            trip_brief_repository=trip_brief_repository,
            entity_provider=entity_provider,
            command_repository=command_repository,
            asset_repository=asset_repository,
            ocr_engine=ocr_engine,
        ).cancel(
            batch_id=batch_id,
            workspace_id=workspace_id,
            actor_user_id=current_user,
            expected_version=_parse_if_match(if_match),
            idempotency_key=require_idempotency_key(idempotency_key),
        )
        if isinstance(result, BatchCommandReplay):
            if result.status_code >= 400:
                raise HTTPException(status_code=result.status_code, detail=result.body["detail"])
            parsed = ScreenshotUploadBatchCancelResult.model_validate(result.body)
        else:
            parsed = result
        _set_batch_headers(response, parsed.batch)
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return parsed
    except ItineraryDomainError as exc:
        _domain_error(exc)


@router.get(
    "/trip-workspaces/{workspace_id}/imports",
    response_model=ImportListResponse,
)
async def list_itinerary_imports(
    workspace_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    import_repository: ImportRepositoryDep,
    limit: int = Query(default=20, ge=1, le=50),
    unfinished_only: bool = Query(default=False),
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    items = await import_repository.list_imports(
        workspace_id,
        limit=limit,
        unfinished_only=unfinished_only,
    )
    return ImportListResponse(items=items, limit=limit, unfinished_only=unfinished_only)


@router.get(
    "/trip-workspaces/{workspace_id}/imports/latest",
    response_model=ItineraryImport,
)
async def get_latest_itinerary_import(
    workspace_id: str,
    response: Response,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    import_repository: ImportRepositoryDep,
    unfinished: bool = Query(default=True),
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    items = await import_repository.list_imports(
        workspace_id,
        limit=1,
        unfinished_only=unfinished,
    )
    if not items:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "matching itinerary import does not exist"},
        )
    _set_import_headers(response, items[0])
    return items[0]


@router.get(
    "/trip-workspaces/{workspace_id}/imports/{import_id}",
    response_model=ItineraryImport,
)
async def get_itinerary_import(
    workspace_id: str,
    import_id: str,
    response: Response,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    import_repository: ImportRepositoryDep,
):
    result = await _import_access(
        workspace_id,
        import_id,
        current_user,
        itinerary_repository,
        import_repository,
    )
    _set_import_headers(response, result)
    return result


@router.patch(
    "/trip-workspaces/{workspace_id}/imports/{import_id}/resolutions",
    response_model=ItineraryImport,
)
async def confirm_itinerary_resolutions(
    workspace_id: str,
    import_id: str,
    body: ConfirmResolutionsRequest,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    import_repository: ImportRepositoryDep,
    entity_provider: EntityProviderDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
):
    await _import_access(workspace_id, import_id, current_user, itinerary_repository, import_repository)
    service = ImportApplicationService(
        import_repository=import_repository,
        itinerary_repository=itinerary_repository,
        entity_resolver=EntityResolver(entity_provider),
    )
    try:
        result = await service.confirm_resolutions(
            import_id=import_id,
            confirmations={item.raw_stop_id: item.place_id for item in body.confirmations},
            actor_user_id=current_user,
            expected_state_version=_parse_if_match(if_match),
        )
    except ItineraryDomainError as exc:
        _domain_error(exc)
    _set_import_headers(response, result)
    return result


@router.post(
    "/trip-workspaces/{workspace_id}/imports/{import_id}/raw-stops/{raw_stop_id}/candidates:search",
    response_model=ItineraryImport,
)
async def retry_itinerary_resolution(
    workspace_id: str,
    import_id: str,
    raw_stop_id: str,
    body: RetryResolutionRequest,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    import_repository: ImportRepositoryDep,
    entity_provider: EntityProviderDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
):
    await _import_access(workspace_id, import_id, current_user, itinerary_repository, import_repository)
    try:
        result = await ImportApplicationService(
            import_repository=import_repository,
            itinerary_repository=itinerary_repository,
            entity_resolver=EntityResolver(entity_provider),
        ).retry_resolution(
            import_id=import_id,
            raw_stop_id=raw_stop_id,
            query=body.query,
            expected_state_version=_parse_if_match(if_match),
        )
        _set_import_headers(response, result)
        return result
    except ItineraryDomainError as exc:
        _domain_error(exc)


@router.post(
    "/trip-workspaces/{workspace_id}/imports/{import_id}/apply",
    response_model=ImportApplyResult,
)
async def apply_itinerary_import(
    workspace_id: str,
    import_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    import_repository: ImportRepositoryDep,
    entity_provider: EntityProviderDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _import_access(workspace_id, import_id, current_user, itinerary_repository, import_repository)
    try:
        result = await ImportApplicationService(
            import_repository=import_repository,
            itinerary_repository=itinerary_repository,
            entity_resolver=EntityResolver(entity_provider),
        ).apply_import(
            import_id,
            actor_user_id=current_user,
            expected_state_version=_parse_if_match(if_match),
            idempotency_key=_idempotency_key(idempotency_key, import_id=import_id),
        )
        _set_import_headers(response, result.itinerary_import)
        return result
    except ItineraryDomainError as exc:
        _domain_error(exc)
