import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest


pytestmark = pytest.mark.integration


def _admin_dsn() -> str:
    return os.getenv("TEST_DATABASE_ADMIN_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")


def test_migration_020_accepts_fresh_019_unique_constraint_backing_index():
    migration = Path("app/db/migrations/020_recommendation_event_commands.sql").read_text(
        encoding="utf-8"
    )
    assert "suggestion_candidates_workspace_set_candidate_key" in migration
    assert "duplicate_object OR duplicate_table" in migration


@pytest.mark.asyncio
async def test_fresh_and_existing_database_migrations():
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    name = f"breezetravel_migration_{uuid4().hex[:12]}"
    assert re.fullmatch(r"[a-z0-9_]+", name)
    admin = await asyncpg.connect(_admin_dsn())
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        base = _admin_dsn().rsplit("/", 1)[0]
        database_url = f"{base}/{name}"
        conn = await asyncpg.connect(database_url)
        try:
            await conn.execute((Path("app/db/init.sql")).read_text(encoding="utf-8"))
            await conn.execute(
                """
                CREATE TABLE applied_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            for path in sorted(Path("app/db/migrations").glob("*.sql")):
                if path.name == "013_idempotent_creation_commands.sql":
                    break
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute(
                    "INSERT INTO applied_migrations(filename) VALUES ($1)",
                    path.name,
                )
            await conn.execute("INSERT INTO users(user_id, nickname) VALUES ('migration-user', 'migration')")
            await conn.execute(
                "INSERT INTO rooms(room_id, thread_id, trip_city, trip_days) "
                "VALUES ('migration-room', 'migration-thread', '北京', 2)"
            )
            await conn.execute(
                """
                INSERT INTO trip_workspaces (
                    workspace_id, room_id, city, trip_start_date, trip_end_date, created_by
                ) VALUES (
                    'migration-workspace', 'migration-room', '北京', '2026-10-01', '2026-10-02', 'migration-user'
                )
                """
            )
            for import_id in ("legacy-import-a", "legacy-import-z"):
                await conn.execute(
                    """
                    INSERT INTO itinerary_imports (
                        import_id, workspace_id, source_type, raw_text, parse_version,
                        status, parsed_json, created_by, updated_at
                    ) VALUES (
                        $1, 'migration-workspace', 'AI_TEXT', 'legacy', 'test',
                        'PARSED', '{}'::jsonb, 'migration-user', '2026-08-01T00:00:00Z'
                    )
                    """,
                    import_id,
                )
        finally:
            await conn.close()

        migration_env = os.environ.copy()
        migration_env["DATABASE_URL"] = database_url.replace("postgresql://", "postgresql+asyncpg://")
        first = subprocess.run(
            [sys.executable, "-m", "scripts.migrate"],
            cwd=Path.cwd(),
            env=migration_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert first.returncode == 0, first.stderr
        check = await asyncpg.connect(database_url)
        try:
            assert await check.fetchval("SELECT COUNT(*) FROM applied_migrations") == 21
            assert await check.fetchval("SELECT to_regclass('public.verification_reports') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.trip_workspaces') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.itinerary_revisions') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.itinerary_edit_commands') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.itinerary_place_receipts') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.suggestion_sets') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.suggestion_candidates') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.recommendation_events') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.suggestion_accept_commands') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.suggestion_undo_links') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.evidence_snapshots') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.audit_reports') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.repair_options') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.final_tips_artifacts') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.idempotent_creation_commands') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.city_route_templates') IS NOT NULL")
            assert await check.fetchval("SELECT to_regclass('public.trip_share_links') IS NOT NULL")
            assert await check.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'idempotent_creation_commands_operation_check')"
            )
            assert (
                await check.fetchval(
                    "SELECT current_import_id FROM trip_workspaces WHERE workspace_id = 'migration-workspace'"
                )
                == "legacy-import-z"
            )
            assert await check.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'trip_workspaces' AND column_name = 'current_import_id'
                )
                """
            )
            assert await check.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'final_tips_artifacts'
                      AND column_name = 'generation_input_hash'
                      AND is_nullable = 'NO'
                )
                """
            )
            assert await check.fetchval("SELECT to_regclass('public.checkpoints') IS NOT NULL")
        finally:
            await check.close()

        # Existing-database path: all SQL and Checkpointer setup are idempotent.
        second = subprocess.run(
            [sys.executable, "-m", "scripts.migrate"],
            cwd=Path.cwd(),
            env=migration_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert second.returncode == 0, second.stderr
        check = await asyncpg.connect(database_url)
        try:
            assert await check.fetchval("SELECT COUNT(*) FROM applied_migrations") == 21
        finally:
            await check.close()
    finally:
        await admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1", name)
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()
