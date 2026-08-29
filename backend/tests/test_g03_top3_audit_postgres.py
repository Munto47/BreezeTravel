from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.errors import RevisionConflictError
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.models import AssumptionSetCommand
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.route_geometry import InMemoryRouteGeometryCache
from app.trip_understanding.service import (
    DEMO_CREATE_REQUEST_HASH,
    TripUnderstandingApplicationService,
)
from app.trip_understanding.source_crypto import SourceCipher


pytestmark = pytest.mark.integration


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


@pytest.mark.asyncio
async def test_g03_postgres_old_db_upgrade_materialize_concurrent_adopt_and_postcheck() -> None:
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name = f"breezetravel_g03_{uuid4().hex[:10]}"
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

        migrations = sorted(Path("app/db/migrations").glob("*.sql"))
        migration_connection = await asyncpg.connect(database_dsn)
        try:
            await migration_connection.execute(
                """
                CREATE TABLE IF NOT EXISTS applied_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            for migration in migrations:
                if migration.name >= "031_day_index_trip_bridge.sql":
                    continue
                await migration_connection.execute(
                    migration.read_text(encoding="utf-8")
                )
                await migration_connection.execute(
                    "INSERT INTO applied_migrations(filename) VALUES ($1)",
                    migration.name,
                )
        finally:
            await migration_connection.close()

        pool = await asyncpg.create_pool(database_dsn, min_size=2, max_size=5)
        repository = PostgresTripUnderstandingRepository(
            pool,
            SourceCipher("g03-postgres-root-secret"),
            InMemoryRouteGeometryCache(),
        )
        service = TripUnderstandingApplicationService(repository)
        now = datetime.now(timezone.utc)
        created = await repository.create_demo(
            capability_hash="b" * 64,
            idempotency_key="g03-postgres-create",
            request_hash=DEMO_CREATE_REQUEST_HASH,
            now=now,
            ttl_hours=24,
        )
        job = await repository.claim_next(
            worker_id="g03-understanding",
            now=now,
            lease_seconds=30,
        )
        assert job is not None
        await repository.complete_job(
            job,
            await build_demo_pipeline().run(DEMO_SOURCE_TEXT),
            now=now,
        )
        resource = await repository.authorize(
            created.accepted.public_resource_id,
            capability_hash="b" * 64,
            now=now,
        )
        first_result = await repository.get_result(resource)
        assert first_result is not None
        worker = MapRenderWorker(repository)
        assert await worker.run_once("g03-map", now=now)
        assert await worker.run_once("g03-stay", now=now)
        stays = await repository.get_stay_view(resource)

        migration_031 = next(
            item for item in migrations if item.name == "031_day_index_trip_bridge.sql"
        )
        migration_connection = await asyncpg.connect(database_dsn)
        try:
            migration_sql = migration_031.read_text(encoding="utf-8")
            await migration_connection.execute(migration_sql)
            await migration_connection.execute(migration_sql)
            await migration_connection.execute(
                """
                INSERT INTO applied_migrations(filename) VALUES ($1)
                ON CONFLICT DO NOTHING
                """,
                migration_031.name,
            )
        finally:
            await migration_connection.close()

        ready_resource = await repository.authorize(
            created.accepted.public_resource_id,
            capability_hash="b" * 64,
            now=now,
        )
        ready_result = await repository.get_result(ready_resource)
        assert ready_result is not None
        assert ready_result.opaque_etag == first_result.opaque_etag
        route_calls_before = await pool.fetchval(
            """
            SELECT COALESCE(SUM(external_call_count), 0)
            FROM trip_map_provider_effect_receipts
            """
        )
        map_jobs_before = await pool.fetchval(
            "SELECT COUNT(*) FROM trip_map_render_jobs"
        )

        ready_materialized = await service.materialize_trip(
            ready_resource,
            expected_etag=ready_result.opaque_etag,
            idempotency_key="g03-materialize-ready-map",
            now=now,
        )
        assert ready_materialized.view.calendar == "按 Day 编号安排"
        assert ready_materialized.view.party_size == 2
        replay = await service.materialize_trip(
            ready_resource,
            expected_etag=ready_result.opaque_etag,
            idempotency_key="g03-materialize-ready-map",
            now=now,
        )
        assert replay.replayed is True
        second_key = await service.materialize_trip(
            ready_resource,
            expected_etag=ready_result.opaque_etag,
            idempotency_key="g03-materialize-ready-map-second-key",
            now=now,
        )
        assert second_key.view == ready_materialized.view
        assert await pool.fetchval("SELECT COUNT(*) FROM itinerary_revisions") == 1
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM trip_materialization_lineage"
        ) == 1
        workspace = await pool.fetchrow(
            """
            SELECT calendar_mode, trip_start_date, trip_end_date, party_size,
                   party_size_source, current_plan_ref_id
            FROM trip_workspaces
            """
        )
        assert workspace["calendar_mode"] == "DAY_INDEX_ONLY"
        assert workspace["trip_start_date"] is None
        assert workspace["trip_end_date"] is None
        assert workspace["party_size"] == 2
        assert workspace["party_size_source"] == "DEFAULT_TWO"
        assert workspace["current_plan_ref_id"]
        assert await pool.fetchval("SELECT COUNT(*) FROM trip_map_render_jobs") == map_jobs_before
        binding_integrity = await pool.fetchrow(
            """
            SELECT COUNT(*) AS binding_count,
                   bool_and(receipt_binding_complete) AS receipts_complete,
                   bool_and(length(receipt_set_sha256) = 64) AS hashes_complete
            FROM trip_g03_evidence_bindings
            """
        )
        assert binding_integrity["binding_count"] >= 1
        assert binding_integrity["receipts_complete"] is True
        assert binding_integrity["hashes_complete"] is True

        ready_checks = await service.get_trip_checks(ready_resource)
        assert len(ready_checks.items) == 3
        assert all(item.can_preview for item in ready_checks.items)

        selected = await service.select_stay(
            ready_resource,
            candidate_token=stays.candidates[0].candidate_token,
            expected_etag=ready_result.opaque_etag,
            idempotency_key="g03-select-stay",
            now=now,
        )
        current_resource = await repository.authorize(
            created.accepted.public_resource_id,
            capability_hash="b" * 64,
            now=now,
        )
        current = await repository.get_result(current_resource)
        assert current is not None
        assert current.opaque_etag == selected.opaque_etag

        materialized = await service.materialize_trip(
            current_resource,
            expected_etag=current.opaque_etag,
            idempotency_key="g03-materialize-selected-stay",
            now=now,
        )
        assert materialized.view.calendar == "按 Day 编号安排"
        assert materialized.view.party_size == 2
        selected_replay = await service.materialize_trip(
            current_resource,
            expected_etag=current.opaque_etag,
            idempotency_key="g03-materialize-selected-stay",
            now=now,
        )
        assert selected_replay.replayed is True
        assert await pool.fetchval("SELECT COUNT(*) FROM itinerary_revisions") == 2
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM trip_materialization_lineage"
        ) == 2

        checks = await service.get_trip_checks(current_resource)
        assert len(checks.items) == 3
        assert all(item.can_preview for item in checks.items)
        assert all(item.label == "可以更好" for item in checks.items)
        assert await pool.fetchval(
            """
            SELECT COUNT(*) FROM audit_findings
            WHERE status IN ('VIOLATED', 'UNKNOWN')
            """
        ) > len(checks.items)
        assert await pool.fetchval(
            """
            SELECT COUNT(*) FROM audit_findings
            WHERE reason_code = 'PROVIDER_RESULT_UNAVAILABLE' AND status = 'UNKNOWN'
            """
        ) >= 1

        previews = [
            await service.preview_trip_change(
                current_resource,
                check_token=item.check_token,
                idempotency_key=f"g03-preview-{index}",
                now=now,
            )
            for index, item in enumerate(checks.items[:2])
        ]

        async def adopt(index: int):
            return await service.adopt_trip_change(
                current_resource,
                change_token=previews[index].preview.change_token,
                expected_etag=current.opaque_etag,
                idempotency_key=f"g03-adopt-{index}",
                now=now,
            )

        concurrent = await asyncio.gather(adopt(0), adopt(1), return_exceptions=True)
        successful = [item for item in concurrent if not isinstance(item, Exception)]
        failed = [item for item in concurrent if isinstance(item, Exception)]
        assert len(successful) == 1, concurrent
        assert len(failed) == 1
        assert isinstance(failed[0], RevisionConflictError)
        adopted = successful[0]
        winner_index = 0 if concurrent[0] is adopted else 1
        assert adopted.adopted.status == "STILL_NEEDS_CONFIRMATION"
        assert adopted.adopted.map_readiness == "NEEDS_UPDATE"
        assert adopted.opaque_etag != current.opaque_etag

        adopted_replay = await service.adopt_trip_change(
            current_resource,
            change_token=previews[winner_index].preview.change_token,
            expected_etag=current.opaque_etag,
            idempotency_key=f"g03-adopt-{winner_index}",
            now=now,
        )
        assert adopted_replay.replayed is True
        assert adopted_replay.opaque_etag == adopted.opaque_etag

        assert await pool.fetchval("SELECT COUNT(*) FROM itinerary_revisions") == 3
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM trip_materialization_lineage"
        ) == 3
        assert await pool.fetchval(
            """
            SELECT bool_and(postcheck_complete)
            FROM trip_materialization_lineage
            """
        ) is True
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM trip_stay_selections"
        ) == 2
        assert await pool.fetchval(
            """
            SELECT COUNT(*) FROM trip_change_previews
            WHERE status = 'APPLIED'
            """
        ) == 1
        assert await pool.fetchval(
            """
            SELECT COUNT(*) FROM trip_change_previews
            WHERE status = 'STALE'
            """
        ) == 1
        assert await pool.fetchval(
            """
            SELECT COALESCE(SUM(external_call_count), 0)
            FROM trip_map_provider_effect_receipts
            """
        ) == route_calls_before
        fresh_resource = await repository.authorize(
            created.accepted.public_resource_id,
            capability_hash="b" * 64,
            now=now,
        )
        assert (await repository.get_map_view(fresh_resource, now=now)).status == "NEEDS_UPDATE"

        calendar_edit = await service.apply_command(
            fresh_resource,
            AssumptionSetCommand(
                command_type="ASSUMPTION_SET",
                key="calendar",
                value="2026-09-01 至 2026-09-03",
            ),
            expected_etag=adopted.opaque_etag,
            idempotency_key="g03-calendar-edit",
            now=now,
        )
        calendar_resource = await repository.authorize(
            created.accepted.public_resource_id,
            capability_hash="b" * 64,
            now=now,
        )
        party_edit = await service.apply_command(
            calendar_resource,
            AssumptionSetCommand(
                command_type="ASSUMPTION_SET",
                key="party_size",
                value="4 人",
            ),
            expected_etag=calendar_edit.opaque_etag,
            idempotency_key="g03-party-edit",
            now=now,
        )
        dated_resource = await repository.authorize(
            created.accepted.public_resource_id,
            capability_hash="b" * 64,
            now=now,
        )
        dated = await service.materialize_trip(
            dated_resource,
            expected_etag=party_edit.opaque_etag,
            idempotency_key="g03-materialize-absolute-dates",
            now=now,
        )
        assert dated.view.calendar == "2026-09-01 至 2026-09-03"
        assert dated.view.party_size == 4
        absolute_revision = await pool.fetchrow(
            """
            SELECT calendar_mode, trip_start_date, trip_end_date,
                   party_size, party_size_source,
                   days_json #>> '{0,date}' AS first_day,
                   days_json #>> '{2,date}' AS last_day
            FROM itinerary_revisions
            ORDER BY revision DESC
            LIMIT 1
            """
        )
        assert absolute_revision["calendar_mode"] == "ABSOLUTE_DATES"
        assert absolute_revision["trip_start_date"].isoformat() == "2026-09-01"
        assert absolute_revision["trip_end_date"].isoformat() == "2026-09-03"
        assert absolute_revision["party_size"] == 4
        assert absolute_revision["party_size_source"] == "USER_PROVIDED"
        assert absolute_revision["first_day"] == "2026-09-01"
        assert absolute_revision["last_day"] == "2026-09-03"
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM trip_materialization_lineage"
        ) == 4
        assert await pool.fetchval(
            """
            SELECT COALESCE(SUM(external_call_count), 0)
            FROM trip_map_provider_effect_receipts
            """
        ) == route_calls_before
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
