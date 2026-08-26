from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.audit.models import AuditStatus, EvidenceFreshness
from app.audit.recheck import PreTripRecheckService, StoredEvidenceRefresher
from app.audit.repositories import PostgresAuditRepository
from app.audit.service import AuditApplicationService
from app.constraints.geo_routes import RouteResult
from app.importing.entity_resolver import EntityResolver
from app.importing.models import ImportSourceType, ImportStatus
from app.importing.repositories import PostgresImportRepository
from app.importing.service import ImportApplicationService
from app.itineraries.adapters import revision_to_legacy
from app.itineraries.command_service import RevisionCommandService
from app.itineraries.errors import RevisionConflictError
from app.itineraries.hash_service import sha256_canonical, with_content_hash
from app.itineraries.models import (
    EditOperation,
    ItineraryDay,
    ItineraryEditCommand,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.map_projection import build_map_projection
from app.itineraries.repositories import PostgresItineraryRepository
from app.itineraries.revision_service import RevisionService
from app.operations.repositories import PostgresCreationCommandRepository
from app.itineraries.tips_models import FinalTipsArtifact
from app.itineraries.tips_repositories import PostgresFinalTipsRepository
from app.repairs.repositories import PostgresRepairRepository
from app.repairs.search import BoundedRepairSearch, ProviderRepairRouteEvidenceRefresher


pytestmark = pytest.mark.integration


class ExactPlaceProvider:
    async def search(self, *, query: str, city: str):
        place_ids = {"故宫": "poi-gugong", "景山公园": "poi-jingshan", "颐和园": "poi-summer"}
        place_id = place_ids.get(query)
        if place_id is None:
            return []
        return [
            {
                "place_id": place_id,
                "name": query,
                "city": city,
                "district": "东城区" if query != "颐和园" else "海淀区",
                "address": "集成测试地址",
                "category": "attraction",
                "coords": {"lng": 116.397, "lat": 39.918},
                "retrieval_provider": "controlled_test",
                "retrieval_request_hash": "5" * 64,
                "execution_mode": "fixture",
                "retrieval_response_hash": "e" * 64,
                "retrieval_observed_at": "2026-08-21T00:00:00+00:00",
                "opening_hours": "08:00-20:00",
            }
        ]


class ControlledRepairRouteProvider:
    async def fetch(self, *, origin, destination, mode, city):
        return RouteResult(
            status="ok",
            duration_minutes=15,
            distance_km=2.5,
            transfer_count=None,
            source="controlled_route_fixture",
            response_hash="a" * 64,
            observed_at=None,
        )


def _admin_dsn() -> str:
    return os.getenv("TEST_DATABASE_ADMIN_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")


@pytest.mark.asyncio
async def test_postgres_import_audit_repair_apply_and_restart_readback():
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name = f"breezetravel_flow_{uuid4().hex[:12]}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    admin = await asyncpg.connect(_admin_dsn())
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        base_dsn = _admin_dsn().rsplit("/", 1)[0]
        database_dsn = f"{base_dsn}/{database_name}"
        bootstrap = await asyncpg.connect(database_dsn)
        try:
            await bootstrap.execute(Path("app/db/init.sql").read_text(encoding="utf-8"))
        finally:
            await bootstrap.close()

        migration_env = os.environ.copy()
        migration_env["DATABASE_URL"] = database_dsn.replace("postgresql://", "postgresql+asyncpg://")
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

        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=3)
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO users(user_id, nickname) VALUES ('flow-user', '集成测试')")
            await conn.execute(
                """
                INSERT INTO rooms(room_id, thread_id, trip_city, trip_days)
                VALUES ('flow-room', 'flow-thread', '北京', 2)
                """
            )
            await conn.execute("INSERT INTO room_members(room_id, user_id) VALUES ('flow-room', 'flow-user')")

        itinerary_repository = PostgresItineraryRepository(pool)
        workspace = await RevisionService(itinerary_repository).create_workspace(
            room_id="flow-room",
            city="北京",
            date_range=TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
            created_by="flow-user",
        )
        import_repository = PostgresImportRepository(pool)
        import_service = ImportApplicationService(
            import_repository=import_repository,
            itinerary_repository=itinerary_repository,
            entity_resolver=EntityResolver(ExactPlaceProvider()),
        )
        itinerary_import = await import_service.create_import(
            workspace_id=workspace.workspace_id,
            source_type=ImportSourceType.AI_TEXT,
            raw_text=("第1天：09:00-12:00 故宫入口待确认 → 11:00-13:00 景山公园\n第2天：10:00-12:00 颐和园"),
            actor_user_id="flow-user",
        )
        assert itinerary_import.status == ImportStatus.NEEDS_RESOLUTION
        missing = next(item for item in itinerary_import.resolutions if item.resolution_status.value == "NOT_FOUND")
        untouched_versions = {
            item.raw_stop_id: item.resolution_version
            for item in itinerary_import.resolutions
            if item.raw_stop_id != missing.raw_stop_id
        }
        itinerary_import = await import_service.retry_resolution(
            import_id=itinerary_import.import_id,
            raw_stop_id=missing.raw_stop_id,
            query="故宫",
        )
        assert itinerary_import.status == ImportStatus.READY
        retried = next(item for item in itinerary_import.resolutions if item.raw_stop_id == missing.raw_stop_id)
        assert retried.resolution_version == 2
        assert retried.canonical_place_id == "poi-gugong"
        assert {
            item.raw_stop_id: item.resolution_version
            for item in itinerary_import.resolutions
            if item.raw_stop_id != missing.raw_stop_id
        } == untouched_versions
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE OR REPLACE FUNCTION fail_import_materialization() RETURNS trigger AS $$
                BEGIN
                    IF NEW.place_id = 'poi-jingshan' THEN
                        RAISE EXCEPTION 'controlled room_places failure';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                CREATE TRIGGER fail_import_materialization_trigger
                BEFORE INSERT ON room_places
                FOR EACH ROW EXECUTE FUNCTION fail_import_materialization();
                """
            )
        with pytest.raises(asyncpg.PostgresError, match="controlled room_places failure"):
            await import_service.apply_import(itinerary_import.import_id, actor_user_id="flow-user")
        async with pool.acquire() as conn:
            rolled_back = await conn.fetchrow(
                """
                SELECT
                    (SELECT current_itinerary_revision FROM trip_workspaces WHERE workspace_id = 'flow-workspace') AS current_revision,
                    (SELECT status FROM itinerary_imports WHERE import_id = $1) AS import_status,
                    (SELECT COUNT(*) FROM itinerary_revisions WHERE workspace_id = 'flow-workspace') AS revisions,
                    (SELECT COUNT(*) FROM room_places WHERE room_id = 'flow-room') AS room_places,
                    (SELECT COUNT(*) FROM itinerary_place_receipts WHERE workspace_id = 'flow-workspace') AS receipts,
                    (SELECT COUNT(*) FROM itinerary_import_commands WHERE import_id = $1) AS commands
                """,
                itinerary_import.import_id,
            )
            assert dict(rolled_back) == {
                "current_revision": None,
                "import_status": "READY",
                "revisions": 0,
                "room_places": 0,
                "receipts": 0,
                "commands": 0,
            }
            await conn.execute("DROP TRIGGER fail_import_materialization_trigger ON room_places")
            await conn.execute("DROP FUNCTION fail_import_materialization()")

        applied_import = await import_service.apply_import(itinerary_import.import_id, actor_user_id="flow-user")
        assert applied_import.revision.revision == 1
        assert len(applied_import.resolved_place_receipts) == 3
        persisted_revision = await itinerary_repository.get_revision(workspace.workspace_id, 1)
        assert persisted_revision is not None
        map_projection = build_map_projection(persisted_revision, lineage=[persisted_revision])
        assert map_projection.status == "AVAILABLE"
        assert len(map_projection.stops) == 3
        assert all(stop.receipt_hash for stop in map_projection.stops)
        async with pool.acquire() as conn:
            materialized = await conn.fetch(
                "SELECT place_id, place_data FROM room_places WHERE room_id = 'flow-room' ORDER BY place_id"
            )
        assert [row["place_id"] for row in materialized] == ["poi-gugong", "poi-jingshan", "poi-summer"]
        materialized_place_data = [
            json.loads(row["place_data"])
            if isinstance(row["place_data"], str)
            else row["place_data"]
            for row in materialized
        ]
        assert all(place_data["coords"] for place_data in materialized_place_data)
        assert all(place_data["resolved_place_receipt"] for place_data in materialized_place_data)

        audit_repository = PostgresAuditRepository(pool)
        source_report = await AuditApplicationService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
        ).run_current_audit(
            workspace.workspace_id,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        source_snapshot = await audit_repository.get_snapshot(source_report.evidence_snapshot_id)
        assert source_snapshot is not None
        identity_facts = [fact for fact in source_snapshot.facts if fact.fact_type == "POI_IDENTITY"]
        assert len(identity_facts) == 3
        assert all(fact.freshness_status == EvidenceFreshness.FRESH for fact in identity_facts)
        assert all(fact.provider == "controlled_test" for fact in identity_facts)
        assert all(fact.value["coords"] for fact in identity_facts)
        time_finding = next(item for item in source_report.findings if item.reason_code == "TIME_CHAIN_BROKEN")
        assert time_finding.input_values["day_stops"][1]["start_time"] == "11:00"

        repair_repository = PostgresRepairRepository(pool)
        options = await BoundedRepairSearch(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
            repair_repository=repair_repository,
            route_refresher=ProviderRepairRouteEvidenceRefresher(
                ControlledRepairRouteProvider()
            ),
        ).propose(source_report.report_id, now=datetime(2026, 8, 20, tzinfo=timezone.utc))
        assert len(options) == 2
        assert all(option.route_cost_delta is None for option in options)
        numeric_route_option = options[0].model_copy(
            update={
                "repair_id": "route-delta-roundtrip",
                "route_cost_delta": -12.5,
            }
        )
        await repair_repository.save_option(numeric_route_option)
        numeric_route_readback = await repair_repository.get_option("route-delta-roundtrip")
        assert numeric_route_readback.route_cost_delta == -12.5
        selected = options[0]
        result = await repair_repository.apply_option(
            selected.repair_id,
            actor_user_id="flow-user",
            if_match_revision=1,
            idempotency_key="flow-repair-apply",
        )
        replay = await repair_repository.apply_option(
            selected.repair_id,
            actor_user_id="flow-user",
            if_match_revision=1,
            idempotency_key="flow-repair-apply",
        )
        assert result.new_revision == 2
        assert replay.idempotent_replay is True

        # Fresh repository instances simulate a process restart/readback boundary.
        restarted_itineraries = PostgresItineraryRepository(pool)
        restarted_audits = PostgresAuditRepository(pool)
        restarted_repairs = PostgresRepairRepository(pool)
        stored_workspace = await restarted_itineraries.get_workspace(workspace.workspace_id)
        stored_revision_1 = await restarted_itineraries.get_revision(workspace.workspace_id, 1)
        stored_revision_2 = await restarted_itineraries.get_revision(workspace.workspace_id, 2)
        stored_postcheck = await restarted_audits.get_report(result.postcheck_report_id)
        stored_option = await restarted_repairs.get_option(selected.repair_id)
        stale_sibling = await restarted_repairs.get_option(options[1].repair_id)

        assert stored_workspace.current_itinerary_revision == 2
        assert stored_workspace.current_report_id == result.postcheck_report_id
        assert stored_revision_1.content_hash == applied_import.revision.content_hash
        assert stored_revision_2.parent_revision == 1
        assert stored_option.route_cost_delta is None
        assert stored_postcheck.itinerary_revision == 2
        assert not any(
            item.reason_code == "TIME_CHAIN_BROKEN" and item.status == AuditStatus.VIOLATED
            for item in stored_postcheck.findings
        )
        source_unknown = {
            (item.rule_id, item.reason_code, tuple(item.affected_days))
            for item in source_report.findings
            if item.status == AuditStatus.UNKNOWN
        }
        postcheck_unknown = {
            (item.rule_id, item.reason_code, tuple(item.affected_days))
            for item in stored_postcheck.findings
            if item.status == AuditStatus.UNKNOWN
        }
        assert postcheck_unknown <= source_unknown
        assert any(item.reason_code == "WEATHER_DATA_MISSING" for item in stored_postcheck.findings)
        assert stored_option.status.value == "APPLIED"
        assert stale_sibling.status.value == "STALE"

        # Revision 2 inherits the nearest immutable import receipt.  A later
        # collaborative room_places overwrite must not replace audit identity.
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE room_places
                SET place_data = '{"place_id":"poi-gugong","name":"tampered","city":"杭州"}'::jsonb
                WHERE room_id = 'flow-room' AND place_id = 'poi-gugong'
                """
            )
        inherited_records = await restarted_audits.load_place_records(
            workspace.workspace_id,
            ["poi-gugong"],
        )
        assert inherited_records["poi-gugong"]["name"] == "故宫"
        assert inherited_records["poi-gugong"]["city"] == "北京"
        assert inherited_records["poi-gugong"]["immutable_receipt"] is True
        assert inherited_records["poi-gugong"]["resolved_place_receipt"]["canonical_place_id"] == "poi-gugong"

        # Repository-level revision isolation: simulate a later legitimate
        # provider refresh for the same canonical place.  Auditing revision 1
        # must keep its import receipt even while current revision 2 has a
        # newer immutable receipt and room_places remains tampered.
        revision_two_stop = next(
            stop
            for day in stored_revision_2.days
            for stop in day.stops
            if stop.place_id == "poi-gugong"
        )
        revision_two_receipt = dict(inherited_records["poi-gugong"]["resolved_place_receipt"])
        revision_two_receipt.update({
            "name": "故宫博物院（rev2 provider refresh）",
            "response_hash": "a" * 64,
            "observed_at": "2026-08-22T00:00:00+00:00",
        })
        revision_two_place = dict(inherited_records["poi-gugong"])
        revision_two_place.update({
            "name": "故宫博物院（rev2 provider refresh）",
            "resolved_place_receipt": revision_two_receipt,
            "retrieval_observed_at": "2026-08-22T00:00:00+00:00",
        })
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO itinerary_place_receipts (
                    workspace_id, itinerary_revision, stop_id, place_id,
                    receipt_hash, receipt_json, place_data_json
                ) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb)
                """,
                workspace.workspace_id,
                2,
                revision_two_stop.stop_id,
                "poi-gugong",
                sha256_canonical(revision_two_receipt),
                json.dumps(revision_two_receipt, ensure_ascii=False),
                json.dumps(revision_two_place, ensure_ascii=False, default=str),
            )
        revision_one_records = await restarted_audits.load_place_records(
            workspace.workspace_id,
            ["poi-gugong"],
            target_itinerary_revision=1,
        )
        revision_two_records = await restarted_audits.load_place_records(
            workspace.workspace_id,
            ["poi-gugong"],
            target_itinerary_revision=2,
        )
        assert revision_one_records["poi-gugong"]["name"] == "故宫"
        assert revision_one_records["poi-gugong"]["receipt_itinerary_revision"] == 1
        assert revision_two_records["poi-gugong"]["name"] == "故宫博物院（rev2 provider refresh）"
        assert revision_two_records["poi-gugong"]["receipt_itinerary_revision"] == 2

        # The Tips artifact is a separate immutable projection, not revision
        # content.  Eligibility is unit-tested in FinalTipsService; this proves
        # migration/FK/JSONB/restart readback against real PostgreSQL.
        tipped_itinerary = revision_to_legacy(
            stored_revision_2,
            thread_id="flow-room",
        )
        tipped_itinerary.days[0].slots[0].tips = ["PostgreSQL 回读提示"]
        tips_artifact = FinalTipsArtifact(
            report_id=stored_postcheck.report_id,
            workspace_id=stored_revision_2.workspace_id,
            itinerary_revision=stored_revision_2.revision,
            basis_content_hash=sha256_canonical(
                {
                    "revision": stored_revision_2.content_hash,
                    "report": stored_postcheck.report_input_hash,
                }
            ),
            generation_input_hash=sha256_canonical(
                {
                    "report_id": stored_postcheck.report_id,
                    "preferences": "",
                }
            ),
            artifact_hash=sha256_canonical(
                {
                    "report": stored_postcheck.report_id,
                    "tips": tipped_itinerary.model_dump(mode="json"),
                }
            ),
            itinerary=tipped_itinerary,
        )
        await PostgresFinalTipsRepository(pool).save(tips_artifact)
        restarted_tips = await PostgresFinalTipsRepository(pool).get_by_report(stored_postcheck.report_id)
        assert restarted_tips.artifact_hash == tips_artifact.artifact_hash
        assert restarted_tips.itinerary.days[0].slots[0].tips == ["PostgreSQL 回读提示"]

        # Two browser-like commands racing on revision 2 must serialize: one
        # creates revision 3, the other receives an explicit stale conflict.
        target_stop_id = stored_revision_2.days[0].stops[0].stop_id
        commands = [
            (
                ItineraryEditCommand(
                    command_id="flow-concurrent-lock",
                    workspace_id=workspace.workspace_id,
                    base_revision=2,
                    actor_user_id="flow-user",
                    operation=EditOperation.LOCK_STOP,
                    payload={"stop_id": target_stop_id},
                ),
                "flow-concurrent-key-a",
            ),
            (
                ItineraryEditCommand(
                    command_id="flow-concurrent-time",
                    workspace_id=workspace.workspace_id,
                    base_revision=2,
                    actor_user_id="flow-user",
                    operation=EditOperation.ADJUST_TIME,
                    payload={"stop_id": target_stop_id, "start_time": "09:15", "end_time": "12:15"},
                ),
                "flow-concurrent-key-b",
            ),
        ]
        command_service = RevisionCommandService(PostgresItineraryRepository(pool))

        async def apply(command, key):
            return await command_service.apply(
                command,
                if_match_revision=2,
                idempotency_key=key,
            )

        outcomes = await asyncio.gather(
            *(apply(command, key) for command, key in commands),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if not isinstance(item, BaseException)]
        conflicts = [item for item in outcomes if isinstance(item, RevisionConflictError)]
        assert len(successes) == 1
        assert len(conflicts) == 1
        winner_index = next(index for index, item in enumerate(outcomes) if not isinstance(item, BaseException))
        replay = await apply(*commands[winner_index])
        assert replay.idempotent_replay is True
        assert replay.new_revision == 3
        final_workspace = await PostgresItineraryRepository(pool).get_workspace(workspace.workspace_id)
        assert final_workspace.current_itinerary_revision == 3
        assert final_workspace.current_report_id is None
        assert [
            item.revision for item in await PostgresItineraryRepository(pool).list_revisions(workspace.workspace_id)
        ] == [1, 2, 3]

        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM itinerary_edit_commands WHERE workspace_id = $1",
                    workspace.workspace_id,
                )
                == 2
            )
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM itinerary_revisions WHERE workspace_id = $1",
                    workspace.workspace_id,
                )
                == 3
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


@pytest.mark.asyncio
async def test_postgres_pre_trip_recheck_appends_immutable_bundle_and_replays_after_restart():
    """Exercise P8's append/replay boundary against a controlled real database."""

    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name = f"breezetravel_recheck_{uuid4().hex[:12]}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    admin = await asyncpg.connect(_admin_dsn())
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        base_dsn = _admin_dsn().rsplit("/", 1)[0]
        database_dsn = f"{base_dsn}/{database_name}"
        bootstrap = await asyncpg.connect(database_dsn)
        try:
            await bootstrap.execute(Path("app/db/init.sql").read_text(encoding="utf-8"))
        finally:
            await bootstrap.close()
        migration_env = os.environ.copy()
        migration_env["DATABASE_URL"] = database_dsn.replace("postgresql://", "postgresql+asyncpg://")
        migrated = subprocess.run(
            [sys.executable, "-m", "scripts.migrate"], cwd=Path.cwd(), env=migration_env,
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert migrated.returncode == 0, migrated.stderr

        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=2)
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO users(user_id, nickname) VALUES ('recheck-user', '复检测试')")
            await conn.execute(
                "INSERT INTO rooms(room_id, thread_id, trip_city, trip_days) "
                "VALUES ('recheck-room', 'recheck-thread', '北京', 2)"
            )
            await conn.execute("INSERT INTO room_members(room_id, user_id) VALUES ('recheck-room', 'recheck-user')")

        itinerary_repository = PostgresItineraryRepository(pool)
        date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
        revision = with_content_hash(ItineraryRevisionContent(
            itinerary_id="recheck-itinerary",
            workspace_id="recheck-workspace",
            revision=1,
            source_type=RevisionSource.MANUAL,
            city="北京",
            date_range=date_range,
            days=[
                ItineraryDay(day_index=0, date=date_range.start, stops=[ItineraryStop(
                    stop_id="recheck-stop", place_id="recheck-place", day_index=0, order_index=0,
                    start_time="09:00", end_time="11:00", raw_name="复检地点",
                )]),
                ItineraryDay(day_index=1, date=date_range.end, stops=[]),
            ],
            created_by="recheck-user",
        ))
        await itinerary_repository.create_workspace(
            TripWorkspace(
                workspace_id="recheck-workspace", room_id="recheck-room", city="北京",
                trip_date_range=date_range, current_itinerary_revision=1, created_by="recheck-user",
            ),
            revision,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO room_places(room_id, place_id, place_data)
                VALUES ('recheck-room', 'recheck-place', $1::jsonb)
                """,
                json.dumps({
                    "place_id": "recheck-place", "name": "复检地点", "city": "北京",
                    "category": "attraction", "opening_hours": "08:00-18:00",
                    "provider": "stored_amap", "retrieval_observed_at": "2026-08-20T00:00:00+00:00",
                }, ensure_ascii=False),
            )

        audit_repository = PostgresAuditRepository(pool)
        source_report = await AuditApplicationService(
            itinerary_repository=itinerary_repository, audit_repository=audit_repository,
        ).run_current_audit("recheck-workspace", now=datetime(2026, 8, 20, tzinfo=timezone.utc))
        source_snapshot = await audit_repository.get_snapshot(source_report.evidence_snapshot_id)
        assert source_snapshot is not None
        source_opening = next(item for item in source_snapshot.facts if item.fact_type == "OPENING_HOURS")
        assert source_opening.value == "08:00-18:00"

        # Updating durable room data must create a new P8 snapshot/report, not
        # mutate the snapshot that the original audit cited.
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE room_places
                SET place_data = jsonb_set(place_data, '{opening_hours}', '"12:00-18:00"'::jsonb),
                    updated_at = '2026-08-30T00:00:00Z'
                WHERE room_id = 'recheck-room' AND place_id = 'recheck-place'
                """
            )
        command_repository = PostgresCreationCommandRepository(pool)
        first, replayed = await PreTripRecheckService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
            evidence_refresher=StoredEvidenceRefresher(),
        ).run_idempotent(
            source_report_id=source_report.report_id,
            actor_user_id="recheck-user",
            idempotency_key="postgres-pre-trip-recheck",
            command_repository=command_repository,
            now=datetime(2026, 8, 30, tzinfo=timezone.utc),
        )
        assert replayed is False
        assert first.evidence_snapshot.snapshot_id != source_snapshot.snapshot_id
        assert first.evidence_snapshot.supersedes_snapshot_id == source_snapshot.snapshot_id
        assert first.report.supersedes_report_id == source_report.report_id
        assert first.recheck_window_state.value == "RECOMMENDED_24_48H"
        assert any(
            item.change_type.value == "VALUE_CHANGED" and item.fact_type == "OPENING_HOURS"
            for item in first.evidence_changes
        )

        # New service/repositories simulate a restart.  It must replay the
        # persisted bundle rather than append another report or recompute a
        # different time-window state.
        restarted = PreTripRecheckService(
            itinerary_repository=PostgresItineraryRepository(pool),
            audit_repository=PostgresAuditRepository(pool),
            evidence_refresher=StoredEvidenceRefresher(),
        )
        replay, replayed = await restarted.run_idempotent(
            source_report_id=source_report.report_id,
            actor_user_id="recheck-user",
            idempotency_key="postgres-pre-trip-recheck",
            command_repository=PostgresCreationCommandRepository(pool),
            now=datetime(2026, 9, 2, tzinfo=timezone.utc),
        )
        assert replayed is True
        assert replay.report.report_id == first.report.report_id
        assert replay.evidence_snapshot.snapshot_id == first.evidence_snapshot.snapshot_id
        assert replay.recheck_window_state == first.recheck_window_state
        assert replay.hours_until_trip_start == first.hours_until_trip_start

        fresh_audits = PostgresAuditRepository(pool)
        persisted_source = await fresh_audits.get_snapshot(source_snapshot.snapshot_id)
        persisted_recheck = await fresh_audits.get_snapshot(first.evidence_snapshot.snapshot_id)
        assert persisted_source is not None and persisted_recheck is not None
        assert next(item for item in persisted_source.facts if item.fact_type == "OPENING_HOURS").value == "08:00-18:00"
        assert next(item for item in persisted_recheck.facts if item.fact_type == "OPENING_HOURS").value == "12:00-18:00"
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM evidence_snapshots WHERE workspace_id = 'recheck-workspace'") == 2
            assert await conn.fetchval("SELECT COUNT(*) FROM audit_reports WHERE workspace_id = 'recheck-workspace'") == 2
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM idempotent_creation_commands "
                "WHERE workspace_id = 'recheck-workspace' AND operation = 'PRE_TRIP_RECHECK'"
            ) == 1
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1", database_name)
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
