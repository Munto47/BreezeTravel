from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.connection import get_pool
from app.importing.errors import OcrProcessingError, PrivacyBlockedError, ScreenshotBatchInvalidError
from app.importing.models import ImportSourceType, ImportStatus, ItineraryImport, resolution_set_is_ready
from app.importing.parser import ItineraryTextParser
from app.importing.repositories import ImportRepository
from app.itineraries.errors import ResourceNotFound
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import ResolutionStatus
from app.itineraries.repositories import ItineraryRepository
from app.operations.models import CreationCommandResponse, CreationOperation
from app.operations.repositories import CreationCommandRepository
from app.trip_check.briefs import TripBriefApplicationService, TripBriefRepository


MAX_SCREENSHOTS = 6
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_SCREENSHOTS * MAX_SCREENSHOT_BYTES + 1024 * 1024
SUPPORTED_MEDIA_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


class OcrBoundingBox(BaseModel):
    model_config = ConfigDict(frozen=True)

    x_min: int = Field(ge=0)
    y_min: int = Field(ge=0)
    x_max: int = Field(ge=0)
    y_max: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_extent(self) -> "OcrBoundingBox":
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError("OCR bounding box must have positive area")
        return self


class OcrTextLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0, le=1)
    box: OcrBoundingBox
    requires_confirmation: bool = False


class ScreenshotOcrReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset_id: str
    asset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str
    byte_size: int = Field(gt=0, le=MAX_SCREENSHOT_BYTES)
    engine: str
    engine_version: str
    observed_at: datetime
    lines: list[OcrTextLine]


class AssetCleanupReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    receipt_id: str
    asset_id: str
    terminal_reason: str
    cleanup_status: str
    asset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cleanup_attempted_at: datetime
    cleanup_error_category: str | None = None


class ScreenshotImportResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    itinerary_import: ItineraryImport
    ocr_receipts: list[ScreenshotOcrReceipt]
    cleanup_receipts: list[AssetCleanupReceipt]


class TemporaryAssetRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset_id: str
    workspace_id: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str
    byte_size: int = Field(gt=0, le=MAX_SCREENSHOT_BYTES)
    storage_locator: str
    state: str = "PENDING"
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class ScreenshotUpload:
    media_type: str
    content: bytes


class OcrEngine(Protocol):
    name: str
    version: str

    async def recognize(self, image_path: Path) -> list[OcrTextLine]: ...


