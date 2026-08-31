from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.trip_understanding.memory_share import FeedbackRequest, PreferenceMemoryView
from app.trip_understanding.models import CreateFullRequest, TextSourceRequest
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.source_crypto import SourceCipher
from app.trip_understanding.worker import TripUnderstandingWorker


pytestmark = pytest.mark.integration
MIGRATIONS = Path("app/db/migrations")
NOW = datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


async def _new_database(prefix: str) -> tuple[str, asyncpg.Connection]:
    database_name = f"{prefix}_{uuid4().hex[:10]}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    admin = await asyncpg.connect(_admin_dsn())
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    return database_name, admin


async def _drop_database(admin: asyncpg.Connection, database_name: str) -> None:
    await admin.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
        database_name,
    )
    await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
    await admin.close()


async def _bootstrap(database_dsn: str, *, upgrade_after_034: bool = False) -> None:
    conn = await asyncpg.connect(database_dsn)
    try:
        await conn.execute(Path("app/db/init.sql").read_text(encoding="utf-8"))
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applied_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        migrations = sorted(MIGRATIONS.glob("*.sql"))
        ordered = (
            [migration for migration in migrations if migration.name != "033_user_memory_and_feedback.sql"]
            + [MIGRATIONS / "033_user_memory_and_feedback.sql"]
            if upgrade_after_034
            else migrations
        )
        for migration in ordered:
            await conn.execute(migration.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO applied_migrations(filename) VALUES ($1) ON CONFLICT DO NOTHING",
                migration.name,
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("upgrade_after_034", [False, True])
async def test_g06_033_migration_works_fresh_and_after_034(upgrade_after_034: bool) -> None:
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name, admin = await _new_database("breezetravel_g06_schema")
    dsn = f"{_admin_dsn().rsplit('/', 1)[0]}/{database_name}"
    try:
        await _bootstrap(dsn, upgrade_after_034=upgrade_after_034)
        conn = await asyncpg.connect(dsn)
        try:
            for table in (
                "g06_data_consents",
                "g06_preference_profiles",
                "g06_feedback_events",
                "g06_share_links",
                "g06_share_sessions",
            ):
                assert await conn.fetchval(
                    "SELECT to_regclass($1) IS NOT NULL", f"public.{table}"
                )
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM applied_migrations WHERE filename = '033_user_memory_and_feedback.sql'"
            ) == 1
            await conn.execute(
                (MIGRATIONS / "033_user_memory_and_feedback.sql").read_text(encoding="utf-8")
            )
        finally:
            await conn.close()
    finally:
        await _drop_database(admin, database_name)


