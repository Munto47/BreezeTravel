from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from app.db.connection import get_pool
from app.importing.errors import (
    PrivacyBlockedError,
    ScreenshotBatchStateConflictError,
    ScreenshotBatchVersionConflictError,
)
from app.importing.screenshots import (
    SUPPORTED_MEDIA_TYPES,
    AssetCleanupReceipt,
    ScreenshotAssetCleanupService,
    ScreenshotAssetRepository,
    ScreenshotImportResult,
    ScreenshotImportService,
    ScreenshotUpload,
    TemporaryAssetRecord,
    validate_screenshot_batch,
)
from app.itineraries.errors import IdempotencyKeyReusedError, ResourceNotFound, ResourceScopeDenied
from app.itineraries.hash_service import sha256_canonical


class ScreenshotUploadBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    batch_id: str
    workspace_id: str
    expected_count: int = Field(ge=1, le=6)
    uploaded_positions: list[int] = Field(default_factory=list)
    status: str
    version: int = Field(gt=0)
    result_import_id: str | None = None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class ScreenshotUploadBatchCommitResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    batch: ScreenshotUploadBatch
    import_result: ScreenshotImportResult


class ScreenshotUploadBatchCancelResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    batch: ScreenshotUploadBatch
    cleanup_receipts: list[AssetCleanupReceipt]


@dataclass(frozen=True)
class BatchCommandReplay:
    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True)
class BatchTransition:
    batch: ScreenshotUploadBatch | None = None
    assets: list[TemporaryAssetRecord] | None = None
    replay: BatchCommandReplay | None = None


