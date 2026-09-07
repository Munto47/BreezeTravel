from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.trip_understanding.errors import (
    IdempotencyInProgressError,
    ScreenshotBatchAlreadyUsedError,
)
from app.trip_understanding.models import (
    ScreenshotBatchAssetInput,
    ScreenshotBatchClaimInput,
    ScreenshotBatchPersistenceInput,
    ScreenshotCleanupPersistenceInput,
    ScreenshotCleanupReceiptInput,
)
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.screenshot_ocr import (
    ScreenshotImageResultV1,
    ScreenshotOcrEngineBindingV1,
    ScreenshotSourceDocumentV1,
    ScreenshotSourceLineV1,
    SemanticSpanV1,
)
from app.trip_understanding.source_crypto import SourceCipher


pytestmark = pytest.mark.integration


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


def _document() -> ScreenshotSourceDocumentV1:
    first = "北京 Day 1"
    second = "故宫博物院"
    semantic_text = f"{first}\n{second}"
    return ScreenshotSourceDocumentV1.create(
        semantic_text=semantic_text,
        partial=False,
        images=(
            ScreenshotImageResultV1(
                image_index=0,
                content_hash="a" * 64,
                status="SUCCEEDED",
                line_count=2,
            ),
        ),
        lines=(
            ScreenshotSourceLineV1(
                image_index=0,
                reading_index=0,
                text=first,
                confidence=0.99,
                bbox=((0, 0), (100, 0), (100, 20), (0, 20)),
                semantic_span=SemanticSpanV1(start=0, end=len(first)),
                requires_confirmation=False,
            ),
            ScreenshotSourceLineV1(
                image_index=0,
                reading_index=1,
                text=second,
                confidence=0.7,
                bbox=((0, 30), (100, 30), (100, 50), (0, 50)),
                semantic_span=SemanticSpanV1(
                    start=len(first) + 1,
                    end=len(semantic_text),
                ),
                requires_confirmation=True,
            ),
        ),
        engine_binding=ScreenshotOcrEngineBindingV1.create(
            engine="g04-postgres-fixture",
            engine_version="1",
            configuration={"evidence_tier": "AUTOMATED_FIXTURE"},
        ),
    )


def _payload(
    *,
    batch_ref: str,
    idempotency_key: str,
    now: datetime,
) -> ScreenshotBatchPersistenceInput:
    document = _document()
    return ScreenshotBatchPersistenceInput(
        batch_ref=batch_ref,
        owner_user_id="g04-owner",
        idempotency_key=idempotency_key,
        request_hash=canonical_sha256(
            {"asset": "a" * 64, "idempotency_key": idempotency_key}
        ),
        source_document_json=document.model_dump_json(),
        source_document_hash=document.document_hash,
        semantic_text_hash=hashlib.sha256(
            document.semantic_text.encode("utf-8")
        ).hexdigest(),
        outcome="COMPLETE",
        expires_at=now + timedelta(minutes=15),
        assets=(
            ScreenshotBatchAssetInput(
                upload_position=0,
                content_hash="a" * 64,
                media_type="image/png",
                byte_size=1024,
                storage_locator=hashlib.sha256(
                    f"locator:{batch_ref}".encode("utf-8")
                ).hexdigest(),
                ocr_status="SUCCEEDED",
            ),
        ),
        cleanup_receipts=(
            ScreenshotCleanupReceiptInput(
                upload_position=0,
                attempt_number=1,
                terminal_reason="SUCCEEDED",
                cleanup_status="DELETED",
                attempted_at=now,
            ),
        ),
    )


def _claim_payload(payload: ScreenshotBatchPersistenceInput) -> ScreenshotBatchClaimInput:
    return ScreenshotBatchClaimInput(
        batch_ref=payload.batch_ref,
        owner_user_id=payload.owner_user_id,
        idempotency_key=payload.idempotency_key,
        request_hash=payload.request_hash,
        expires_at=payload.expires_at,
        assets=tuple(
            asset.model_copy(update={"ocr_status": "PENDING"})
            for asset in payload.assets
        ),
    )


