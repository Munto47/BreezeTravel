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

from app.itineraries.errors import RevisionConflictError
from app.itineraries.models import TripDateRange
from app.itineraries.repositories import PostgresItineraryRepository
from app.itineraries.revision_service import RevisionService
from app.members.models import (
    ConstraintConfirmationStatus,
    ConstraintHardness,
    ConstraintSource,
    MemberConstraintDraft,
    TravelerProfile,
)
from app.members.repositories import PostgresMemberConstraintRepository
from app.members.service import MemberConstraintService


pytestmark = pytest.mark.integration


def _admin_dsn() -> str:
    return os.getenv("TEST_DATABASE_ADMIN_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")


def _draft(constraint_id: str) -> MemberConstraintDraft:
    return MemberConstraintDraft(
        constraint_id=constraint_id,
        owner_member_id="member-pg-user",
        type="latest_return_time",
        operator="LTE",
        value="20:00",
        hardness=ConstraintHardness.HARD,
        priority=100,
        source=ConstraintSource.MEMBER_EXPLICIT,
        confirmation_status=ConstraintConfirmationStatus.CONFIRMED,
        waivable_by=["member-pg-user"],
    )


@pytest.mark.asyncio
async def test_postgres_member_constraint_append_concurrency_and_reconstruction():
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name = f"breezetravel_members_{uuid4().hex[:12]}"
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
            [sys.executable, "-m", "scripts.migrate"],
            cwd=Path.cwd(),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert migrated.returncode == 0, migrated.stderr

        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=4)
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO users(user_id, nickname) VALUES ('member-pg-user', '成员')")
            await conn.execute(
                "INSERT INTO rooms(room_id, thread_id, trip_city, trip_days) "
                "VALUES ('member-pg-room', 'member-pg-thread', '北京', 2)"
            )
            await conn.execute(
                "INSERT INTO room_members(room_id, user_id) VALUES ('member-pg-room', 'member-pg-user')"
            )
        workspace = await RevisionService(PostgresItineraryRepository(pool)).create_workspace(
            room_id="member-pg-room",
            city="北京",
            date_range=TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
            created_by="member-pg-user",
        )
        repository = PostgresMemberConstraintRepository(pool)
        service = MemberConstraintService(repository)
        profile = TravelerProfile(
            workspace_id=workspace.workspace_id,
            member_id="member-pg-user",
            display_name="成员",
            age_group="adult",
            walking_limit_minutes=120,
        )
        await service.save_profile(profile)
        assert await repository.get_profile(workspace.workspace_id, "member-pg-user") == profile

        results = await asyncio.gather(
            service.write_constraint(workspace.workspace_id, _draft("return-a"), expected_base_revision=0),
            service.write_constraint(workspace.workspace_id, _draft("return-b"), expected_base_revision=0),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in results) == 1
        assert sum(isinstance(item, RevisionConflictError) for item in results) == 1
        winner = next(item for item in results if not isinstance(item, BaseException))
        await service.write_constraint(
            workspace.workspace_id,
            _draft(winner.constraint.constraint_id).model_copy(update={"value": "19:30"}),
            expected_base_revision=1,
        )
        assert (await repository.list_effective_constraints(workspace.workspace_id, 1))[0].value == "20:00"
        effective = await repository.list_effective_constraints(workspace.workspace_id, 2)
        assert len(effective) == 1
        assert effective[0].value == "19:30"
        current = await PostgresItineraryRepository(pool).get_workspace(workspace.workspace_id)
        assert current.current_member_constraint_revision == 2
        assert current.current_report_id is None
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        await admin.close()
