from __future__ import annotations

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
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.route_geometry import InMemoryRouteGeometryCache
from app.trip_understanding.service import DEMO_CREATE_REQUEST_HASH, TripUnderstandingApplicationService
from app.trip_understanding.source_crypto import SourceCipher


pytestmark = pytest.mark.integration


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


@pytest.mark.asyncio
async def test_g02_postgres_persists_snapshot_selection_restart_and_zero_hidden_routes() -> None:
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name = f"breezetravel_g02_{uuid4().hex[:10]}"
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
            for migration in sorted(Path("app/db/migrations").glob("*.sql")):
                await migration_connection.execute(migration.read_text(encoding="utf-8"))
                await migration_connection.execute(
                    "INSERT INTO applied_migrations(filename) VALUES ($1) ON CONFLICT DO NOTHING",
                    migration.name,
                )
        finally:
            await migration_connection.close()

        pool = await asyncpg.create_pool(database_dsn, min_size=2, max_size=4)
        geometry_cache = InMemoryRouteGeometryCache()
        repository = PostgresTripUnderstandingRepository(
            pool,
            SourceCipher("g02-postgres-root-secret"),
            geometry_cache,
        )
        now = datetime.now(timezone.utc)
        created = await repository.create_demo(
            capability_hash="a" * 64,
            idempotency_key="g02-postgres-create",
            request_hash=DEMO_CREATE_REQUEST_HASH,
            now=now,
            ttl_hours=24,
        )
        job = await repository.claim_next(
            worker_id="g02-understanding",
            now=now,
            lease_seconds=30,
        )
        assert job is not None
        await repository.complete_job(job, await build_demo_pipeline().run(DEMO_SOURCE_TEXT), now=now)
        resource = await repository.authorize(
            created.accepted.public_resource_id,
            capability_hash="a" * 64,
            now=now,
        )
        stored = await repository.get_result(resource)
        assert stored is not None
        assert stored.result.stay.status == "UNAVAILABLE"
        assert (await repository.get_stay_view(resource)).status == "PREPARING"
        worker = MapRenderWorker(repository)
        assert await worker.run_once("g02-map", now=now)
        assert await worker.run_once("g02-stay", now=now)

        cached_geometry = {
            reference: [dict(point) for point in points]
            for reference, points in geometry_cache._items.items()
        }
        for reference in cached_geometry:
            geometry_cache.expire(reference)
        degraded_map = await repository.get_map_view(resource, now=now)
        assert degraded_map.status == "LIMITED"
        assert all(route.message for day in degraded_map.days for route in day.routes)
        for points in cached_geometry.values():
            await geometry_cache.put(points)

        stay_view = await repository.get_stay_view(resource)
        assert stay_view.status in {"AVAILABLE", "LIMITED"}
        assert 1 <= len(stay_view.candidates) <= 3
        candidate = stay_view.candidates[0]
        map_jobs_before = await pool.fetchval("SELECT COUNT(*) FROM trip_map_render_jobs")
        route_calls_before = await pool.fetchval(
            "SELECT COALESCE(SUM(external_call_count), 0) FROM trip_map_provider_effect_receipts"
        )
        service = TripUnderstandingApplicationService(repository)
        selected = await service.select_stay(
            resource,
            candidate_token=candidate.candidate_token,
            expected_etag=stored.opaque_etag,
            idempotency_key="g02-postgres-select",
            now=now,
        )
        assert selected.applied.overnight_days == ["Day 1", "Day 2"]
        replay = await service.select_stay(
            resource,
            candidate_token=candidate.candidate_token,
            expected_etag=stored.opaque_etag,
            idempotency_key="g02-postgres-select",
            now=now,
        )
        assert replay.replayed is True
        assert replay.opaque_etag == selected.opaque_etag
        with pytest.raises(RevisionConflictError):
            await service.select_stay(
                resource,
                candidate_token=candidate.candidate_token,
                expected_etag=stored.opaque_etag,
                idempotency_key="g02-postgres-stale",
                now=now,
            )

        restarted = PostgresTripUnderstandingRepository(
            pool,
            SourceCipher("g02-postgres-root-secret"),
            geometry_cache,
        )
        current_resource = await restarted.authorize(
            created.accepted.public_resource_id,
            capability_hash="a" * 64,
            now=now,
        )
        current = await restarted.get_result(current_resource)
        assert current is not None
        assert current.opaque_etag == selected.opaque_etag
        assert current.result.map.status == "NEEDS_UPDATE"
        assert current.result.stay.candidates[0].selected is True
        assert await pool.fetchval("SELECT COUNT(*) FROM trip_map_render_jobs") == map_jobs_before
        rerender = await service.request_map_render(
            current_resource,
            expected_etag=selected.opaque_etag,
            idempotency_key="g02-postgres-map-after-stay",
            now=now,
        )
        assert rerender.accepted.status == "PREPARING"
        assert await worker.run_once("g02-map-after-stay", now=now)
        refreshed_map = await restarted.get_map_view(current_resource, now=now)
        assert refreshed_map.status == "AVAILABLE"
        selected_name = current.result.stay.candidates[0].name
        overnight_routes = [
            route
            for day in refreshed_map.days[:2]
            for route in day.routes
        ]
        assert sum(route.from_name == selected_name for route in overnight_routes) == 2
        assert sum(route.to_name == selected_name for route in overnight_routes) == 2

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM applied_migrations WHERE filename = '030_stay_recommendation_snapshots.sql')"
            )
            assert await conn.fetchval("SELECT COUNT(*) FROM trip_stay_recommendation_snapshots") == 1
            assert 1 <= await conn.fetchval("SELECT COUNT(*) FROM trip_stay_candidates") <= 12
            assert await conn.fetchval("SELECT COUNT(*) FROM trip_stay_selections") == 1
            assert await conn.fetchval("SELECT COUNT(*) FROM trip_understanding_revisions") == 3
            assert await conn.fetchval("SELECT COUNT(*) FROM trip_map_render_jobs") == map_jobs_before + 1
            assert await conn.fetchval(
                "SELECT COALESCE(SUM(external_call_count), 0) FROM trip_map_provider_effect_receipts"
            ) == route_calls_before
            candidate_id = await conn.fetchval("SELECT candidate_id FROM trip_stay_candidates LIMIT 1")
            with pytest.raises(asyncpg.RaiseError, match="immutable"):
                await conn.execute(
                    "UPDATE trip_stay_candidates SET name = name WHERE candidate_id = $1",
                    candidate_id,
                )
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
