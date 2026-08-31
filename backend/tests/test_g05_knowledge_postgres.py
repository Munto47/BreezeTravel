from __future__ import annotations

import asyncio
import copy
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.trip_understanding.knowledge_admin import PostgresKnowledgeAdmin
from app.trip_understanding.models import CreateFullRequest, TextSourceRequest
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.source_crypto import SourceCipher
from app.trip_understanding.worker import TripUnderstandingWorker
from evals.g05_knowledge import load_admission_manifest


pytestmark = pytest.mark.integration
MIGRATIONS = Path("app/db/migrations")
MANIFEST = Path("eval_data/g05_knowledge/admission_v1.json")
NOW = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)


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
            [migration for migration in migrations if migration.name != "032_knowledge_claims.sql"]
            + [MIGRATIONS / "032_knowledge_claims.sql"]
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
async def test_g05_032_migration_works_fresh_and_after_034(upgrade_after_034: bool) -> None:
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name, admin = await _new_database("breezetravel_g05_schema")
    database_dsn = f"{_admin_dsn().rsplit('/', 1)[0]}/{database_name}"
    try:
        await _bootstrap(database_dsn, upgrade_after_034=upgrade_after_034)
        conn = await asyncpg.connect(database_dsn)
        try:
            assert await conn.fetchval(
                "SELECT to_regclass('public.knowledge_claim_versions') IS NOT NULL"
            )
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM applied_migrations WHERE filename = '032_knowledge_claims.sql'"
            ) == 1
            migration_sql = (MIGRATIONS / "032_knowledge_claims.sql").read_text(encoding="utf-8")
            await conn.execute(migration_sql)
        finally:
            await conn.close()
    finally:
        await _drop_database(admin, database_name)