class ScreenshotAssetRepository(Protocol):
    async def create_assets(self, assets: list[TemporaryAssetRecord]) -> None: ...

    async def mark_processing(self, asset_id: str) -> None: ...

    async def record_cleanup(self, receipt: AssetCleanupReceipt) -> AssetCleanupReceipt: ...

    async def attach_ocr_artifacts(
        self,
        import_id: str,
        receipts: list[ScreenshotOcrReceipt],
        *,
        conn: Any | None = None,
    ) -> None: ...

    async def get_ocr_artifacts(self, import_id: str) -> list[ScreenshotOcrReceipt]: ...

    async def list_expired_assets(self, *, now: datetime) -> list[TemporaryAssetRecord]: ...


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class PostgresScreenshotAssetRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def create_assets(self, assets: list[TemporaryAssetRecord]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            for asset in assets:
                await conn.execute(
                    """
                    INSERT INTO trip_temporary_assets (
                        asset_id, workspace_id, content_hash, media_type, byte_size,
                        storage_locator, state, expires_at, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, 'PENDING', $7, $8, $8)
                    """,
                    asset.asset_id,
                    asset.workspace_id,
                    asset.content_hash,
                    asset.media_type,
                    asset.byte_size,
                    asset.storage_locator,
                    asset.expires_at,
                    asset.created_at,
                )

    async def mark_processing(self, asset_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE trip_temporary_assets
                SET state = 'PROCESSING', updated_at = NOW()
                WHERE asset_id = $1 AND state = 'PENDING'
                """,
                asset_id,
            )

    async def record_cleanup(self, receipt: AssetCleanupReceipt) -> AssetCleanupReceipt:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                "SELECT * FROM temporary_asset_cleanup_receipts WHERE asset_id = $1 FOR UPDATE",
                receipt.asset_id,
            )
            if existing is not None:
                return AssetCleanupReceipt(
                    receipt_id=existing["receipt_id"],
                    asset_id=existing["asset_id"],
                    terminal_reason=existing["terminal_reason"],
                    cleanup_status=existing["cleanup_status"],
                    asset_hash=existing["asset_hash"].strip(),
                    cleanup_attempted_at=existing["cleanup_attempted_at"],
                    cleanup_error_category=existing["cleanup_error_category"],
                )
            await conn.execute(
                """
                INSERT INTO temporary_asset_cleanup_receipts (
                    receipt_id, asset_id, terminal_reason, cleanup_status, asset_hash,
                    cleanup_attempted_at, cleanup_error_category
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                receipt.receipt_id,
                receipt.asset_id,
                receipt.terminal_reason,
                receipt.cleanup_status,
                receipt.asset_hash,
                receipt.cleanup_attempted_at,
                receipt.cleanup_error_category,
            )
            await conn.execute(
                """
                UPDATE trip_temporary_assets
                SET state = $2,
                    storage_locator = CASE WHEN $2 = 'CLEANED' THEN 'deleted://' || asset_id ELSE storage_locator END,
                    updated_at = NOW()
                WHERE asset_id = $1
                """,
                receipt.asset_id,
                "CLEANED" if receipt.cleanup_status == "DELETED" else "CLEANUP_FAILED",
            )
        return receipt

    async def attach_ocr_artifacts(
        self,
        import_id: str,
        receipts: list[ScreenshotOcrReceipt],
        *,
        conn: Any | None = None,
    ) -> None:
        if conn is None:
            pool = await self._get_pool()
            async with pool.acquire() as acquired, acquired.transaction():
                await self.attach_ocr_artifacts(import_id, receipts, conn=acquired)
                return
        result = await conn.execute(
            """
            UPDATE itinerary_imports
            SET parsed_json = parsed_json || jsonb_build_object('ocr_artifacts', $2::jsonb)
            WHERE import_id = $1
            """,
            import_id,
            json.dumps([item.model_dump(mode="json") for item in receipts], ensure_ascii=False),
        )
        if result != "UPDATE 1":
            raise ResourceNotFound("import does not exist while attaching OCR artifacts")

    async def get_ocr_artifacts(self, import_id: str) -> list[ScreenshotOcrReceipt]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT parsed_json->'ocr_artifacts' FROM itinerary_imports WHERE import_id = $1",
                import_id,
            )
        if value is None:
            return []
        return [ScreenshotOcrReceipt.model_validate(item) for item in (_json_value(value) or [])]

    async def list_expired_assets(self, *, now: datetime) -> list[TemporaryAssetRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM trip_temporary_assets
                WHERE state IN ('PENDING', 'PROCESSING', 'CLEANUP_FAILED') AND expires_at <= $1
                ORDER BY created_at, asset_id
                """,
                now,
            )
        return [
            TemporaryAssetRecord(
                asset_id=row["asset_id"],
                workspace_id=row["workspace_id"],
                content_hash=row["content_hash"].strip(),
                media_type=row["media_type"],
                byte_size=row["byte_size"],
                storage_locator=row["storage_locator"],
                state=row["state"],
                expires_at=row["expires_at"],
                created_at=row["created_at"],
            )
            for row in rows
        ]


class InMemoryScreenshotAssetRepository:
    def __init__(self):
        self.assets: dict[str, TemporaryAssetRecord] = {}
        self.cleanup_receipts: dict[str, AssetCleanupReceipt] = {}
        self.ocr_artifacts: dict[str, list[ScreenshotOcrReceipt]] = {}

    async def create_assets(self, assets: list[TemporaryAssetRecord]) -> None:
        for asset in assets:
            if asset.asset_id in self.assets:
                raise ValueError("temporary asset already exists")
            self.assets[asset.asset_id] = asset

    async def mark_processing(self, asset_id: str) -> None:
        asset = self.assets[asset_id]
        self.assets[asset_id] = asset.model_copy(update={"state": "PROCESSING"})

    async def record_cleanup(self, receipt: AssetCleanupReceipt) -> AssetCleanupReceipt:
        existing = self.cleanup_receipts.get(receipt.asset_id)
        if existing is not None:
            return existing
        self.cleanup_receipts[receipt.asset_id] = receipt
        asset = self.assets[receipt.asset_id]
        self.assets[receipt.asset_id] = asset.model_copy(
            update={
                "state": "CLEANED" if receipt.cleanup_status == "DELETED" else "CLEANUP_FAILED",
                "storage_locator": (
                    f"deleted://{receipt.asset_id}"
                    if receipt.cleanup_status == "DELETED"
                    else asset.storage_locator
                ),
            }
        )
        return receipt

    async def attach_ocr_artifacts(
        self,
        import_id: str,
        receipts: list[ScreenshotOcrReceipt],
        *,
        conn: Any | None = None,
    ) -> None:
        self.ocr_artifacts[import_id] = list(receipts)

    async def get_ocr_artifacts(self, import_id: str) -> list[ScreenshotOcrReceipt]:
        return list(self.ocr_artifacts.get(import_id, []))

    async def list_expired_assets(self, *, now: datetime) -> list[TemporaryAssetRecord]:
        return [
            asset
            for asset in self.assets.values()
            if asset.state in {"PENDING", "PROCESSING", "CLEANUP_FAILED"} and asset.expires_at <= now
        ]


class PaddleOcrEngine:
    name = "paddleocr"
    version = "3.7.0"

    def __init__(self, *, confirmation_threshold: float = 0.85):
        self.confirmation_threshold = confirmation_threshold
        self._pipeline: Any | None = None

    def _predict(self, image_path: Path) -> Any:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise OcrProcessingError("PaddleOCR runtime is not installed") from exc
        if self._pipeline is None:
            self._pipeline = PaddleOCR(
                lang="ch",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        return self._pipeline.predict(str(image_path))

    @staticmethod
    def _payload(result: Any) -> dict[str, Any]:
        candidate = getattr(result, "json", result)
        if callable(candidate):
            candidate = candidate()
        candidate = _json_value(candidate)
        if isinstance(candidate, dict) and isinstance(candidate.get("res"), dict):
            return candidate["res"]
        if not isinstance(candidate, dict):
            raise OcrProcessingError("PaddleOCR returned an unsupported result shape")
        return candidate

    async def recognize(self, image_path: Path) -> list[OcrTextLine]:
        try:
            results = await asyncio.to_thread(self._predict, image_path)
            lines: list[OcrTextLine] = []
            for result in results:
                payload = self._payload(result)
                texts = list(payload.get("rec_texts") or [])
                scores = list(payload.get("rec_scores") or [])
                boxes = list(payload.get("rec_boxes") or [])
                if not (len(texts) == len(scores) == len(boxes)):
                    raise OcrProcessingError("PaddleOCR result fields have inconsistent lengths")
                for text, score, box in zip(texts, scores, boxes, strict=True):
                    normalized = str(text).strip()
                    if not normalized:
                        continue
                    coords = [int(item) for item in list(box)]
                    if len(coords) != 4:
                        raise OcrProcessingError("PaddleOCR rectangle must contain four coordinates")
                    confidence = float(score)
                    lines.append(
                        OcrTextLine(
                            text=normalized,
                            confidence=confidence,
                            box=OcrBoundingBox(
                                x_min=coords[0],
                                y_min=coords[1],
                                x_max=coords[2],
                                y_max=coords[3],
                            ),
                            requires_confirmation=confidence < self.confirmation_threshold,
                        )
                    )
            return lines
        except (OcrProcessingError, PrivacyBlockedError):
            raise
        except Exception as exc:
            raise OcrProcessingError("PaddleOCR processing failed") from exc


def validate_screenshot_batch(uploads: list[ScreenshotUpload]) -> None:
    if not 1 <= len(uploads) <= MAX_SCREENSHOTS:
        raise ScreenshotBatchInvalidError("screenshot batch must contain 1 to 6 images")
    for upload in uploads:
        expected_suffix = SUPPORTED_MEDIA_TYPES.get(upload.media_type)
        if expected_suffix is None:
            raise ScreenshotBatchInvalidError("screenshots must be PNG, JPEG or WebP")
        if not 0 < len(upload.content) <= MAX_SCREENSHOT_BYTES:
            raise ScreenshotBatchInvalidError("each screenshot must contain at most 10MB")
        is_valid = (
            expected_suffix == ".png"
            and upload.content.startswith(b"\x89PNG\r\n\x1a\n")
            or expected_suffix == ".jpg"
            and upload.content.startswith(b"\xff\xd8\xff")
            or expected_suffix == ".webp"
            and len(upload.content) >= 12
            and upload.content.startswith(b"RIFF")
            and upload.content[8:12] == b"WEBP"
        )
        if not is_valid:
            raise ScreenshotBatchInvalidError("declared screenshot media type does not match its bytes")


class ScreenshotAssetCleanupService:
    def __init__(
        self,
        asset_repository: ScreenshotAssetRepository,
        *,
        temp_root: Path | None = None,
    ):
        self.asset_repository = asset_repository
        self.temp_root = (temp_root or Path(tempfile.gettempdir()) / "breezetravel-screenshots").resolve()

    async def cleanup(
        self,
        asset: TemporaryAssetRecord,
        *,
        terminal_reason: str,
    ) -> AssetCleanupReceipt:
        attempted_at = datetime.now(timezone.utc)
        path = Path(asset.storage_locator).resolve()
        error_category: str | None = None
        try:
            if path.parent != self.temp_root:
                raise PermissionError("asset path is outside screenshot temp root")
            path.unlink(missing_ok=True)
            status = "DELETED"
        except OSError as exc:
            status = "DELETE_FAILED"
            error_category = type(exc).__name__.upper()
        receipt = AssetCleanupReceipt(
            receipt_id=str(uuid5(NAMESPACE_URL, f"breezetravel:asset-cleanup:{asset.asset_id}")),
            asset_id=asset.asset_id,
            terminal_reason=terminal_reason,
            cleanup_status=status,
            asset_hash=asset.content_hash,
            cleanup_attempted_at=attempted_at,
            cleanup_error_category=error_category,
        )
        return await self.asset_repository.record_cleanup(receipt)

    async def recover_expired(self, *, now: datetime | None = None) -> list[AssetCleanupReceipt]:
        current = now or datetime.now(timezone.utc)
        assets = await self.asset_repository.list_expired_assets(now=current)
        return [await self.cleanup(asset, terminal_reason="TIMED_OUT") for asset in assets]


class ScreenshotImportService:
    def __init__(
        self,
        *,
        import_repository: ImportRepository,
        itinerary_repository: ItineraryRepository,
        trip_brief_repository: TripBriefRepository,
        entity_resolver: Any,
        command_repository: CreationCommandRepository,
        asset_repository: ScreenshotAssetRepository,
        ocr_engine: OcrEngine,
        parser: ItineraryTextParser | None = None,
        temp_root: Path | None = None,
        asset_ttl: timedelta = timedelta(minutes=15),
    ):
        self.import_repository = import_repository
        self.itinerary_repository = itinerary_repository
        self.trip_brief_repository = trip_brief_repository
        self.entity_resolver = entity_resolver
        self.command_repository = command_repository
        self.asset_repository = asset_repository
        self.ocr_engine = ocr_engine
        self.parser = parser or ItineraryTextParser()
        self.temp_root = (temp_root or Path(tempfile.gettempdir()) / "breezetravel-screenshots").resolve()
        self.cleanup_service = ScreenshotAssetCleanupService(asset_repository, temp_root=self.temp_root)
        self.asset_ttl = asset_ttl

    def _safe_asset_path(self, asset_id: str, media_type: str) -> Path:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        path = (self.temp_root / f"{asset_id}{SUPPORTED_MEDIA_TYPES[media_type]}").resolve()
        if path.parent != self.temp_root:
            raise RuntimeError("temporary asset path escaped screenshot root")
        return path

    async def create_import(
        self,
        *,
        workspace_id: str,
        uploads: list[ScreenshotUpload],
        actor_user_id: str,
        idempotency_key: str,
    ) -> tuple[ScreenshotImportResult, bool]:
        validate_screenshot_batch(uploads)
        workspace = await self.itinerary_repository.get_workspace(workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        upload_facts = [
            {
                "media_type": upload.media_type,
                "byte_size": len(upload.content),
                "content_hash": hashlib.sha256(upload.content).hexdigest(),
            }
            for upload in uploads
        ]
        request_hash = sha256_canonical(
            {
                "schema_version": 1,
                "operation": "CREATE_SCREENSHOT_IMPORT",
                "workspace_id": workspace_id,
                "actor_user_id": actor_user_id,
                "images": upload_facts,
            }
        )
        claim = await self.command_repository.claim(
            workspace_id=workspace_id,
            operation=CreationOperation.CREATE_IMPORT,
            target_id=workspace_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            basis={"current_import_id": workspace.current_import_id},
        )
        if claim.replay is not None:
            return ScreenshotImportResult.model_validate(claim.replay.body), True

        now = datetime.now(timezone.utc)
        assets: list[TemporaryAssetRecord] = []
        try:
            for upload, fact in zip(uploads, upload_facts, strict=True):
                asset_id = str(uuid4())
                path = self._safe_asset_path(asset_id, upload.media_type)
                path.write_bytes(upload.content)
                assets.append(
                    TemporaryAssetRecord(
                        asset_id=asset_id,
                        workspace_id=workspace_id,
                        content_hash=fact["content_hash"],
                        media_type=upload.media_type,
                        byte_size=fact["byte_size"],
                        storage_locator=str(path),
                        expires_at=now + self.asset_ttl,
                        created_at=now,
                    )
                )
            await self.asset_repository.create_assets(assets)
        except Exception:
            for asset in assets:
                Path(asset.storage_locator).unlink(missing_ok=True)
            await self.command_repository.abandon(claim)
            raise

        ocr_receipts: list[ScreenshotOcrReceipt] = []
        processing_error: Exception | None = None
        try:
            for asset in assets:
                await self.asset_repository.mark_processing(asset.asset_id)
                lines = await self.ocr_engine.recognize(Path(asset.storage_locator))
                ocr_receipts.append(
                    ScreenshotOcrReceipt(
                        asset_id=asset.asset_id,
                        asset_hash=asset.content_hash,
                        media_type=asset.media_type,
                        byte_size=asset.byte_size,
                        engine=self.ocr_engine.name,
                        engine_version=self.ocr_engine.version,
                        observed_at=datetime.now(timezone.utc),
                        lines=lines,
                    )
                )
        except Exception as exc:
            processing_error = exc

        cleanup_receipts = [
            await self.cleanup_service.cleanup(
                asset,
                terminal_reason="FAILED" if processing_error is not None else "SUCCEEDED",
            )
            for asset in assets
        ]
        failed_cleanup = [item for item in cleanup_receipts if item.cleanup_status != "DELETED"]
        if failed_cleanup:
            await self.command_repository.abandon(claim)
            raise PrivacyBlockedError(
                "one or more screenshot originals could not be deleted",
                context={"asset_ids": [item.asset_id for item in failed_cleanup]},
            )
        if processing_error is not None:
            await self.command_repository.abandon(claim)
            if isinstance(processing_error, OcrProcessingError):
                raise processing_error
            raise OcrProcessingError("screenshot OCR failed") from processing_error

        raw_text = "\n".join(line.text for receipt in ocr_receipts for line in receipt.lines).strip()
        if not raw_text:
            await self.command_repository.abandon(claim)
            raise OcrProcessingError("screenshot OCR produced no text")
        try:
            import_id = str(uuid4())
            draft = self.parser.parse(raw_text, import_id=import_id)
            resolutions = (
                await self.entity_resolver.resolve_all(draft.raw_stops, city=workspace.city) if draft.raw_stops else []
            )
            source_confidence = min(
                (line.confidence for receipt in ocr_receipts for line in receipt.lines),
                default=0.0,
            )
            if any(line.requires_confirmation for receipt in ocr_receipts for line in receipt.lines):
                resolutions = [
                    item.model_copy(
                        update={
                            "canonical_place_id": None,
                            "confidence": min(item.confidence, source_confidence),
                            "resolution_status": ResolutionStatus.AMBIGUOUS,
                        }
                    )
                    for item in resolutions
                ]
            ready = resolution_set_is_ready(draft.raw_stops, resolutions)
            status = (
                ImportStatus.FAILED
                if not draft.raw_stops
                else ImportStatus.READY
                if ready
                else ImportStatus.NEEDS_RESOLUTION
            )
            itinerary_import = ItineraryImport(
                import_id=import_id,
                workspace_id=workspace_id,
                source_type=ImportSourceType.AI_TEXT,
                raw_text=raw_text,
                parse_version=f"{self.parser.version}+{self.ocr_engine.name}-{self.ocr_engine.version}",
                status=status,
                raw_stops=draft.raw_stops,
                resolutions=resolutions,
                member_summary=draft.member_summary,
                parse_errors=draft.errors,
                state_version=2 if draft.raw_stops else 1,
                created_by=actor_user_id,
                created_at=now,
                updated_at=now,
            )

            async def finalize(conn: Any, stored_basis: dict[str, Any]) -> CreationCommandResponse:
                stored = await self.import_repository.create_import_bundle(
                    itinerary_import,
                    basis=stored_basis,
                    conn=conn,
                )
                await self.asset_repository.attach_ocr_artifacts(import_id, ocr_receipts, conn=conn)
                await TripBriefApplicationService(self.trip_brief_repository).create_for_import(
                    workspace=workspace,
                    itinerary_import=stored,
                    actor_user_id=actor_user_id,
                    conn=conn,
                    source_confidence=source_confidence,
                )
                result = ScreenshotImportResult(
                    itinerary_import=stored,
                    ocr_receipts=ocr_receipts,
                    cleanup_receipts=cleanup_receipts,
                )
                return CreationCommandResponse(
                    status_code=201,
                    body=result.model_dump(mode="json"),
                    headers={"ETag": f'"{stored.state_version}"', "Cache-Control": "no-store"},
                )

            response = await self.command_repository.finalize(claim, finalize)
            return ScreenshotImportResult.model_validate(response.body), response.idempotent_replay
        except Exception:
            await self.command_repository.abandon(claim)
            raise

    async def recover_expired_assets(self, *, now: datetime | None = None) -> list[AssetCleanupReceipt]:
        return await self.cleanup_service.recover_expired(now=now)
