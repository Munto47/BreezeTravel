from __future__ import annotations

import hashlib
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.trip_understanding.map_render import MapRenderer
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.models import (
    ActivityRole,
    InferenceProposal,
    ProposedMention,
    ResolvedPlace,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.source_crypto import SourceCipher
from evals.trip_text_cards_v1.map_positive import (
    PositiveFixtureRouteProvider,
    load_and_validate_fixture,
)


pytestmark = pytest.mark.integration
MAP_DATA_ROOT = Path("eval_data/g01_map_positive_v1")


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


class PlanInferenceProvider:
    def __init__(self, city: str, names: list[str]) -> None:
        self.city = city
        self.names = names

    async def propose(self, source_text: str) -> InferenceProposal:
        mentions = []
        cursor = 0
        for index, name in enumerate(self.names):
            start = source_text.index(name, cursor)
            cursor = start + len(name)
            mentions.append(
                ProposedMention(
                    mention_id=f"map-positive-{index}",
                    raw_text=name,
                    span_start=start,
                    span_end=cursor,
                    role=ActivityRole.PLANNED,
                    day_index=1,
                    sequence_index=index,
                    atomic_place_name=name,
                    category_hint="景点",
                )
            )
        return InferenceProposal(
            source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            destination_name=self.city,
            mentions=mentions,
            binding={
                "provider": "g01-map-positive-fixture-inference",
                "external_calls": 0,
            },
        )


class PlanPlaceResolver:
    def __init__(self, city: str, place_ids: dict[str, str]) -> None:
        self.city = city
        self.place_ids = place_ids

    async def resolve(
        self,
        *,
        city: str,
        atomic_place_name: str,
        category_hint: str | None = None,
    ) -> ResolvedPlace | None:
        del category_hint
        if city != self.city or atomic_place_name not in self.place_ids:
            return None
        return ResolvedPlace(
            canonical_place_id=self.place_ids[atomic_place_name],
            name=atomic_place_name,
            category="景点",
            area_or_address=f"{city}·受控fixture",
            provider_binding={
                "provider": "g01-map-positive-place-fixture",
                "external_calls": 0,
                "authority": "NON_LIVE_SYNTHETIC_PLACE_FACT",
            },
        )


@pytest.mark.asyncio
async def test_postgres_persists_30_ready_snapshots_and_120_usable_edges() -> None:
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    fixture, fixture_validation = load_and_validate_fixture(MAP_DATA_ROOT)
    fixture_sha256 = hashlib.sha256((MAP_DATA_ROOT / "fixture.json").read_bytes()).hexdigest()
    database_name = f"breezetravel_g01_map_{uuid4().hex[:10]}"
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

        pool = await asyncpg.create_pool(database_dsn, min_size=2, max_size=6)
        repository = PostgresTripUnderstandingRepository(
            pool,
            SourceCipher("g01-map-positive-postgres-secret"),
        )
        owner_user_id = str(uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, nickname) VALUES ($1, 'G01 map matrix owner')",
                owner_user_id,
            )
        now = datetime.now(timezone.utc)
        for index, plan in enumerate(fixture.plans):
            names = [stop.name for stop in plan.stops]
            source_text = f"{plan.city}路线。Day 1：{'、'.join(names)}。"
            request_hash = canonical_sha256(
                {"mode": "FULL", "source": {"type": "TEXT", "text": source_text}}
            )
            await repository.create_full(
                owner_user_id=owner_user_id,
                source_text=source_text,
                idempotency_key=f"g01-map-positive-{index:02d}",
                request_hash=request_hash,
                now=now,
                retention_days=30,
            )
            job = await repository.claim_next(
                worker_id="g01-map-positive-understanding-worker",
                now=now,
                lease_seconds=30,
            )
            assert job is not None
            source = await repository.load_source(job, now=now)
            place_ids = {stop.name: stop.canonical_place_id for stop in plan.stops}
            output = await TripUnderstandingPipeline(
                PlanInferenceProvider(plan.city, names),
                PlanPlaceResolver(plan.city, place_ids),
            ).run(source.text)
            assert output.public_result.status == "READY"
            assert await repository.complete_job(job, output, now=now) is False

        route_provider = PositiveFixtureRouteProvider(fixture, fixture_sha256)
        map_worker = MapRenderWorker(
            repository,
            renderer=MapRenderer(route_provider),
            lease_seconds=30,
        )
        durations = []
        for index in range(30):
            started = time.perf_counter()
            assert await map_worker.run_once(f"g01-postgres-map-{index:02d}", now=now)
            durations.append((time.perf_counter() - started) * 1000)
        assert not await map_worker.run_once("g01-postgres-map-drain", now=now)

        async with pool.acquire() as conn:
            counts = {
                "jobs": await conn.fetchval("SELECT COUNT(*) FROM trip_map_render_jobs"),
                "ready_jobs": await conn.fetchval(
                    "SELECT COUNT(*) FROM trip_map_render_jobs WHERE status = 'READY'"
                ),
                "snapshots": await conn.fetchval("SELECT COUNT(*) FROM trip_map_render_snapshots"),
                "ready_snapshots": await conn.fetchval(
                    "SELECT COUNT(*) FROM trip_map_render_snapshots WHERE status = 'READY'"
                ),
                "edges": await conn.fetchval("SELECT COUNT(*) FROM trip_map_route_edges"),
                "usable_edges": await conn.fetchval(
                    "SELECT COUNT(*) FROM trip_map_route_edges WHERE selected_mode IS NOT NULL"
                ),
                "mode_facts": await conn.fetchval("SELECT COUNT(*) FROM trip_map_route_mode_facts"),
                "available_mode_facts": await conn.fetchval(
                    "SELECT COUNT(*) FROM trip_map_route_mode_facts WHERE status = 'AVAILABLE'"
                ),
                "provider_effects": await conn.fetchval(
                    "SELECT COUNT(*) FROM trip_map_provider_effect_receipts"
                ),
                "external_calls": await conn.fetchval(
                    "SELECT COALESCE(SUM(external_call_count), 0) FROM trip_map_provider_effect_receipts"
                ),
            }
            assert await conn.fetchval("SELECT to_regclass('public.rooms') IS NOT NULL")
        assert fixture_validation["plan_count"] == counts["jobs"] == counts["ready_jobs"] == 30
        assert counts["snapshots"] == counts["ready_snapshots"] == 30
        assert fixture_validation["edge_count"] == counts["edges"] == counts["usable_edges"] == 120
        assert counts["mode_facts"] == counts["available_mode_facts"] == 240
        assert counts["provider_effects"] == 240
        assert counts["external_calls"] == 0
        assert len(route_provider.requests) == len(set(route_provider.requests)) == 240
        assert _p95(durations) <= 15_000
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
