from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.errors import (
    JobLeaseLostError,
    ResourceAccessDeniedError,
    ResourceGoneError,
    RevisionConflictError,
    SourceUnavailableError,
)
from app.trip_understanding.full_text import build_full_text_pipeline
from app.trip_understanding.map_render import MapRenderer
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.models import ActivityMoveCommand
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.route_geometry import InMemoryRouteGeometryCache
from app.trip_understanding.service import DEMO_CREATE_REQUEST_HASH
from app.trip_understanding.source_crypto import SourceCipher


pytestmark = pytest.mark.integration


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


@pytest.mark.asyncio
async def test_postgres_demo_idempotency_lease_events_and_public_projection() -> None:
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name = f"breezetravel_tu3_{uuid4().hex[:10]}"
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
            migrations = sorted(Path("app/db/migrations").glob("*.sql"))
            for migration in migrations:
                await migration_connection.execute(migration.read_text(encoding="utf-8"))
                await migration_connection.execute(
                    "INSERT INTO applied_migrations(filename) VALUES ($1)",
                    migration.name,
                )
            migration_028 = Path("app/db/migrations/028_trip_understanding_v3.sql")
            await migration_connection.execute(migration_028.read_text(encoding="utf-8"))
            migration_029 = Path("app/db/migrations/029_map_render_snapshots.sql")
            await migration_connection.execute(migration_029.read_text(encoding="utf-8"))
        finally:
            await migration_connection.close()

        pool = await asyncpg.create_pool(database_dsn, min_size=2, max_size=4)
        geometry_cache = InMemoryRouteGeometryCache()
        repository = PostgresTripUnderstandingRepository(
            pool,
            SourceCipher("postgres-integration-root-secret"),
            geometry_cache,
        )
        now = datetime.now(timezone.utc)
        created = await repository.create_demo(
            capability_hash="a" * 64,
            idempotency_key="postgres-demo",
            request_hash=DEMO_CREATE_REQUEST_HASH,
            now=now,
            ttl_hours=24,
        )
        replay = await repository.create_demo(
            capability_hash="a" * 64,
            idempotency_key="postgres-demo",
            request_hash=DEMO_CREATE_REQUEST_HASH,
            now=now,
            ttl_hours=24,
        )
        assert replay.replayed is True
        assert replay.accepted == created.accepted
        with pytest.raises(ResourceAccessDeniedError):
            await repository.authorize(
                created.accepted.public_resource_id,
                capability_hash="b" * 64,
                now=now,
            )
        stale_job = await repository.claim_next(
            worker_id="postgres-reused-worker",
            now=now,
            lease_seconds=5,
        )
        assert stale_job is not None
        job = await repository.claim_next(
            worker_id="postgres-reused-worker",
            now=now + timedelta(seconds=6),
            lease_seconds=30,
        )
        assert job is not None
        assert job.attempt == 2
        output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
        await repository.fail_job(
            stale_job,
            category="STALE_FAILURE",
            now=now + timedelta(seconds=7),
        )
        with pytest.raises(JobLeaseLostError):
            await repository.complete_job(stale_job, output, now=now + timedelta(seconds=7))
        now += timedelta(seconds=7)
        assert await repository.complete_job(job, output, now=now) is False
        assert await repository.complete_job(job, output, now=now) is True

        resource = await repository.authorize(
            created.accepted.public_resource_id,
            capability_hash="a" * 64,
            now=now,
        )
        stored = await repository.get_result(resource)
        assert stored is not None
        assert [len(day.activities) for day in stored.result.days] == [2, 2, 2]
        assert stored.result.map.status == "PREPARING"
        assert await MapRenderWorker(repository).run_once(
            "postgres-map-worker",
            now=now,
        )
        map_view = await repository.get_map_view(resource, now=now)
        assert map_view.status == "AVAILABLE"
        assert [route.selected_mode for day in map_view.days for route in day.routes] == [
            "walking",
            "transit",
            "transit",
        ]
        events = await repository.list_events(resource, after_event_id=2)
        assert [item.event_type for item in events] == ["result_available"]
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_understanding_revisions WHERE understanding_id = $1",
                resource.understanding_id,
            ) == 2
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_understanding_activities WHERE understanding_id = $1",
                resource.understanding_id,
            ) == 10
            assert await conn.fetchval(
                """
                SELECT COUNT(*) FROM trip_understanding_activities
                WHERE understanding_id = $1 AND eligible_for_place_search
                """,
                resource.understanding_id,
            ) == 6
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_understanding_side_effect_receipts"
            ) == 1
            assert await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM applied_migrations WHERE filename = '028_trip_understanding_v3.sql')"
            )
            assert await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM applied_migrations WHERE filename = '029_map_render_snapshots.sql')"
            )
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_map_render_snapshots WHERE status = 'READY'"
            ) == 1
            assert await conn.fetchval("SELECT COUNT(*) FROM trip_map_route_edges") == 3
            assert await conn.fetchval("SELECT COUNT(*) FROM trip_map_route_mode_facts") == 6
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_map_provider_effect_receipts"
            ) == 6
            assert await conn.fetchval(
                "SELECT COALESCE(SUM(external_call_count), 0) FROM trip_map_provider_effect_receipts"
            ) == 0
            snapshot_id = await conn.fetchval(
                "SELECT snapshot_id FROM trip_map_render_snapshots LIMIT 1"
            )
            with pytest.raises(asyncpg.RaiseError, match="immutable"):
                await conn.execute(
                    "UPDATE trip_map_render_snapshots SET stop_count = stop_count WHERE snapshot_id = $1",
                    snapshot_id,
                )
            assert await conn.fetchval("SELECT to_regclass('public.rooms') IS NOT NULL")

        full_text = """北京三日行程
Day 1：故宫博物院、景山公园。
Day 2：天坛公园、前门大街。
Day 3：颐和园、圆明园。
有空可以考虑南锣鼓巷，不去上海迪士尼乐园。
"""
        owner_user_id = str(uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, nickname) VALUES ($1, 'FULL owner')",
                owner_user_id,
            )
        full_request_hash = canonical_sha256(
            {"mode": "FULL", "source": {"type": "TEXT", "text": full_text}}
        )
        full_created = await repository.create_full(
            owner_user_id=owner_user_id,
            source_text=full_text,
            idempotency_key="postgres-full",
            request_hash=full_request_hash,
            now=now,
            retention_days=30,
        )
        full_replay = await repository.create_full(
            owner_user_id=owner_user_id,
            source_text=full_text,
            idempotency_key="postgres-full",
            request_hash=full_request_hash,
            now=now,
            retention_days=30,
        )
        assert full_replay.replayed is True
        full_job = await repository.claim_next(worker_id="postgres-full-worker", now=now, lease_seconds=30)
        assert full_job is not None
        source = await repository.load_source(full_job, now=now)
        assert source.source_type == "TEXT"
        assert source.text == full_text
        full_output = await build_full_text_pipeline().run(source.text)
        await repository.complete_job(full_job, full_output, now=now)
        full_resource = await repository.authorize(
            full_created.accepted.public_resource_id,
            capability_hash=None,
            user_id=owner_user_id,
            now=now,
        )
        full_result = await repository.get_result(full_resource)
        assert full_result is not None
        assert full_result.result.status == "READY"
        assert [len(day.activities) for day in full_result.result.days] == [2, 2, 2]
        assert full_result.result.map.status == "PREPARING"
        assert await MapRenderWorker(repository).run_once(
            "postgres-full-map-worker",
            now=now,
        )
        assert (await repository.get_map_view(full_resource, now=now)).status == "AVAILABLE"
        with pytest.raises(ResourceAccessDeniedError):
            await repository.authorize(
                full_created.accepted.public_resource_id,
                capability_hash=None,
                user_id=str(uuid4()),
                now=now,
            )
        async with pool.acquire() as conn:
            encrypted = await conn.fetchval(
                """
                SELECT encrypted_content FROM trip_understanding_sources
                WHERE understanding_id = $1
                """,
                full_resource.understanding_id,
            )

        first_token = full_result.result.days[0].activities[0].activity_token
        move = ActivityMoveCommand(
            command_type="ACTIVITY_MOVE",
            activity_token=first_token,
            target_day_index=3,
            target_position=1,
        )
        command_request_hash = canonical_sha256(
            {
                "command": move.model_dump(mode="json"),
                "if_match": full_result.opaque_etag,
            }
        )
        command_outcome = await repository.apply_command(
            full_resource,
            move,
            expected_etag=full_result.opaque_etag,
            idempotency_key="postgres-move",
            request_hash=command_request_hash,
            now=now,
        )
        assert command_outcome.applied.changed_days == ["Day 1", "Day 3"]
        replayed_command = await repository.apply_command(
            full_resource,
            move,
            expected_etag=full_result.opaque_etag,
            idempotency_key="postgres-move",
            request_hash=command_request_hash,
            now=now,
        )
        assert replayed_command.replayed is True
        assert replayed_command.opaque_etag == command_outcome.opaque_etag
        with pytest.raises(RevisionConflictError):
            await repository.apply_command(
                full_resource,
                move,
                expected_etag=full_result.opaque_etag,
                idempotency_key="postgres-stale-move",
                request_hash=command_request_hash,
                now=now,
            )
        updated_resource = await repository.authorize(
            full_created.accepted.public_resource_id,
            capability_hash=None,
            user_id=owner_user_id,
            now=now,
        )
        updated_result = await repository.get_result(updated_resource)
        assert updated_result is not None
        assert updated_result.opaque_etag == command_outcome.opaque_etag
        assert updated_result.result.map.status == "NEEDS_UPDATE"
        assert [item.name for item in updated_result.result.days[2].activities] == [
            "颐和园",
            "故宫博物院",
            "圆明园",
        ]
        map_request_hash = canonical_sha256(
            {"action": "RENDER_MAP", "if_match": command_outcome.opaque_etag}
        )
        requested_map = await repository.request_map_render(
            updated_resource,
            expected_etag=command_outcome.opaque_etag,
            idempotency_key="postgres-manual-map",
            request_hash=map_request_hash,
            now=now,
        )
        replayed_map = await repository.request_map_render(
            updated_resource,
            expected_etag=command_outcome.opaque_etag,
            idempotency_key="postgres-manual-map",
            request_hash=map_request_hash,
            now=now,
        )
        await repository.request_map_render(
            updated_resource,
            expected_etag=command_outcome.opaque_etag,
            idempotency_key="postgres-manual-map-second-key",
            request_hash=map_request_hash,
            now=now,
        )
        assert requested_map.accepted.status == "PREPARING"
        assert replayed_map.replayed is True
        old_map_claim = await repository.claim_next_map(
            worker_id="postgres-reused-map-worker",
            now=now + timedelta(seconds=1),
            lease_seconds=5,
        )
        assert old_map_claim is not None
        old_map_output = await MapRenderer().render(
            await repository.load_map_plan(old_map_claim),
            observed_at=now + timedelta(seconds=1),
        )
        replacement_map_claim = await repository.claim_next_map(
            worker_id="postgres-reused-map-worker",
            now=now + timedelta(seconds=7),
            lease_seconds=30,
        )
        assert replacement_map_claim is not None
        assert replacement_map_claim.attempt == old_map_claim.attempt + 1
        await repository.fail_map_job(
            old_map_claim,
            category="STALE_MAP_FAILURE",
            now=now + timedelta(seconds=8),
        )
        cached_geometry_before_stale_completion = dict(geometry_cache._items)
        with pytest.raises(JobLeaseLostError):
            await repository.complete_map_job(
                old_map_claim,
                old_map_output,
                now=now + timedelta(seconds=8),
            )
        assert geometry_cache._items == cached_geometry_before_stale_completion
        replacement_map_output = await MapRenderer().render(
            await repository.load_map_plan(replacement_map_claim),
            observed_at=now + timedelta(seconds=8),
        )
        await repository.complete_map_job(
            replacement_map_claim,
            replacement_map_output,
            now=now + timedelta(seconds=8),
        )
        assert (
            await repository.get_map_view(
                updated_resource,
                now=now + timedelta(seconds=9),
            )
        ).status == "LIMITED"
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_understanding_revisions WHERE understanding_id = $1",
                full_resource.understanding_id,
            ) == 3
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_understanding_jobs WHERE understanding_id = $1",
                full_resource.understanding_id,
            ) == 1
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_understanding_side_effect_receipts WHERE job_id = $1",
                full_job.job_id,
            ) == 1
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_map_render_jobs WHERE understanding_id = $1",
                full_resource.understanding_id,
            ) == 2
            assert encrypted is not None
            assert full_text.encode("utf-8") not in bytes(encrypted)
            assert await conn.fetchval(
                """
                SELECT bool_and(quote LIKE 'enc:v1:%')
                FROM trip_understanding_source_claims WHERE understanding_id = $1
                """,
                full_resource.understanding_id,
            )
            persisted_proposal = await conn.fetchval(
                """
                SELECT proposal_json::text FROM trip_understanding_revisions
                WHERE understanding_id = $1 AND revision = 2
                """,
                full_resource.understanding_id,
            )
            assert "预约说明" not in persisted_proposal
            assert '"raw_text"' not in persisted_proposal
            assert "ENCRYPTED_IN_SOURCE_CLAIMS" in persisted_proposal
            assert await conn.fetchval(
                """
                SELECT retention_until - created_at <= INTERVAL '30 days 1 second'
                FROM trip_understanding_sources WHERE understanding_id = $1
                """,
                full_resource.understanding_id,
            )

        claim_owner_id = str(uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, nickname) VALUES ($1, 'Claim owner')",
                claim_owner_id,
            )
        claim_request_hash = canonical_sha256(
            {
                "public_resource_id": created.accepted.public_resource_id,
                "user_id": claim_owner_id,
                "action": "CLAIM",
            }
        )
        claim = await repository.claim_demo(
            created.accepted.public_resource_id,
            capability_hash="a" * 64,
            user_id=claim_owner_id,
            idempotency_key="postgres-claim",
            request_hash=claim_request_hash,
            now=now,
            retention_days=30,
        )
        claimed_id = claim.claimed.public_resource_id
        assert claimed_id != created.accepted.public_resource_id
        assert claim.opaque_etag == stored.opaque_etag
        with pytest.raises(ResourceGoneError):
            await repository.authorize(
                created.accepted.public_resource_id,
                capability_hash="a" * 64,
                user_id=claim_owner_id,
                now=now,
            )
        claimed_resource = await repository.authorize(
            claimed_id,
            capability_hash=None,
            user_id=claim_owner_id,
            now=now,
        )
        claim_replay = await repository.claim_demo(
            created.accepted.public_resource_id,
            capability_hash="",
            user_id=claim_owner_id,
            idempotency_key="postgres-claim",
            request_hash=claim_request_hash,
            now=now,
            retention_days=30,
        )
        assert claim_replay.replayed is True
        assert claim_replay.claimed.public_resource_id == claimed_id

        source_delete_hash = canonical_sha256(
            {
                "understanding_id": full_resource.understanding_id,
                "action": "DELETE_SOURCE",
            }
        )
        private_planned_text = "仅供内部参考的联系电话 010-00000000"
        async with pool.acquire() as conn:
            source_revision = await conn.fetchval(
                """
                SELECT MAX(revision) FROM trip_understanding_revisions
                WHERE understanding_id = $1
                """,
                full_resource.understanding_id,
            )
            await conn.execute(
                """
                INSERT INTO trip_understanding_activities (
                    activity_id, understanding_id, revision, public_activity_token,
                    day_index, sequence_index, role, mention_text, atomic_place_name,
                    category_hint, time_hint, eligible_for_place_search,
                    resolution_status, canonical_place_id, resolver_receipt_json, created_at
                ) VALUES (
                    $1, $2, $3, $4, 1, 999, 'PLANNED', $5, NULL,
                    NULL, NULL, FALSE, 'NOT_ELIGIBLE', NULL, '{}'::jsonb, $6
                )
                """,
                str(uuid4()),
                full_resource.understanding_id,
                source_revision,
                "private-planned-token-1234567890",
                private_planned_text,
                now,
            )
            assert await conn.fetchval(
                """
                SELECT COUNT(*) FROM trip_understanding_activities
                WHERE understanding_id = $1 AND mention_text = $2
                """,
                full_resource.understanding_id,
                private_planned_text,
            ) == 1
        source_delete = await repository.delete_source(
            full_resource,
            user_id=owner_user_id,
            idempotency_key="postgres-delete-source",
            request_hash=source_delete_hash,
            now=now,
        )
        assert source_delete.replayed is False
        source_delete_replay = await repository.delete_source(
            full_resource,
            user_id=owner_user_id,
            idempotency_key="postgres-delete-source",
            request_hash=source_delete_hash,
            now=now,
        )
        assert source_delete_replay.replayed is True
        with pytest.raises(SourceUnavailableError):
            await repository.load_source(full_job, now=now)
        retained_result = await repository.get_result(updated_resource)
        assert retained_result is not None
        assert retained_result.result.days == updated_result.result.days
        assert retained_result.result.assumptions == updated_result.result.assumptions
        assert retained_result.result.map.status == "LIMITED"
        async with pool.acquire() as conn:
            source_state = await conn.fetchrow(
                """
                SELECT encrypted_content, encryption_key_ref, deleted_at,
                       deletion_receipt_hash
                FROM trip_understanding_sources
                WHERE understanding_id = $1
                """,
                full_resource.understanding_id,
            )
            assert source_state["encrypted_content"] is None
            assert source_state["encryption_key_ref"] is None
            assert source_state["deleted_at"] is not None
            assert re.fullmatch(r"[0-9a-f]{64}", source_state["deletion_receipt_hash"])
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_understanding_source_claims WHERE understanding_id = $1",
                full_resource.understanding_id,
            ) == 0
            assert await conn.fetchval(
                """
                SELECT COUNT(*) FROM trip_understanding_activities
                WHERE understanding_id = $1
                """,
                full_resource.understanding_id,
            ) == 0
            assert await conn.fetchval(
                """
                SELECT COUNT(*) FROM trip_understanding_deletion_jobs
                WHERE understanding_id = $1 AND scope = 'SOURCE' AND status = 'COMPLETED'
                """,
                full_resource.understanding_id,
            ) == 1

        trip_delete_hash = canonical_sha256(
            {"public_resource_id": claimed_id, "action": "DELETE_TRIP"}
        )
        await repository.delete_trip(
            claimed_resource,
            capability_hash=None,
            user_id=claim_owner_id,
            idempotency_key="postgres-delete-trip",
            request_hash=trip_delete_hash,
            now=now,
        )
        with pytest.raises(ResourceGoneError):
            await repository.authorize(
                claimed_id,
                capability_hash=None,
                user_id=claim_owner_id,
                now=now,
            )
        assert await repository.replay_trip_deletion(
            claimed_id,
            capability_hash=None,
            user_id=claim_owner_id,
            idempotency_key="postgres-delete-trip",
            request_hash=trip_delete_hash,
        )
        assert not await repository.replay_trip_deletion(
            claimed_id,
            capability_hash=None,
            user_id=owner_user_id,
            idempotency_key="postgres-delete-trip",
            request_hash=trip_delete_hash,
        )
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_understandings WHERE understanding_id = $1",
                claimed_resource.understanding_id,
            ) == 0
            claimed_tombstones = await conn.fetch(
                """
                SELECT public_resource_id, reason, replacement_public_resource_id
                FROM trip_understanding_resource_tombstones
                WHERE public_resource_id = ANY($1::text[])
                ORDER BY public_resource_id
                """,
                [created.accepted.public_resource_id, claimed_id],
            )
            assert len(claimed_tombstones) == 2
            assert all(row["reason"] == "DELETED" for row in claimed_tombstones)
            assert all(row["replacement_public_resource_id"] is None for row in claimed_tombstones)
            assert await conn.fetchval(
                """
                SELECT COUNT(*) FROM trip_understanding_anonymous_sessions
                WHERE claimed_by = $1
                """,
                claim_owner_id,
            ) == 0
            assert await conn.fetchval(
                """
                SELECT COUNT(*) FROM trip_understanding_idempotency_records
                WHERE response_json ->> 'public_resource_id' = ANY($1::text[])
                """,
                [created.accepted.public_resource_id, claimed_id],
            ) == 0

        extra_full_text = "Day 1 去颐和园。"
        extra_created = await repository.create_full(
            owner_user_id=owner_user_id,
            source_text=extra_full_text,
            idempotency_key="postgres-account-delete-extra",
            request_hash=canonical_sha256(
                {"mode": "FULL", "source": {"type": "TEXT", "text": extra_full_text}}
            ),
            now=now,
            retention_days=30,
        )
        account_delete_hash = canonical_sha256({"action": "DELETE_ALL_TRAVEL_DATA"})
        account_deleted = await repository.delete_account_travel_data(
            user_id=owner_user_id,
            idempotency_key="postgres-account-delete",
            request_hash=account_delete_hash,
            now=now,
        )
        assert account_deleted.view.status == "COMPLETED"
        assert account_deleted.view.next_action == "NONE"
        account_replay = await repository.delete_account_travel_data(
            user_id=owner_user_id,
            idempotency_key="postgres-account-delete",
            request_hash=account_delete_hash,
            now=now,
        )
        assert account_replay.replayed is True
        assert (await repository.get_account_travel_data_deletion(user_id=owner_user_id)).status == (
            "COMPLETED"
        )
        for deleted_public_id in (
            full_created.accepted.public_resource_id,
            extra_created.accepted.public_resource_id,
        ):
            with pytest.raises(ResourceGoneError):
                await repository.authorize(
                    deleted_public_id,
                    capability_hash=None,
                    user_id=owner_user_id,
                    now=now,
                )
        async with pool.acquire() as conn:
            for table in (
                "trip_understandings",
                "trip_understanding_sources",
                "trip_understanding_revisions",
                "trip_understanding_activities",
                "trip_understanding_source_claims",
                "trip_understanding_jobs",
                "trip_understanding_events",
                "trip_understanding_results",
                "trip_understanding_side_effect_receipts",
                "trip_understanding_claim_commands",
                "trip_plan_revision_refs",
                "trip_map_render_jobs",
                "trip_map_render_events",
                "trip_map_render_snapshots",
                "trip_map_route_edges",
                "trip_map_route_mode_facts",
                "trip_map_provider_effect_receipts",
            ):
                assert await conn.fetchval(f"SELECT COUNT(*) FROM {table}") == 0
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_understanding_deletion_jobs WHERE owner_user_id = $1",
                owner_user_id,
            ) == 0
            retained_idempotency = await conn.fetchval(
                "SELECT COALESCE(string_agg(scope || COALESCE(response_json::text, ''), E'\\n'), '') FROM trip_understanding_idempotency_records"
            )
            assert owner_user_id not in retained_idempotency
            assert full_text not in retained_idempotency
            assert extra_full_text not in retained_idempotency
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_understanding_anonymous_sessions WHERE claimed_by IS NOT NULL"
            ) == 0
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
