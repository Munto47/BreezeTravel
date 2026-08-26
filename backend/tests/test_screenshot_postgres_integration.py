from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.importing.models import ImportSourceType, ImportStatus, ItineraryImport
from app.importing.repositories import PostgresImportRepository
from app.importing.screenshots import (
    OcrBoundingBox,
    OcrTextLine,
    PostgresScreenshotAssetRepository,
    ScreenshotAssetCleanupService,
    ScreenshotOcrReceipt,
    TemporaryAssetRecord,
)
from app.importing.upload_batches import PostgresScreenshotUploadBatchRepository
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import TripDateRange, TripWorkspace
from app.itineraries.repositories import PostgresItineraryRepository


pytestmark = pytest.mark.integration
PNG = b"\x89PNG\r\n\x1a\npostgres-controlled-fixture"


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


@pytest.mark.asyncio
async def test_postgres_screenshot_cleanup_and_ocr_artifact_readback(tmp_path):
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")

    database_name = f"breezetravel_screenshot_{uuid4().hex[:10]}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    admin = await asyncpg.connect(_admin_dsn())
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        database_dsn = f"{_admin_dsn().rsplit('/', 1)[0]}/{database_name}"
        bootstrap = await asyncpg.connect(database_dsn)
        try:
            await bootstrap.execute(Path("app/db/init.sql").read_text(encoding="utf-8"))
        finally:
            await bootstrap.close()
        migration_env = os.environ.copy()
        migration_env["DATABASE_URL"] = database_dsn.replace("postgresql://", "postgresql+asyncpg://")
        migrated = subprocess.run(
            [sys.executable, "-m", "scripts.migrate"],
            cwd=Path.cwd(),
            env=migration_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert migrated.returncode == 0, migrated.stderr

        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=4)
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO users(user_id, nickname) VALUES ('screenshot-pg-user', 'Screenshot PG')")
            await conn.execute(
                """
                INSERT INTO rooms(room_id, thread_id, trip_city, trip_days)
                VALUES ('screenshot-pg-room', 'screenshot-pg-thread', '北京', 2)
                """
            )
        workspace = TripWorkspace(
            workspace_id="screenshot-pg-workspace",
            room_id="screenshot-pg-room",
            city="北京",
            trip_date_range=TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
            created_by="screenshot-pg-user",
        )
        await PostgresItineraryRepository(pool).create_workspace(workspace)
        batch_repository = PostgresScreenshotUploadBatchRepository(pool)
        batch_request_hash = sha256_canonical({"operation": "controlled-create"})
        batch, created_replayed = await batch_repository.create(
            workspace_id=workspace.workspace_id,
            actor_user_id="screenshot-pg-user",
            expected_count=1,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
            idempotency_key="postgres-create-batch",
            request_hash=batch_request_hash,
        )
        assert created_replayed is False
        replayed_batch, batch_replayed = await batch_repository.create(
            workspace_id=workspace.workspace_id,
            actor_user_id="screenshot-pg-user",
            expected_count=1,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
            idempotency_key="postgres-create-batch",
            request_hash=batch_request_hash,
        )
        assert batch_replayed is True
        assert replayed_batch.batch_id == batch.batch_id

        staged_path = tmp_path / "batch-asset.png"
        staged_path.write_bytes(PNG)
        staged_asset = TemporaryAssetRecord(
            asset_id="screenshot-pg-batch-asset",
            workspace_id=workspace.workspace_id,
            content_hash="b" * 64,
            media_type="image/png",
            byte_size=len(PNG),
            storage_locator=str(staged_path),
            upload_batch_id=batch.batch_id,
            upload_position=0,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
            created_at=datetime.now(timezone.utc),
        )
        stored, stored_replayed = await batch_repository.store_file(
            batch_id=batch.batch_id,
            workspace_id=workspace.workspace_id,
            actor_user_id="screenshot-pg-user",
            position=0,
            expected_version=1,
            asset=staged_asset,
            idempotency_key="postgres-upload-file",
            request_hash=sha256_canonical({"operation": "controlled-upload"}),
        )
        assert stored_replayed is False
        assert stored.uploaded_positions == [0]
        assert stored.version == 2
        await ScreenshotAssetCleanupService(
            PostgresScreenshotAssetRepository(pool),
            temp_root=tmp_path,
        ).cleanup(staged_asset, terminal_reason="CANCELLED")
        assert not staged_path.exists()

        itinerary_import = ItineraryImport(
            import_id="screenshot-pg-import",
            workspace_id=workspace.workspace_id,
            source_type=ImportSourceType.AI_TEXT,
            raw_text="第1天：09:00-11:00 颐和园",
            parse_version="controlled-ocr-v1",
            status=ImportStatus.FAILED,
            created_by="screenshot-pg-user",
        )
        await PostgresImportRepository(pool).create_import(itinerary_import)

        image_path = tmp_path / "asset.png"
        image_path.write_bytes(PNG)
        now = datetime.now(timezone.utc)
        asset = TemporaryAssetRecord(
            asset_id="screenshot-pg-asset",
            workspace_id=workspace.workspace_id,
            content_hash="a" * 64,
            media_type="image/png",
            byte_size=len(PNG),
            storage_locator=str(image_path),
            expires_at=now - timedelta(seconds=1),
            created_at=now - timedelta(minutes=20),
        )
        repository = PostgresScreenshotAssetRepository(pool)
        await repository.create_assets([asset])
        recovered = await ScreenshotAssetCleanupService(repository, temp_root=tmp_path).recover_expired(now=now)
        assert recovered[0].cleanup_status == "DELETED"
        assert not image_path.exists()

        ocr = ScreenshotOcrReceipt(
            asset_id=asset.asset_id,
            asset_hash=asset.content_hash,
            media_type=asset.media_type,
            byte_size=asset.byte_size,
            engine="controlled_ocr_fixture",
            engine_version="fixture-v1",
            observed_at=now,
            lines=[
                OcrTextLine(
                    text="颐和园",
                    confidence=0.98,
                    box=OcrBoundingBox(x_min=1, y_min=2, x_max=80, y_max=30),
                )
            ],
        )
        await repository.attach_ocr_artifacts(itinerary_import.import_id, [ocr])
        assert await repository.get_ocr_artifacts(itinerary_import.import_id) == [ocr]
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, storage_locator FROM trip_temporary_assets WHERE asset_id = $1",
                asset.asset_id,
            )
            assert row["state"] == "CLEANED"
            assert row["storage_locator"] == f"deleted://{asset.asset_id}"
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM temporary_asset_cleanup_receipts WHERE asset_id = $1",
                asset.asset_id,
            ) == 1
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
