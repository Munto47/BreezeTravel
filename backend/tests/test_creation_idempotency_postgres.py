from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.itineraries.errors import IdempotencyKeyReusedError
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import TripDateRange, TripWorkspace
from app.itineraries.repositories import PostgresItineraryRepository
from app.operations.errors import IdempotencyRequestInProgressError
from app.operations.models import CreationCommandClaim, CreationCommandResponse, CreationOperation
from app.operations.repositories import PostgresCreationCommandRepository


pytestmark = pytest.mark.integration


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


@pytest.mark.asyncio
async def test_postgres_creation_command_race_rollback_replay_and_import_pointer_delete():
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name = f"breezetravel_create_cmd_{uuid4().hex[:10]}"
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
        migration_env["DATABASE_URL"] = database_dsn.replace(
            "postgresql://",
            "postgresql+asyncpg://",
        )
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
        pool = await asyncpg.create_pool(database_dsn, min_size=2, max_size=4)
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO users(user_id, nickname) VALUES ('cmd-user', 'command')")
            await conn.execute(
                "INSERT INTO rooms(room_id, thread_id, trip_city, trip_days) "
                "VALUES ('cmd-room', 'cmd-thread', '北京', 2)"
            )
            await conn.execute("INSERT INTO room_members(room_id, user_id) VALUES ('cmd-room', 'cmd-user')")
        workspace = TripWorkspace(
            workspace_id="cmd-workspace",
            room_id="cmd-room",
            city="北京",
            trip_date_range=TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2)),
            created_by="cmd-user",
        )
        await PostgresItineraryRepository(pool).create_workspace(workspace)
        repositories = [
            PostgresCreationCommandRepository(pool, lease_seconds=60),
            PostgresCreationCommandRepository(pool, lease_seconds=60),
        ]
        request_hash = sha256_canonical(
            {
                "schema_version": 1,
                "operation": "CREATE_IMPORT",
                "workspace_id": workspace.workspace_id,
                "target_id": workspace.workspace_id,
                "actor_user_id": "cmd-user",
                "body": {"source_type": "AI_TEXT", "raw_text": "第1天：故宫"},
            }
        )

        async def claim(repository):
            return await repository.claim(
                workspace_id=workspace.workspace_id,
                operation=CreationOperation.CREATE_IMPORT,
                target_id=workspace.workspace_id,
                actor_user_id="cmd-user",
                idempotency_key="pg-create-import",
                request_hash=request_hash,
                basis={"current_import_id": None},
            )

        outcomes = await asyncio.gather(*(claim(item) for item in repositories), return_exceptions=True)
        claims = [item for item in outcomes if isinstance(item, CreationCommandClaim)]
        in_progress = [item for item in outcomes if isinstance(item, IdempotencyRequestInProgressError)]
        assert len(claims) == len(in_progress) == 1
        active = claims[0]

        async def failing_finalize(conn, _basis):
            await conn.execute(
                """
                INSERT INTO itinerary_imports (
                    import_id, workspace_id, source_type, raw_text, parse_version,
                    status, parsed_json, created_by
                ) VALUES ('rolled-back-import', $1, 'AI_TEXT', 'x', 'test', 'PARSED', '{}'::jsonb, 'cmd-user')
                """,
                workspace.workspace_id,
            )
            raise RuntimeError("fault after domain write")

        with pytest.raises(RuntimeError, match="fault after domain write"):
            await repositories[0].finalize(active, failing_finalize)
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT COUNT(*) FROM itinerary_imports WHERE import_id = 'rolled-back-import'")
                == 0
            )
        await repositories[0].abandon(active)
        replacement = await claim(repositories[1])

        async def successful_finalize(conn, basis):
            assert basis == {"current_import_id": None}
            await conn.execute(
                """
                INSERT INTO itinerary_imports (
                    import_id, workspace_id, source_type, raw_text, parse_version,
                    status, parsed_json, created_by
                ) VALUES ('durable-import', $1, 'AI_TEXT', 'x', 'test', 'PARSED', '{}'::jsonb, 'cmd-user')
                """,
                workspace.workspace_id,
            )
            await conn.execute(
                "UPDATE trip_workspaces SET current_import_id = 'durable-import' WHERE workspace_id = $1",
                workspace.workspace_id,
            )
            return CreationCommandResponse(
                status_code=201,
                body={"import_id": "durable-import"},
                headers={"ETag": '"1"'},
            )

        stored = await repositories[1].finalize(replacement, successful_finalize)
        assert stored.body == {"import_id": "durable-import"}
        replay = await claim(repositories[0])
        assert replay.replay.body == stored.body
        assert replay.replay.headers["Idempotency-Replayed"] == "true"

        with pytest.raises(IdempotencyKeyReusedError):
            await repositories[0].claim(
                workspace_id=workspace.workspace_id,
                operation=CreationOperation.CREATE_IMPORT,
                target_id=workspace.workspace_id,
                actor_user_id="cmd-user",
                idempotency_key="pg-create-import",
                request_hash=sha256_canonical({"different": True}),
                basis={"current_import_id": "durable-import"},
            )

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM itinerary_imports WHERE import_id = 'durable-import'")
            pointer = await conn.fetchval(
                "SELECT current_import_id FROM trip_workspaces WHERE workspace_id = $1",
                workspace.workspace_id,
            )
            assert pointer is None
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