@pytest.mark.asyncio
async def test_g05_postgres_import_version_conflict_withdrawal_and_projection() -> None:
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name, admin_conn = await _new_database("breezetravel_g05_runtime")
    database_dsn = f"{_admin_dsn().rsplit('/', 1)[0]}/{database_name}"
    pool = None
    try:
        await _bootstrap(database_dsn)
        pool = await asyncpg.create_pool(database_dsn, min_size=2, max_size=6)
        await pool.execute(
            "INSERT INTO users(user_id, nickname) VALUES ('g05-owner', 'G05 owner')"
        )
        admin = PostgresKnowledgeAdmin(pool)
        manifest = load_admission_manifest(MANIFEST)

        parallel_a = copy.deepcopy(manifest)
        parallel_a["dataset_id"] = "g05-three-city-parallel-import-a"
        parallel_b = copy.deepcopy(manifest)
        parallel_b["dataset_id"] = "g05-three-city-parallel-import-b"
        distinct_bundle_concurrency = await asyncio.gather(
            admin.import_manifest(parallel_a, imported_at=NOW),
            admin.import_manifest(parallel_b, imported_at=NOW),
        )
        assert [item.replayed for item in distinct_bundle_concurrency] == [False, False]

        concurrent_manifest = copy.deepcopy(manifest)
        concurrent_manifest["dataset_id"] = "g05-three-city-concurrent-import-v1"
        concurrent = await asyncio.gather(
            admin.import_manifest(concurrent_manifest, imported_at=NOW),
            admin.import_manifest(concurrent_manifest, imported_at=NOW),
        )
        assert sorted(item.replayed for item in concurrent) == [False, True]
        first = await admin.import_manifest(manifest, imported_at=NOW)
        replay = await admin.import_manifest(manifest, imported_at=NOW)
        assert first.replayed is False
        assert replay.replayed is True

        repository = PostgresTripUnderstandingRepository(
            pool,
            SourceCipher("g05-postgres-root-secret"),
        )
        service = TripUnderstandingApplicationService(repository)
        created = await service.create_full(
            CreateFullRequest(
                mode="FULL",
                source=TextSourceRequest(
                    type="TEXT",
                    text="北京一日行程。Day 1 上午故宫博物院，下午景山公园。",
                ),
            ),
            owner_user_id="g05-owner",
            idempotency_key="g05-postgres-trip",
            now=NOW,
        )
        assert await TripUnderstandingWorker(repository).run_once(
            "g05-postgres-worker",
            now=NOW,
        )
        resource = await repository.authorize(
            created.accepted.public_resource_id,
            capability_hash=None,
            user_id="g05-owner",
            now=NOW,
        )
        stored = await repository.get_result(resource)
        assert stored is not None
        projected = await repository.project_current_knowledge(
            resource,
            stored.result,
            now=NOW + timedelta(minutes=1),
        )
        palace = next(
            card
            for day in projected.days
            for card in day.activities
            if card.name == "故宫博物院"
        )
        assert [item.type for item in palace.knowledge_suggestions] == [
            "RESERVATION_ADVICE",
            "SEASON",
        ]
        assert await pool.fetchval("SELECT COUNT(*) FROM knowledge_usage_receipts") == 1
        await repository.project_current_knowledge(
            resource,
            stored.result,
            now=NOW + timedelta(minutes=2),
        )
        assert await pool.fetchval("SELECT COUNT(*) FROM knowledge_usage_receipts") == 1

        upgraded = copy.deepcopy(manifest)
        upgraded["dataset_id"] = "g05-three-city-source-upgrade-v2"
        beijing_source = next(
            source
            for source in upgraded["sources"]
            if source["source_key"] == "beijing-gov-palace-visitor-policy"
        )
        beijing_source.update(
            {
                "version": 2,
                "observed_at": "2026-09-01T08:00:00+08:00",
                "reviewed_at": "2026-09-01T08:20:00+08:00",
                "expires_at": "2026-11-30T08:20:00+08:00",
                "version_note": "Second manual review; normalized facts only.",
            }
        )
        upgraded["sources"] = [beijing_source]
        palace_claim = next(
            claim
            for claim in upgraded["claims"]
            if claim["claim_key"] == "beijing-palace-reservation"
        )
        palace_claim.update(
            {
                "version": 2,
                "source_version": 2,
                "suggestion_text": "建议提前实名预约，并在出发当天再次核对官方规则。",
                "effective_at": "2026-09-01T08:20:00+08:00",
                "expires_at": "2026-11-30T08:20:00+08:00",
            }
        )
        upgraded["claims"] = [palace_claim]
        await admin.import_manifest(upgraded, imported_at=NOW + timedelta(days=1))
        projected_v2 = await repository.project_current_knowledge(
            resource,
            stored.result,
            now=NOW + timedelta(days=2),
        )
        palace_v2 = next(
            card
            for day in projected_v2.days
            for card in day.activities
            if card.name == "故宫博物院"
        )
        assert "出发当天" in palace_v2.knowledge_suggestions[0].text

        future = copy.deepcopy(upgraded)
        future["dataset_id"] = "g05-three-city-future-source-upgrade-v3"
        future_source = copy.deepcopy(beijing_source)
        future_source.update(
            {
                "version": 3,
                "observed_at": "2026-09-05T08:00:00+08:00",
                "reviewed_at": "2026-09-05T08:20:00+08:00",
                "expires_at": "2026-12-31T08:20:00+08:00",
                "version_note": "Scheduled future review fixture; normalized facts only.",
            }
        )
        future_claim = copy.deepcopy(palace_claim)
        future_claim.update(
            {
                "version": 3,
                "source_version": 3,
                "suggestion_text": "未来版本：建议在官方规则生效后再按新流程预约。",
                "effective_at": "2026-09-05T08:20:00+08:00",
                "expires_at": "2026-12-31T08:20:00+08:00",
            }
        )
        future["sources"] = [future_source]
        future["claims"] = [future_claim]
        await admin.import_manifest(future, imported_at=NOW + timedelta(days=2))
        before_future_effective = await repository.project_current_knowledge(
            resource,
            stored.result,
            now=NOW + timedelta(days=2, minutes=1),
        )
        before_future_palace = next(
            card
            for day in before_future_effective.days
            for card in day.activities
            if card.name == "故宫博物院"
        )
        assert "出发当天" in before_future_palace.knowledge_suggestions[0].text
        assert all(
            "未来版本" not in suggestion.text
            for suggestion in before_future_palace.knowledge_suggestions
        )

        conflict = copy.deepcopy(upgraded)
        conflict["dataset_id"] = "g05-three-city-conflict-v1"
        conflicting_claim = copy.deepcopy(palace_claim)
        conflicting_claim.update(
            {
                "claim_key": "beijing-palace-reservation-conflict",
                "version": 1,
                "suggestion_text": "无需提前预约，可在现场直接购票。",
            }
        )
        conflict["claims"] = [conflicting_claim]
        await admin.import_manifest(conflict, imported_at=NOW + timedelta(days=2))
        conflicted = await repository.project_current_knowledge(
            resource,
            stored.result,
            now=NOW + timedelta(days=2, minutes=1),
        )
        conflicted_palace = next(
            card
            for day in conflicted.days
            for card in day.activities
            if card.name == "故宫博物院"
        )
        assert [item.type for item in conflicted_palace.knowledge_suggestions] == ["SEASON"]

        concurrent_withdrawals = await asyncio.gather(
            *(
                admin.withdraw_claim(
                    claim_key="beijing-palace-reservation-conflict",
                    version=1,
                    reason="conflict fixture removed",
                    reviewer="WP-G05-INTEGRATOR",
                    withdrawn_at=NOW + timedelta(days=2, minutes=2),
                )
                for _ in range(2)
            )
        )
        assert sorted(concurrent_withdrawals) == [False, True]
        restored = await repository.project_current_knowledge(
            resource,
            stored.result,
            now=NOW + timedelta(days=2, minutes=3),
        )
        restored_palace = next(
            card
            for day in restored.days
            for card in day.activities
            if card.name == "故宫博物院"
        )
        assert [item.type for item in restored_palace.knowledge_suggestions] == [
            "RESERVATION_ADVICE",
            "SEASON",
        ]

        replayed_withdrawal = await admin.withdraw_claim(
            claim_key="beijing-palace-reservation",
            version=2,
            reason="operator withdrawal test",
            reviewer="WP-G05-INTEGRATOR",
            withdrawn_at=NOW + timedelta(days=2, minutes=4),
        )
        assert replayed_withdrawal is False
        after_withdrawal = await repository.project_current_knowledge(
            resource,
            stored.result,
            now=NOW + timedelta(days=2, minutes=5),
        )
        withdrawn_palace = next(
            card
            for day in after_withdrawal.days
            for card in day.activities
            if card.name == "故宫博物院"
        )
        assert [item.type for item in withdrawn_palace.knowledge_suggestions] == ["SEASON"]

        source_withdrawal_replayed = await admin.withdraw_source(
            source_key="beijing-gov-palace-profile",
            version=1,
            reason="government profile withdrawal test",
            reviewer="WP-G05-INTEGRATOR",
            withdrawn_at=NOW + timedelta(days=2, minutes=6),
        )
        assert source_withdrawal_replayed is False
        assert await admin.withdraw_source(
            source_key="beijing-gov-palace-profile",
            version=1,
            reason="government profile withdrawal test",
            reviewer="WP-G05-INTEGRATOR",
            withdrawn_at=NOW + timedelta(days=2, minutes=7),
        )
        after_source_withdrawal = await repository.project_current_knowledge(
            resource,
            stored.result,
            now=NOW + timedelta(days=2, minutes=8),
        )
        fully_withdrawn_palace = next(
            card
            for day in after_source_withdrawal.days
            for card in day.activities
            if card.name == "故宫博物院"
        )
        assert fully_withdrawn_palace.knowledge_suggestions == []
        readback = await admin.readback()
        assert readback["claim_withdrawals"] == 2
        assert readback["source_withdrawals"] == 1

        immutable_tables = (
            "knowledge_source_versions",
            "knowledge_source_withdrawals",
            "knowledge_claim_versions",
            "knowledge_claim_withdrawals",
            "knowledge_import_receipts",
            "knowledge_usage_receipts",
        )
        for table_name in immutable_tables:
            with pytest.raises(asyncpg.RaiseError, match="immutable"):
                await pool.execute(
                    f"UPDATE {table_name} SET created_at = created_at"
                )

        await repository.delete_trip(
            resource,
            capability_hash=None,
            user_id="g05-owner",
            idempotency_key="g05-postgres-delete-trip",
            request_hash="f" * 64,
            now=NOW + timedelta(days=2, minutes=9),
        )
        assert await pool.fetchval("SELECT COUNT(*) FROM knowledge_usage_receipts") == 0
    finally:
        if pool is not None:
            await pool.close()
        await _drop_database(admin_conn, database_name)
