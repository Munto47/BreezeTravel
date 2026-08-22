from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.importing.entity_resolver import EntityResolver
from app.importing.errors import ImportStateConflictError
from app.importing.models import ImportSourceType
from app.importing.repositories import PostgresImportRepository
from app.importing.service import ImportApplicationService
from app.itineraries.errors import InvalidEditCommandError
from app.itineraries.models import TripDateRange
from app.itineraries.repositories import PostgresItineraryRepository
from app.itineraries.revision_service import RevisionService


pytestmark = pytest.mark.integration


class AmbiguousProvider:
    async def search(self, *, query: str, city: str):
        return [
            {
                "place_id": place_id,
                "name": query,
                "city": city,
                "category": "attraction",
                "address": "受控测试地址",
                "coords": {"lng": 116.397, "lat": 39.918},
                "retrieval_provider": "controlled_test",
                "retrieval_request_hash": "4" * 64,
                "execution_mode": "fixture",
                "retrieval_response_hash": "d" * 64,
                "retrieval_observed_at": "2026-08-21T00:00:00+00:00",
            }
            for place_id in ("candidate-a", "candidate-b")
        ]


class WrongCityProvider:
    async def search(self, *, query: str, city: str):
        return [
            {
                "place_id": "shanghai-tower",
                "provider_place_id": "amap-shanghai-tower",
                "name": query,
                "city": "上海市",
                "district": "浦东新区",
                "category": "attraction",
                "address": "世纪大道1号",
                "coords": {"lng": 121.4997, "lat": 31.2397},
                "retrieval_provider": "controlled_test",
                "retrieval_request_hash": "3" * 64,
                "execution_mode": "fixture",
                "retrieval_response_hash": "c" * 64,
                "retrieval_observed_at": "2026-08-21T00:00:00+00:00",
                "source_url": "https://example.test/provider/shanghai-tower",
            }
        ]


def _admin_dsn() -> str:
    return os.getenv("TEST_DATABASE_ADMIN_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")


@pytest.mark.asyncio
async def test_postgres_import_compare_and_set_and_apply_replay():
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name = f"breezetravel_import_mobile_{uuid4().hex[:10]}"
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
            await conn.execute("INSERT INTO users(user_id, nickname) VALUES ('mobile-pg-user', 'Mobile PG')")
            await conn.execute(
                "INSERT INTO rooms(room_id, thread_id, trip_city, trip_days) VALUES ('mobile-pg-room', 'mobile-pg-thread', '北京', 2)"
            )
            await conn.execute("INSERT INTO room_members(room_id, user_id) VALUES ('mobile-pg-room', 'mobile-pg-user')")
        itineraries = PostgresItineraryRepository(pool)
        workspace = await RevisionService(itineraries).create_workspace(
            room_id="mobile-pg-room",
            city="北京",
            date_range=TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2)),
            created_by="mobile-pg-user",
        )
        imports = PostgresImportRepository(pool)
        service = ImportApplicationService(
            import_repository=imports,
            itinerary_repository=itineraries,
            entity_resolver=EntityResolver(AmbiguousProvider()),
        )
        draft = await service.create_import(
            workspace_id=workspace.workspace_id,
            source_type=ImportSourceType.AI_TEXT,
            raw_text="第1天：故宫博物院\n第2天：颐和园",
            actor_user_id="mobile-pg-user",
        )
        target = draft.resolutions[0]

        async def confirm(place_id: str):
            return await imports.confirm_resolutions(
                draft.import_id,
                {target.raw_stop_id: place_id},
                "mobile-pg-user",
                draft.state_version,
            )

        results = await asyncio.gather(
            confirm(target.candidates[0].place_id),
            confirm(target.candidates[1].place_id),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in results) == 1
        assert sum(isinstance(item, ImportStateConflictError) for item in results) == 1

        current = await imports.get_import(draft.import_id)
        remaining = next(item for item in current.resolutions if item.raw_stop_id != target.raw_stop_id)
        current = await imports.confirm_resolutions(
            current.import_id,
            {remaining.raw_stop_id: remaining.candidates[0].place_id},
            "mobile-pg-user",
            current.state_version,
        )
        applied = await service.apply_import(
            current.import_id,
            actor_user_id="mobile-pg-user",
            expected_state_version=current.state_version,
            idempotency_key="pg-mobile-apply",
        )
        replay = await service.apply_import(
            current.import_id,
            actor_user_id="mobile-pg-user",
            expected_state_version=current.state_version,
            idempotency_key="pg-mobile-apply",
        )
        assert replay.idempotent_replay is True
        assert replay.revision.content_hash == applied.revision.content_hash

        rejected_draft = await ImportApplicationService(
            import_repository=imports,
            itinerary_repository=itineraries,
            entity_resolver=EntityResolver(WrongCityProvider()),
        ).create_import(
            workspace_id=workspace.workspace_id,
            source_type=ImportSourceType.MANUAL_TEXT,
            raw_text="第1天：09:00-11:00 东方明珠",
            actor_user_id="mobile-pg-user",
        )
        rejected_resolution = rejected_draft.resolutions[0]
        assert rejected_resolution.candidates == []
        assert len(rejected_resolution.rejected_candidates) == 1

        # Simulate an application process/repository restart.  The GET path must
        # reconstruct the rejection receipt exclusively from PostgreSQL.
        restarted_imports = PostgresImportRepository(pool)
        readback = await restarted_imports.get_import(rejected_draft.import_id)
        assert readback is not None
        restored = readback.resolutions[0]
        assert restored.candidates == []
        assert restored.rejected_candidates == rejected_resolution.rejected_candidates
        receipt = restored.rejected_candidates[0].resolved_place_receipt
        assert receipt.provider_place_id == "amap-shanghai-tower"
        assert receipt.request_hash == "3" * 64
        assert receipt.response_hash == "c" * 64
        assert receipt.source_url == "https://example.test/provider/shanghai-tower"
        with pytest.raises(
            InvalidEditCommandError,
            match="not an offered resolution candidate",
        ):
            await restarted_imports.confirm_resolution(
                rejected_draft.import_id,
                restored.raw_stop_id,
                restored.rejected_candidates[0].place_id,
                "mobile-pg-user",
                readback.state_version,
            )

        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM itinerary_import_commands WHERE import_id = $1",
                    current.import_id,
                )
                == 1
            )
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM itinerary_revisions WHERE workspace_id = $1",
                    workspace.workspace_id,
                )
                == 1
            )
            stored_candidates = await conn.fetchval(
                """
                SELECT candidates_json FROM itinerary_stop_resolutions
                WHERE import_id = $1 AND raw_stop_id = $2
                """,
                rejected_draft.import_id,
                restored.raw_stop_id,
            )
            if isinstance(stored_candidates, str):
                stored_candidates = json.loads(stored_candidates)
            assert stored_candidates["schema_version"] == 2
            assert stored_candidates["candidates"] == []
            assert stored_candidates["rejected_candidates"][0]["resolved_place_receipt"]["response_hash"] == "c" * 64
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