@pytest.mark.asyncio
async def test_g06_postgres_consent_feedback_share_revoke_and_account_clear() -> None:
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name, admin = await _new_database("breezetravel_g06_runtime")
    dsn = f"{_admin_dsn().rsplit('/', 1)[0]}/{database_name}"
    pool = None
    try:
        await _bootstrap(dsn)
        pool = await asyncpg.create_pool(dsn, min_size=2, max_size=8)
        await pool.executemany(
            "INSERT INTO users(user_id, nickname) VALUES ($1, $2)",
            [("g06-owner", "G06 owner"), ("g06-other", "G06 other")],
        )
        repository = PostgresTripUnderstandingRepository(
            pool,
            SourceCipher("g06-postgres-root-secret"),
        )
        assert (await repository.get_data_consents("g06-owner")).model_dump() == {
            "memory_enabled": False,
            "feedback_enabled": False,
            "training_eval_enabled": False,
        }
        await repository.set_data_consent("g06-owner", "memory", True, now=NOW)
        preference = PreferenceMemoryView(
            walking_tolerance_minutes=30,
            preferred_start_time="08:15",
            dining_preferences=["LOCAL", "NO_SPICY"],
            hotel_preferences=["CHAIN", "NEAR_TRANSIT"],
            intensity="BALANCED",
        )
        assert await repository.save_preference_memory(
            "g06-owner", preference, now=NOW
        ) == preference
        await repository.set_data_consent("g06-owner", "feedback", True, now=NOW)
        consents = await repository.get_data_consents("g06-owner")
        assert consents.memory_enabled and consents.feedback_enabled
        assert consents.training_eval_enabled is False

        created = await TripUnderstandingApplicationService(repository).create_full(
            CreateFullRequest(
                mode="FULL",
                source=TextSourceRequest(
                    type="TEXT",
                    text="北京一日行程。Day 1 故宫博物院、景山公园。",
                ),
            ),
            owner_user_id="g06-owner",
            idempotency_key="g06-postgres-trip",
            now=NOW,
        )
        assert await TripUnderstandingWorker(repository).run_once(
            "g06-postgres-worker", now=NOW
        )
        resource = await repository.authorize(
            created.accepted.public_resource_id,
            capability_hash=None,
            user_id="g06-owner",
            now=NOW,
        )
        stored = await repository.get_result(resource)
        assert stored is not None

        feedback = FeedbackRequest(event_type="VOLUNTARY", subject_type="TRIP")
        assert await repository.record_feedback(
            resource,
            "g06-owner",
            feedback,
            idempotency_key="feedback-1",
            now=NOW,
        ) is False
        assert await repository.record_feedback(
            resource,
            "g06-owner",
            feedback,
            idempotency_key="feedback-1",
            now=NOW,
        ) is True
        assert await pool.fetchval("SELECT COUNT(*) FROM g06_feedback_events") == 1

        concurrent = await asyncio.gather(
            repository.create_share(
                resource,
                "g06-owner",
                stored.result,
                idempotency_key="share-1",
                expires_in_days=7,
                signing_key="g06-share-signing-key-with-adequate-length",
                now=NOW,
            ),
            repository.create_share(
                resource,
                "g06-owner",
                stored.result,
                idempotency_key="share-1",
                expires_in_days=7,
                signing_key="g06-share-signing-key-with-adequate-length",
                now=NOW,
            ),
        )
        assert sorted(replayed for _, replayed in concurrent) == [False, True]
        assert concurrent[0][0] == concurrent[1][0]
        path, secret = concurrent[0][0].share_url.split("#s=", 1)
        share_ref = path.rsplit("/", 1)[1]
        assert secret not in str(await pool.fetchrow("SELECT * FROM g06_share_links"))
        session = await repository.exchange_share_secret(share_ref, secret, now=NOW)
        projection = await repository.read_share(
            share_ref, session.capability, now=NOW + timedelta(minutes=1)
        )
        serialized = projection.model_dump_json()
        assert "故宫博物院" in serialized
        for forbidden in ("activity_token", "revision", "hash", "receipt", "source"):
            assert forbidden not in serialized.lower()

        assert await repository.revoke_share(
            share_ref, "g06-other", now=NOW
        ) is False
        assert await repository.revoke_share(
            share_ref, "g06-owner", now=NOW
        ) is True
        with pytest.raises(Exception, match="share is unavailable"):
            await repository.read_share(
                share_ref, session.capability, now=NOW + timedelta(minutes=2)
            )

        await repository.create_share(
            resource,
            "g06-owner",
            stored.result,
            idempotency_key="share-account-clear",
            expires_in_days=7,
            signing_key="g06-share-signing-key-with-adequate-length",
            now=NOW,
        )
        outcome = await repository.delete_account_travel_data(
            user_id="g06-owner",
            idempotency_key="g06-account-clear",
            request_hash="d" * 64,
            now=NOW + timedelta(hours=1),
        )
        assert outcome.view.status == "COMPLETED"
        assert await repository.get_preference_memory("g06-owner") is None
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM g06_feedback_events WHERE owner_user_id = 'g06-owner'"
        ) == 0
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM g06_share_links WHERE owner_user_id = 'g06-owner'"
        ) == 0
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM trip_understandings WHERE owner_user_id = 'g06-owner'"
        ) == 0
    finally:
        if pool is not None:
            await pool.close()
        await _drop_database(admin, database_name)