@pytest.mark.asyncio
async def test_g04_postgres_upgrade_encryption_atomic_consume_and_ttl_purge() -> None:
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name = f"breezetravel_g04_{uuid4().hex[:10]}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    admin = await asyncpg.connect(_admin_dsn())
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        database_dsn = f"{_admin_dsn().rsplit('/', 1)[0]}/{database_name}"
        bootstrap = await asyncpg.connect(database_dsn)
        try:
            await bootstrap.execute(Path("app/db/init.sql").read_text(encoding="utf-8"))
            await bootstrap.execute(
                """
                CREATE TABLE applied_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            migrations = sorted(Path("app/db/migrations").glob("*.sql"))
            for migration in migrations:
                if migration.name == "034_trip_understanding_screenshot_batches.sql":
                    continue
                await bootstrap.execute(migration.read_text(encoding="utf-8"))
                await bootstrap.execute(
                    "INSERT INTO applied_migrations(filename) VALUES ($1)",
                    migration.name,
                )
            migration_034 = next(
                migration
                for migration in migrations
                if migration.name == "034_trip_understanding_screenshot_batches.sql"
            )
            migration_sql = migration_034.read_text(encoding="utf-8")
            await bootstrap.execute(migration_sql)
            await bootstrap.execute(migration_sql)
            await bootstrap.execute(
                "INSERT INTO applied_migrations(filename) VALUES ($1)",
                migration_034.name,
            )
            await bootstrap.execute(
                "INSERT INTO users(user_id, nickname) VALUES ('g04-owner', 'G04 owner')"
            )
        finally:
            await bootstrap.close()

        pool = await asyncpg.create_pool(database_dsn, min_size=2, max_size=5)
        repository = PostgresTripUnderstandingRepository(
            pool,
            SourceCipher("g04-postgres-root-secret"),
        )
        now = datetime.now(timezone.utc)
        batch_ref = "A" * 43
        upload_payload = _payload(
            batch_ref=batch_ref,
            idempotency_key="g04-upload",
            now=now,
        )
        concurrent_claims = await asyncio.gather(
            repository.claim_screenshot_batch(_claim_payload(upload_payload), now=now),
            repository.claim_screenshot_batch(_claim_payload(upload_payload), now=now),
            return_exceptions=True,
        )
        assert sum(item is None for item in concurrent_claims) == 1
        assert sum(
            isinstance(item, IdempotencyInProgressError)
            for item in concurrent_claims
        ) == 1
        created = await repository.store_screenshot_batch(
            upload_payload,
            now=now,
        )
        assert created.accepted.batch_ref == batch_ref
        batch = await pool.fetchrow(
            "SELECT * FROM trip_understanding_screenshot_batches"
        )
        assert batch["batch_ref_hash"].strip() == hashlib.sha256(
            batch_ref.encode("utf-8")
        ).hexdigest()
        assert batch_ref.encode("utf-8") not in bytes(batch["encrypted_source_document"])
        assert "故宫博物院".encode("utf-8") not in bytes(
            batch["encrypted_source_document"]
        )
        assert await pool.fetchval(
            """
            SELECT COUNT(*) FROM trip_understanding_screenshot_cleanup_receipts
            WHERE cleanup_status = 'DELETED'
            """
        ) == 1

        request_hash = canonical_sha256(
            {
                "mode": "FULL",
                "source": {"type": "SCREENSHOT_BATCH", "batch_ref": batch_ref},
            }
        )

        async def consume(idempotency_key: str):
            return await repository.create_full_from_screenshot(
                owner_user_id="g04-owner",
                batch_ref=batch_ref,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now,
                retention_days=30,
            )

        concurrent = await asyncio.gather(
            consume("g04-consume-a"),
            consume("g04-consume-b"),
            return_exceptions=True,
        )
        winners = [item for item in concurrent if not isinstance(item, Exception)]
        losers = [item for item in concurrent if isinstance(item, Exception)]
        assert len(winners) == 1, concurrent
        assert len(losers) == 1
        assert isinstance(losers[0], ScreenshotBatchAlreadyUsedError)
        assert await pool.fetchval(
            """
            SELECT encrypted_source_document IS NULL
              AND encryption_key_ref IS NULL
              AND status = 'CONSUMED'
            FROM trip_understanding_screenshot_batches
            """
        )
        source_row = await pool.fetchrow(
            """
            SELECT source_type, encrypted_content, encryption_key_ref
            FROM trip_understanding_sources
            WHERE source_type = 'SCREENSHOT_OCR'
            """
        )
        assert source_row["source_type"] == "SCREENSHOT_OCR"
        assert "故宫博物院".encode("utf-8") not in bytes(source_row["encrypted_content"])

        job = await repository.claim_next(
            worker_id="g04-postgres-worker",
            now=now,
            lease_seconds=30,
        )
        assert job is not None
        source = await repository.load_source(job, now=now)
        assert source.source_type == "SCREENSHOT_OCR"
        assert source.text == _document().semantic_text
        assert [(span.start, span.end) for span in source.requires_confirmation_spans] == [
            (len("北京 Day 1") + 1, len(_document().semantic_text))
        ]
        source_binding = await pool.fetchrow(
            """
            SELECT r.understanding_id, r.revision, r.source_id
            FROM trip_understanding_revisions r
            WHERE r.understanding_id = $1 AND r.revision = $2
            """,
            job.understanding_id,
            job.revision,
        )
        await pool.execute(
            """
            INSERT INTO trip_understanding_source_claims (
                claim_id, understanding_id, revision, source_id, claim_type,
                span_start, span_end, quote, created_at
            ) VALUES ($1, $2, $3, $4, 'DAY', 0, 2, $5, $6)
            """,
            str(uuid4()),
            source_binding["understanding_id"],
            source_binding["revision"],
            source_binding["source_id"],
            "受控密文片段",
            now,
        )
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM trip_understanding_source_claims WHERE source_id = $1",
            source_binding["source_id"],
        ) == 1
        private_planned_text = "截图中的私人集合说明 010-00000000"
        await pool.execute(
            """
            INSERT INTO trip_understanding_activities (
                activity_id, understanding_id, revision, public_activity_token,
                day_index, sequence_index, role, mention_text, atomic_place_name,
                category_hint, time_hint, eligible_for_place_search,
                resolution_status, canonical_place_id, resolver_receipt_json, created_at
            ) VALUES (
                $1, $2, $3, $4, 1, 999, 'PLANNED', $5, NULL,
                NULL, NULL, FALSE, 'NOT_ELIGIBLE', NULL, '{}'::jsonb, $6
            )
            """,
            str(uuid4()),
            source_binding["understanding_id"],
            source_binding["revision"],
            "ttl-private-token-123456789012",
            private_planned_text,
            now,
        )
        assert await pool.fetchval(
            """
            SELECT COUNT(*) FROM trip_understanding_activities
            WHERE understanding_id = $1 AND revision = $2 AND mention_text = $3
            """,
            source_binding["understanding_id"],
            source_binding["revision"],
            private_planned_text,
        ) == 1

        second_ref = "B" * 43
        expiry_payload = _payload(
            batch_ref=second_ref,
            idempotency_key="g04-expiry",
            now=now,
        )
        await repository.claim_screenshot_batch(_claim_payload(expiry_payload), now=now)
        await repository.store_screenshot_batch(
            expiry_payload,
            now=now,
        )
        purged = await repository.purge_expired_private_data(
            now=now + timedelta(days=31),
            limit=100,
        )
        assert purged["sources_purged"] >= 1
        assert purged["batches_purged"] == 1
        assert await pool.fetchval(
            """
            SELECT COUNT(*) FROM trip_understanding_sources
            WHERE source_type = 'SCREENSHOT_OCR'
              AND encrypted_content IS NULL
              AND deleted_at IS NOT NULL
            """
        ) == 1
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM trip_understanding_source_claims WHERE source_id = $1",
            source_binding["source_id"],
        ) == 0
        assert await pool.fetchval(
            """
            SELECT COUNT(*) FROM trip_understanding_activities AS activity
            JOIN trip_understanding_revisions AS revision
              ON revision.understanding_id = activity.understanding_id
             AND revision.revision = activity.revision
            WHERE revision.source_id = $1
            """,
            source_binding["source_id"],
        ) == 0
        assert await pool.fetchval(
            """
            SELECT COUNT(*) FROM trip_understanding_screenshot_batches
            WHERE status = 'EXPIRED'
              AND encrypted_source_document IS NULL
              AND document_purged_at IS NOT NULL
            """
        ) == 1
        reasons = {
            row["terminal_reason"]
            for row in await pool.fetch(
                "SELECT terminal_reason FROM trip_understanding_screenshot_cleanup_receipts"
            )
        }
        assert {
            "SUCCEEDED",
            "CONSUMED_SOURCE_MOVED",
            "SOURCE_TTL_EXPIRED",
            "BATCH_TTL_EXPIRED",
        } <= reasons
        source_ttl_receipts = await pool.fetchval(
            """
            SELECT COUNT(*) FROM trip_understanding_screenshot_cleanup_receipts
            WHERE source_id = $1 AND terminal_reason = 'SOURCE_TTL_EXPIRED'
            """,
            source_binding["source_id"],
        )
        assert source_ttl_receipts == 1
        assert await repository.purge_expired_private_data(
            now=now + timedelta(days=31),
            limit=100,
        ) == {"sources_purged": 0, "batches_purged": 0}
        assert await pool.fetchval(
            """
            SELECT COUNT(*) FROM trip_understanding_screenshot_cleanup_receipts
            WHERE source_id = $1 AND terminal_reason = 'SOURCE_TTL_EXPIRED'
            """,
            source_binding["source_id"],
        ) == 1
        receipt_id = await pool.fetchval(
            "SELECT receipt_id FROM trip_understanding_screenshot_cleanup_receipts LIMIT 1"
        )
        with pytest.raises(asyncpg.RaiseError, match="immutable"):
            await pool.execute(
                """
                UPDATE trip_understanding_screenshot_cleanup_receipts
                SET terminal_reason = terminal_reason
                WHERE receipt_id = $1
                """,
                receipt_id,
            )

        pending_payload = _payload(
            batch_ref="C" * 43,
            idempotency_key="g04-pending-delete",
            now=now,
        )
        pending_claim = _claim_payload(pending_payload)
        await repository.claim_screenshot_batch(pending_claim, now=now)
        account_request_hash = canonical_sha256(
            {"action": "DELETE_ALL_TRAVEL_DATA"}
        )

        retry = await repository.delete_account_travel_data(
            user_id="g04-owner",
            idempotency_key="g04-account-delete-pending",
            request_hash=account_request_hash,
            now=now,
        )
        assert retry.view.status == "RETRY_REQUIRED"
        assert retry.view.next_action == "RETRY"
        assert await pool.fetchval(
            """
            SELECT status = 'PROCESSING'
              AND encrypted_source_document IS NULL
              AND document_purged_at IS NULL
            FROM trip_understanding_screenshot_batches
            WHERE batch_ref_hash = $1
            """,
            hashlib.sha256(("C" * 43).encode("utf-8")).hexdigest(),
        )

        await repository.record_screenshot_cleanup(
            ScreenshotCleanupPersistenceInput(
                owner_user_id="g04-owner",
                idempotency_key=pending_payload.idempotency_key,
                assets=pending_claim.assets,
                cleanup_receipts=(
                    ScreenshotCleanupReceiptInput(
                        upload_position=0,
                        attempt_number=1,
                        terminal_reason="CANCELLED",
                        cleanup_status="DELETED",
                        attempted_at=now,
                    ),
                ),
            ),
            now=now,
        )
        completed = await repository.delete_account_travel_data(
            user_id="g04-owner",
            idempotency_key="g04-account-delete-clean",
            request_hash=account_request_hash,
            now=now,
        )
        assert completed.view.status == "COMPLETED"
        assert completed.view.next_action == "NONE"
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