class ScreenshotUploadBatchRepository(Protocol):
    async def create(
        self,
        *,
        workspace_id: str,
        actor_user_id: str,
        expected_count: int,
        expires_at: datetime,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ScreenshotUploadBatch, bool]: ...

    async def store_file(
        self,
        *,
        batch_id: str,
        workspace_id: str,
        actor_user_id: str,
        position: int,
        expected_version: int,
        asset: TemporaryAssetRecord,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ScreenshotUploadBatch, bool]: ...

    async def begin_terminal(
        self,
        *,
        operation: str,
        batch_id: str,
        workspace_id: str,
        actor_user_id: str,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> BatchTransition: ...

    async def finish_terminal(
        self,
        *,
        operation: str,
        batch_id: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        status: str,
        result_import_id: str | None,
        response_status: int,
        response_payload: dict[str, Any],
    ) -> ScreenshotUploadBatch: ...

    async def list_expired(self, *, now: datetime) -> list[ScreenshotUploadBatch]: ...

    async def mark_recovered(self, *, batch_id: str, privacy_blocked: bool) -> None: ...


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _batch_from_row(row: Any) -> ScreenshotUploadBatch:
    return ScreenshotUploadBatch(
        batch_id=row["batch_id"],
        workspace_id=row["workspace_id"],
        expected_count=row["expected_count"],
        uploaded_positions=list(row["uploaded_positions"] or []),
        status=row["status"],
        version=row["version"],
        result_import_id=row["result_import_id"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_BATCH_SELECT = """
SELECT batch.*,
       COALESCE(
           ARRAY_AGG(asset.upload_position ORDER BY asset.upload_position)
               FILTER (WHERE asset.upload_position IS NOT NULL),
           ARRAY[]::SMALLINT[]
       ) AS uploaded_positions
FROM screenshot_upload_batches AS batch
LEFT JOIN trip_temporary_assets AS asset ON asset.upload_batch_id = batch.batch_id
WHERE batch.batch_id = $1
GROUP BY batch.batch_id
"""


class PostgresScreenshotUploadBatchRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    @staticmethod
    async def _replay(
        conn: Any,
        *,
        batch_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> BatchCommandReplay | None:
        row = await conn.fetchrow(
            """
            SELECT operation, request_hash, response_status, response_json
            FROM screenshot_upload_commands
            WHERE batch_id = $1 AND idempotency_key = $2
            """,
            batch_id,
            idempotency_key,
        )
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"].strip() != request_hash:
            raise IdempotencyKeyReusedError("idempotency key was already used with a different request")
        return BatchCommandReplay(row["response_status"], dict(_json_value(row["response_json"])))

    @staticmethod
    async def _batch(conn: Any, batch_id: str) -> ScreenshotUploadBatch:
        row = await conn.fetchrow(_BATCH_SELECT, batch_id)
        if row is None:
            raise ResourceNotFound("screenshot upload batch does not exist")
        return _batch_from_row(row)

    async def create(
        self,
        *,
        workspace_id: str,
        actor_user_id: str,
        expected_count: int,
        expires_at: datetime,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ScreenshotUploadBatch, bool]:
        batch_id = str(
            uuid5(NAMESPACE_URL, f"breezetravel:screenshot-batch:{workspace_id}:{actor_user_id}:{idempotency_key}")
        )
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            workspace = await conn.fetchrow(
                "SELECT workspace_id FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                workspace_id,
            )
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            replay = await self._replay(
                conn,
                batch_id=batch_id,
                operation="CREATE_BATCH",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return ScreenshotUploadBatch.model_validate(replay.body), True
            await conn.execute(
                """
                INSERT INTO screenshot_upload_batches (
                    batch_id, workspace_id, actor_user_id, expected_count,
                    status, version, expires_at
                ) VALUES ($1, $2, $3, $4, 'PENDING', 1, $5)
                """,
                batch_id,
                workspace_id,
                actor_user_id,
                expected_count,
                expires_at,
            )
            batch = await self._batch(conn, batch_id)
            body = batch.model_dump(mode="json")
            await conn.execute(
                """
                INSERT INTO screenshot_upload_commands (
                    command_id, batch_id, actor_user_id, operation, idempotency_key,
                    request_hash, response_status, response_json
                ) VALUES ($1, $2, $3, 'CREATE_BATCH', $4, $5, 201, $6::jsonb)
                """,
                str(uuid4()),
                batch_id,
                actor_user_id,
                idempotency_key,
                request_hash,
                json.dumps(body, ensure_ascii=False),
            )
            return batch, False

    async def store_file(
        self,
        *,
        batch_id: str,
        workspace_id: str,
        actor_user_id: str,
        position: int,
        expected_version: int,
        asset: TemporaryAssetRecord,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ScreenshotUploadBatch, bool]:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM screenshot_upload_batches WHERE batch_id = $1 FOR UPDATE",
                batch_id,
            )
            if row is None:
                raise ResourceNotFound("screenshot upload batch does not exist")
            if row["workspace_id"] != workspace_id or row["actor_user_id"] != actor_user_id:
                raise ResourceScopeDenied("screenshot upload batch is outside the current actor scope")
            replay = await self._replay(
                conn,
                batch_id=batch_id,
                operation="UPLOAD_FILE",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return ScreenshotUploadBatch.model_validate(replay.body), True
            self._require_pending(row, expected_version)
            if position >= row["expected_count"]:
                raise ScreenshotBatchStateConflictError("screenshot position exceeds expected batch size")
            exists = await conn.fetchval(
                "SELECT 1 FROM trip_temporary_assets WHERE upload_batch_id = $1 AND upload_position = $2",
                batch_id,
                position,
            )
            if exists:
                raise ScreenshotBatchStateConflictError("screenshot position is already occupied")
            await conn.execute(
                """
                INSERT INTO trip_temporary_assets (
                    asset_id, workspace_id, content_hash, media_type, byte_size,
                    storage_locator, state, upload_batch_id, upload_position,
                    expires_at, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, 'PENDING', $7, $8, $9, $10, $10)
                """,
                asset.asset_id,
                asset.workspace_id,
                asset.content_hash,
                asset.media_type,
                asset.byte_size,
                asset.storage_locator,
                batch_id,
                position,
                asset.expires_at,
                asset.created_at,
            )
            await conn.execute(
                "UPDATE screenshot_upload_batches SET version = version + 1, updated_at = NOW() WHERE batch_id = $1",
                batch_id,
            )
            batch = await self._batch(conn, batch_id)
            body = batch.model_dump(mode="json")
            await self._insert_command(
                conn,
                batch_id=batch_id,
                actor_user_id=actor_user_id,
                operation="UPLOAD_FILE",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_status=200,
                response_payload=body,
            )
            return batch, False

    @staticmethod
    def _require_pending(row: Any, expected_version: int) -> None:
        if row["version"] != expected_version:
            raise ScreenshotBatchVersionConflictError(
                "screenshot upload batch version changed",
                context={"expected_version": expected_version, "actual_version": row["version"]},
            )
        if row["status"] != "PENDING":
            raise ScreenshotBatchStateConflictError("screenshot upload batch is not pending")
        if row["expires_at"] <= datetime.now(timezone.utc):
            raise ScreenshotBatchStateConflictError("screenshot upload batch has expired")

    async def begin_terminal(
        self,
        *,
        operation: str,
        batch_id: str,
        workspace_id: str,
        actor_user_id: str,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> BatchTransition:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM screenshot_upload_batches WHERE batch_id = $1 FOR UPDATE",
                batch_id,
            )
            if row is None:
                raise ResourceNotFound("screenshot upload batch does not exist")
            if row["workspace_id"] != workspace_id or row["actor_user_id"] != actor_user_id:
                raise ResourceScopeDenied("screenshot upload batch is outside the current actor scope")
            replay = await self._replay(
                conn,
                batch_id=batch_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return BatchTransition(replay=replay)
            self._require_pending(row, expected_version)
            assets = await conn.fetch(
                "SELECT * FROM trip_temporary_assets WHERE upload_batch_id = $1 ORDER BY upload_position",
                batch_id,
            )
            if operation == "COMMIT" and len(assets) != row["expected_count"]:
                raise ScreenshotBatchStateConflictError(
                    "all expected screenshots must be uploaded before commit",
                    context={"expected_count": row["expected_count"], "uploaded_count": len(assets)},
                )
            await conn.execute(
                """
                UPDATE screenshot_upload_batches
                SET status = 'PROCESSING', version = version + 1, updated_at = NOW()
                WHERE batch_id = $1
                """,
                batch_id,
            )
            batch = await self._batch(conn, batch_id)
            records = [
                TemporaryAssetRecord(
                    asset_id=asset["asset_id"],
                    workspace_id=asset["workspace_id"],
                    content_hash=asset["content_hash"].strip(),
                    media_type=asset["media_type"],
                    byte_size=asset["byte_size"],
                    storage_locator=asset["storage_locator"],
                    state=asset["state"],
                    upload_batch_id=asset["upload_batch_id"],
                    upload_position=asset["upload_position"],
                    expires_at=asset["expires_at"],
                    created_at=asset["created_at"],
                )
                for asset in assets
            ]
            return BatchTransition(batch=batch, assets=records)

    @staticmethod
    async def _insert_command(
        conn: Any,
        *,
        batch_id: str,
        actor_user_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        response_status: int,
        response_payload: dict[str, Any],
    ) -> None:
        await conn.execute(
            """
            INSERT INTO screenshot_upload_commands (
                command_id, batch_id, actor_user_id, operation, idempotency_key,
                request_hash, response_status, response_json
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            """,
            str(uuid4()),
            batch_id,
            actor_user_id,
            operation,
            idempotency_key,
            request_hash,
            response_status,
            json.dumps(response_payload, ensure_ascii=False),
        )

    async def finish_terminal(
        self,
        *,
        operation: str,
        batch_id: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        status: str,
        result_import_id: str | None,
        response_status: int,
        response_payload: dict[str, Any],
    ) -> ScreenshotUploadBatch:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM screenshot_upload_batches WHERE batch_id = $1 FOR UPDATE",
                batch_id,
            )
            if row is None:
                raise ResourceNotFound("screenshot upload batch does not exist")
            if row["actor_user_id"] != actor_user_id:
                raise ResourceScopeDenied("screenshot upload batch is outside the current actor scope")
            if row["status"] != "PROCESSING":
                raise ScreenshotBatchStateConflictError("screenshot upload batch terminal transition was lost")
            await conn.execute(
                """
                UPDATE screenshot_upload_batches
                SET status = $2, result_import_id = $3, version = version + 1, updated_at = NOW()
                WHERE batch_id = $1
                """,
                batch_id,
                status,
                result_import_id,
            )
            batch = await self._batch(conn, batch_id)
            payload = dict(response_payload)
            payload["batch"] = batch.model_dump(mode="json")
            await self._insert_command(
                conn,
                batch_id=batch_id,
                actor_user_id=actor_user_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_status=response_status,
                response_payload=payload,
            )
            return batch

    async def list_expired(self, *, now: datetime) -> list[ScreenshotUploadBatch]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            ids = await conn.fetch(
                """
                SELECT batch_id FROM screenshot_upload_batches
                WHERE status IN ('PENDING', 'PROCESSING', 'PRIVACY_BLOCKED') AND expires_at <= $1
                ORDER BY created_at, batch_id
                """,
                now,
            )
            return [await self._batch(conn, row["batch_id"]) for row in ids]

    async def mark_recovered(self, *, batch_id: str, privacy_blocked: bool) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE screenshot_upload_batches
                SET status = $2, version = version + 1, updated_at = NOW()
                WHERE batch_id = $1 AND status IN ('PENDING', 'PROCESSING', 'PRIVACY_BLOCKED')
                """,
                batch_id,
                "PRIVACY_BLOCKED" if privacy_blocked else "EXPIRED",
            )


class InMemoryScreenshotUploadBatchRepository:
    def __init__(self):
        self.batches: dict[str, ScreenshotUploadBatch] = {}
        self.assets: dict[str, list[TemporaryAssetRecord]] = {}
        self.actors: dict[str, str] = {}
        self.commands: dict[tuple[str, str], tuple[str, str, BatchCommandReplay]] = {}

    def _existing(self, batch_id: str, key: str, operation: str, request_hash: str) -> BatchCommandReplay | None:
        existing = self.commands.get((batch_id, key))
        if existing is None:
            return None
        stored_operation, stored_hash, replay = existing
        if stored_operation != operation or stored_hash != request_hash:
            raise IdempotencyKeyReusedError("idempotency key was already used with a different request")
        return replay

    def _scope(self, batch_id: str, workspace_id: str, actor_user_id: str) -> ScreenshotUploadBatch:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise ResourceNotFound("screenshot upload batch does not exist")
        if batch.workspace_id != workspace_id or self.actors[batch_id] != actor_user_id:
            raise ResourceScopeDenied("screenshot upload batch is outside the current actor scope")
        return batch

    async def create(self, **kwargs: Any) -> tuple[ScreenshotUploadBatch, bool]:
        batch_id = str(uuid5(NAMESPACE_URL, f"breezetravel:screenshot-batch:{kwargs['workspace_id']}:{kwargs['actor_user_id']}:{kwargs['idempotency_key']}"))
        replay = self._existing(batch_id, kwargs["idempotency_key"], "CREATE_BATCH", kwargs["request_hash"])
        if replay is not None:
            return ScreenshotUploadBatch.model_validate(replay.body), True
        now = datetime.now(timezone.utc)
        batch = ScreenshotUploadBatch(
            batch_id=batch_id,
            workspace_id=kwargs["workspace_id"],
            expected_count=kwargs["expected_count"],
            status="PENDING",
            version=1,
            expires_at=kwargs["expires_at"],
            created_at=now,
            updated_at=now,
        )
        self.batches[batch_id] = batch
        self.actors[batch_id] = kwargs["actor_user_id"]
        self.assets[batch_id] = []
        body = batch.model_dump(mode="json")
        self.commands[(batch_id, kwargs["idempotency_key"])] = (
            "CREATE_BATCH",
            kwargs["request_hash"],
            BatchCommandReplay(201, body),
        )
        return batch, False

    async def store_file(self, **kwargs: Any) -> tuple[ScreenshotUploadBatch, bool]:
        batch = self._scope(kwargs["batch_id"], kwargs["workspace_id"], kwargs["actor_user_id"])
        replay = self._existing(kwargs["batch_id"], kwargs["idempotency_key"], "UPLOAD_FILE", kwargs["request_hash"])
        if replay is not None:
            return ScreenshotUploadBatch.model_validate(replay.body), True
        self._require(batch, kwargs["expected_version"])
        if kwargs["position"] >= batch.expected_count:
            raise ScreenshotBatchStateConflictError("screenshot position exceeds expected batch size")
        if any(item.upload_position == kwargs["position"] for item in self.assets[batch.batch_id]):
            raise ScreenshotBatchStateConflictError("screenshot position is already occupied")
        self.assets[batch.batch_id].append(kwargs["asset"])
        updated = batch.model_copy(
            update={
                "uploaded_positions": sorted([*batch.uploaded_positions, kwargs["position"]]),
                "version": batch.version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.batches[batch.batch_id] = updated
        body = updated.model_dump(mode="json")
        self.commands[(batch.batch_id, kwargs["idempotency_key"])] = (
            "UPLOAD_FILE",
            kwargs["request_hash"],
            BatchCommandReplay(200, body),
        )
        return updated, False

    @staticmethod
    def _require(batch: ScreenshotUploadBatch, expected_version: int) -> None:
        if batch.version != expected_version:
            raise ScreenshotBatchVersionConflictError(
                "screenshot upload batch version changed",
                context={"expected_version": expected_version, "actual_version": batch.version},
            )
        if batch.status != "PENDING" or batch.expires_at <= datetime.now(timezone.utc):
            raise ScreenshotBatchStateConflictError("screenshot upload batch is not pending")

    async def begin_terminal(self, **kwargs: Any) -> BatchTransition:
        batch = self._scope(kwargs["batch_id"], kwargs["workspace_id"], kwargs["actor_user_id"])
        replay = self._existing(
            batch.batch_id,
            kwargs["idempotency_key"],
            kwargs["operation"],
            kwargs["request_hash"],
        )
        if replay is not None:
            return BatchTransition(replay=replay)
        self._require(batch, kwargs["expected_version"])
        assets = list(self.assets[batch.batch_id])
        if kwargs["operation"] == "COMMIT" and len(assets) != batch.expected_count:
            raise ScreenshotBatchStateConflictError(
                "all expected screenshots must be uploaded before commit",
                context={"expected_count": batch.expected_count, "uploaded_count": len(assets)},
            )
        updated = batch.model_copy(update={"status": "PROCESSING", "version": batch.version + 1})
        self.batches[batch.batch_id] = updated
        return BatchTransition(batch=updated, assets=assets)

    async def finish_terminal(self, **kwargs: Any) -> ScreenshotUploadBatch:
        batch = self.batches[kwargs["batch_id"]]
        updated = batch.model_copy(
            update={
                "status": kwargs["status"],
                "result_import_id": kwargs["result_import_id"],
                "version": batch.version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.batches[batch.batch_id] = updated
        payload = dict(kwargs["response_payload"])
        payload["batch"] = updated.model_dump(mode="json")
        self.commands[(batch.batch_id, kwargs["idempotency_key"])] = (
            kwargs["operation"],
            kwargs["request_hash"],
            BatchCommandReplay(kwargs["response_status"], payload),
        )
        return updated

    async def list_expired(self, *, now: datetime) -> list[ScreenshotUploadBatch]:
        return [
            item
            for item in self.batches.values()
            if item.status in {"PENDING", "PROCESSING", "PRIVACY_BLOCKED"} and item.expires_at <= now
        ]

    async def mark_recovered(self, *, batch_id: str, privacy_blocked: bool) -> None:
        batch = self.batches[batch_id]
        self.batches[batch_id] = batch.model_copy(
            update={"status": "PRIVACY_BLOCKED" if privacy_blocked else "EXPIRED", "version": batch.version + 1}
        )


class ScreenshotUploadBatchService:
    def __init__(
        self,
        *,
        repository: ScreenshotUploadBatchRepository,
        asset_repository: ScreenshotAssetRepository,
        import_service: ScreenshotImportService | None = None,
        temp_root: Path | None = None,
        batch_ttl: timedelta = timedelta(minutes=15),
    ):
        self.repository = repository
        self.asset_repository = asset_repository
        self.import_service = import_service
        self.temp_root = (temp_root or Path(tempfile.gettempdir()) / "breezetravel-screenshots").resolve()
        self.batch_ttl = batch_ttl
        self.cleanup_service = ScreenshotAssetCleanupService(asset_repository, temp_root=self.temp_root)

    def _path(self, asset_id: str, media_type: str) -> Path:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        path = (self.temp_root / f"{asset_id}{SUPPORTED_MEDIA_TYPES[media_type]}").resolve()
        if path.parent != self.temp_root:
            raise RuntimeError("temporary asset path escaped screenshot root")
        return path

    async def create_batch(
        self, *, workspace_id: str, actor_user_id: str, expected_count: int, idempotency_key: str
    ) -> tuple[ScreenshotUploadBatch, bool]:
        request_hash = sha256_canonical(
            {"operation": "CREATE_BATCH", "workspace_id": workspace_id, "actor_user_id": actor_user_id, "expected_count": expected_count}
        )
        return await self.repository.create(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            expected_count=expected_count,
            expires_at=datetime.now(timezone.utc) + self.batch_ttl,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def upload_file(
        self,
        *,
        batch_id: str,
        workspace_id: str,
        actor_user_id: str,
        position: int,
        expected_version: int,
        upload: ScreenshotUpload,
        idempotency_key: str,
    ) -> tuple[ScreenshotUploadBatch, bool]:
        validate_screenshot_batch([upload])
        content_hash = hashlib.sha256(upload.content).hexdigest()
        request_hash = sha256_canonical(
            {
                "operation": "UPLOAD_FILE",
                "batch_id": batch_id,
                "workspace_id": workspace_id,
                "actor_user_id": actor_user_id,
                "position": position,
                "expected_version": expected_version,
                "content_hash": content_hash,
                "media_type": upload.media_type,
                "byte_size": len(upload.content),
            }
        )
        now = datetime.now(timezone.utc)
        asset_id = str(uuid4())
        path = self._path(asset_id, upload.media_type)
        path.write_bytes(upload.content)
        asset = TemporaryAssetRecord(
            asset_id=asset_id,
            workspace_id=workspace_id,
            content_hash=content_hash,
            media_type=upload.media_type,
            byte_size=len(upload.content),
            storage_locator=str(path),
            upload_batch_id=batch_id,
            upload_position=position,
            expires_at=now + self.batch_ttl,
            created_at=now,
        )
        try:
            batch, replayed = await self.repository.store_file(
                batch_id=batch_id,
                workspace_id=workspace_id,
                actor_user_id=actor_user_id,
                position=position,
                expected_version=expected_version,
                asset=asset,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        if replayed:
            path.unlink(missing_ok=True)
        elif hasattr(self.asset_repository, "assets"):
            # The PostgreSQL repository stores the asset atomically above. Keep the
            # in-memory asset repository aligned for OCR and cleanup unit tests.
            await self.asset_repository.create_assets([asset])
        return batch, replayed

    async def commit(
        self,
        *,
        batch_id: str,
        workspace_id: str,
        actor_user_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> tuple[ScreenshotUploadBatchCommitResult | BatchCommandReplay, bool]:
        request_hash = sha256_canonical(
            {"operation": "COMMIT", "batch_id": batch_id, "workspace_id": workspace_id, "actor_user_id": actor_user_id, "expected_version": expected_version}
        )
        transition = await self.repository.begin_terminal(
            operation="COMMIT",
            batch_id=batch_id,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if transition.replay is not None:
            return transition.replay, True
        assert transition.assets is not None
        if self.import_service is None:
            raise RuntimeError("screenshot import service is required for commit")
        try:
            result, _ = await self.import_service.create_import(
                workspace_id=workspace_id,
                staged_assets=transition.assets,
                actor_user_id=actor_user_id,
                idempotency_key=f"screenshot-batch:{batch_id}:commit",
            )
        except Exception as exc:
            cleanup_receipts = [
                await self.cleanup_service.cleanup(asset, terminal_reason="FAILED")
                for asset in transition.assets
            ]
            privacy_blocked = any(item.cleanup_status != "DELETED" for item in cleanup_receipts)
            if privacy_blocked and not isinstance(exc, PrivacyBlockedError):
                exc = PrivacyBlockedError(
                    "one or more screenshot originals could not be deleted",
                    context={
                        "asset_ids": [
                            item.asset_id
                            for item in cleanup_receipts
                            if item.cleanup_status != "DELETED"
                        ]
                    },
                )
            status_code = getattr(exc, "status_code", 503)
            detail = exc.detail() if hasattr(exc, "detail") else {"code": "OCR_PROCESSING_FAILED", "message": "screenshot OCR failed"}
            await self.repository.finish_terminal(
                operation="COMMIT",
                batch_id=batch_id,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="PRIVACY_BLOCKED" if privacy_blocked else "FAILED",
                result_import_id=None,
                response_status=status_code,
                response_payload={"detail": detail},
            )
            raise exc
        batch = await self.repository.finish_terminal(
            operation="COMMIT",
            batch_id=batch_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status="SUCCEEDED",
            result_import_id=result.itinerary_import.import_id,
            response_status=201,
            response_payload={"import_result": result.model_dump(mode="json")},
        )
        return ScreenshotUploadBatchCommitResult(batch=batch, import_result=result), False

    async def cancel(
        self,
        *,
        batch_id: str,
        workspace_id: str,
        actor_user_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> tuple[ScreenshotUploadBatchCancelResult | BatchCommandReplay, bool]:
        request_hash = sha256_canonical(
            {"operation": "CANCEL", "batch_id": batch_id, "workspace_id": workspace_id, "actor_user_id": actor_user_id, "expected_version": expected_version}
        )
        transition = await self.repository.begin_terminal(
            operation="CANCEL",
            batch_id=batch_id,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if transition.replay is not None:
            return transition.replay, True
        receipts = [
            await self.cleanup_service.cleanup(asset, terminal_reason="CANCELLED")
            for asset in (transition.assets or [])
        ]
        blocked = any(item.cleanup_status != "DELETED" for item in receipts)
        response_status = 500 if blocked else 200
        response_payload: dict[str, Any] = {
            "cleanup_receipts": [item.model_dump(mode="json") for item in receipts]
        }
        if blocked:
            response_payload = {"detail": {"code": "PRIVACY_BLOCKED", "message": "one or more screenshot originals could not be deleted"}}
        batch = await self.repository.finish_terminal(
            operation="CANCEL",
            batch_id=batch_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status="PRIVACY_BLOCKED" if blocked else "CANCELLED",
            result_import_id=None,
            response_status=response_status,
            response_payload=response_payload,
        )
        if blocked:
            raise PrivacyBlockedError("one or more screenshot originals could not be deleted")
        return ScreenshotUploadBatchCancelResult(batch=batch, cleanup_receipts=receipts), False

    async def recover_expired(self, *, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(timezone.utc)
        recovered: list[str] = []
        for batch in await self.repository.list_expired(now=current):
            assets = await self.asset_repository.list_batch_assets(batch.batch_id)
            receipts = [
                await self.cleanup_service.cleanup(asset, terminal_reason="TIMED_OUT")
                for asset in assets
                if asset.state != "CLEANED"
            ]
            await self.repository.mark_recovered(
                batch_id=batch.batch_id,
                privacy_blocked=any(item.cleanup_status != "DELETED" for item in receipts),
            )
            recovered.append(batch.batch_id)
        return recovered
