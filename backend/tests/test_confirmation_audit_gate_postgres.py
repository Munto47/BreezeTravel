from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.audit.repositories import PostgresAuditRepository
from app.audit.service import AuditApplicationService
from app.itineraries.command_service import RevisionCommandService
from app.itineraries.errors import CurrentAuditRequiredError
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    EditOperation,
    ItineraryDay,
    ItineraryEditCommand,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
)
from app.itineraries.repositories import PostgresItineraryRepository
from app.itineraries.revision_service import RevisionService


pytestmark = pytest.mark.integration


def _admin_dsn() -> str:
    return os.getenv("TEST_DATABASE_ADMIN_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")


@pytest.mark.asyncio
async def test_postgres_confirmation_requires_and_locks_current_full_audit():
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name = f"breezetravel_confirm_{uuid4().hex[:12]}"
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
        env = os.environ.copy()
        env["DATABASE_URL"] = database_dsn.replace("postgresql://", "postgresql+asyncpg://")
        migrated = subprocess.run(
            [sys.executable, "-m", "scripts.migrate"], cwd=Path.cwd(), env=env,
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert migrated.returncode == 0, migrated.stderr
        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=3)
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO users(user_id, nickname) VALUES ('confirm-user', '确认测试')")
            await conn.execute(
                "INSERT INTO rooms(room_id, thread_id, trip_city, trip_days) VALUES ('confirm-room', 'confirm-thread', '北京', 2)"
            )
            await conn.execute("INSERT INTO room_members(room_id, user_id) VALUES ('confirm-room', 'confirm-user')")

        itineraries = PostgresItineraryRepository(pool)
        workspace = await RevisionService(itineraries).create_workspace(
            room_id="confirm-room",
            city="北京",
            date_range=TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
            created_by="confirm-user",
        )
        revision = with_content_hash(ItineraryRevisionContent(
            itinerary_id="confirm-itinerary",
            workspace_id=workspace.workspace_id,
            revision=1,
            source_type=RevisionSource.MANUAL,
            city="北京",
            date_range=workspace.trip_date_range,
            days=[
                ItineraryDay(day_index=0, date=date(2026, 9, 1), stops=[ItineraryStop(
                    stop_id="confirm-stop", place_id="confirm-place", day_index=0, order_index=0,
                    start_time="09:00", end_time="11:00",
                )]),
                ItineraryDay(day_index=1, date=date(2026, 9, 2), stops=[]),
            ],
            created_by="confirm-user",
        ))
        await itineraries.attach_initial_revision(workspace.workspace_id, revision)
        audits = PostgresAuditRepository(pool)
        service = RevisionCommandService(itineraries, audit_repository=audits)
        command = ItineraryEditCommand(
            command_id="confirm-command",
            workspace_id=workspace.workspace_id,
            base_revision=1,
            actor_user_id="confirm-user",
            operation=EditOperation.CONFIRM,
        )

        with pytest.raises(CurrentAuditRequiredError):
            await service.apply(command, if_match_revision=1, idempotency_key="confirm-no-audit")
        assert (await itineraries.get_workspace(workspace.workspace_id)).current_itinerary_revision == 1

        report = await AuditApplicationService(
            itinerary_repository=itineraries,
            audit_repository=audits,
        ).run_current_audit(workspace.workspace_id, now=datetime(2026, 8, 20, tzinfo=timezone.utc))
        accepted = await service.apply(command, if_match_revision=1, idempotency_key="confirm-with-audit")
        replay = await service.apply(command, if_match_revision=1, idempotency_key="confirm-with-audit")
        persisted = await itineraries.get_workspace(workspace.workspace_id)

        assert report.itinerary_revision == 1
        assert accepted.new_revision == 2
        assert replay.idempotent_replay is True
        assert persisted.status.value == "CONFIRMED"
        assert persisted.current_itinerary_revision == 2
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1", database_name)
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
