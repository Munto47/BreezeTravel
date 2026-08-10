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
            assert await check.fetchval("SELECT COUNT(*) FROM applied_migrations") == 8
            assert await check.fetchval("SELECT to_regclass('public.verification_reports') IS NOT NULL")
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
            assert await check.fetchval("SELECT COUNT(*) FROM applied_migrations") == 8
        finally:
            await check.close()
    finally:
        await admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1", name)
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()
