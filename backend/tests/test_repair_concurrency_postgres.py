from __future__ import annotations

import asyncio
import json
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
from app.constraints.geo_routes import RouteResult
from app.itineraries.hash_service import with_content_hash
from app.itineraries.errors import ItineraryDomainError
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import PostgresItineraryRepository
from app.repairs.errors import RepairStaleError
from app.repairs.models import RepairApplyResult, RepairOption, RepairStatus
from app.repairs.repositories import PostgresRepairRepository
from app.repairs.search import BoundedRepairSearch, ProviderRepairRouteEvidenceRefresher


pytestmark = pytest.mark.integration


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


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


@pytest.mark.asyncio
async def test_postgres_apply_and_reject_choose_exactly_one_terminal_state():
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name = f"breezetravel_repair_race_{uuid4().hex[:10]}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    admin = await asyncpg.connect(_admin_dsn())
    pool = None
    sibling_apply_pools = []
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
        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=4)

        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO users(user_id, nickname) VALUES ('race-user', 'race')")
            await conn.execute("INSERT INTO users(user_id, nickname) VALUES ('other-user', 'other')")
            await conn.execute(
                "INSERT INTO rooms(room_id, thread_id, trip_city, trip_days) "
                "VALUES ('race-room', 'race-thread', '北京', 2)"
            )
            await conn.execute(
                "INSERT INTO room_members(room_id, user_id) VALUES ('race-room', 'race-user')"
            )

        date_range = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
        revision = with_content_hash(
            ItineraryRevisionContent(
                itinerary_id="race-itinerary",
                workspace_id="race-workspace",
                revision=1,
                source_type=RevisionSource.IMPORT,
                city="北京",
                date_range=date_range,
                days=[
                    ItineraryDay(
                        day_index=0,
                        date=date_range.start,
                        stops=[
                            ItineraryStop(
                                stop_id="race-stop-1",
                                place_id="race-place-1",
                                day_index=0,
                                order_index=0,
                                start_time="09:00",
                                end_time="12:00",
                                visit_duration_minutes=180,
                            ),
                                ItineraryStop(
                                    stop_id="race-stop-2",
                                place_id="race-place-2",
                                day_index=0,
                                order_index=1,
                                start_time="11:00",
                                end_time="13:00",
                                    visit_duration_minutes=120,
                                ),
                                ItineraryStop(
                                    stop_id="race-hotel-stop",
                                    place_id="race-hotel",
                                    day_index=0,
                                    order_index=2,
                                    start_time="22:00",
                                    end_time="22:30",
                                    visit_duration_minutes=30,
                                ),
                            ],
                        ),
                        ItineraryDay(day_index=1, date=date_range.end, stops=[]),
                ],
                created_by="race-user",
                change_summary={
                    "map_stop_projections": {
                        "race-stop-1": {
                            "place_id": "race-place-1",
                            "canonical_name": "故宫",
                            "coords": {"lng": 116.397, "lat": 39.916},
                            "coordinate_role": "CANONICAL_POI",
                            "provenance": "controlled_route_fixture",
                        },
                        "race-stop-2": {
                            "place_id": "race-place-2",
                            "canonical_name": "景山公园",
                            "coords": {"lng": 116.396, "lat": 39.925},
                            "coordinate_role": "CANONICAL_POI",
                            "provenance": "controlled_route_fixture",
                        },
                        "race-hotel-stop": {
                            "place_id": "race-hotel",
                            "canonical_name": "集成测试酒店",
                            "coords": {"lng": 116.39, "lat": 39.92},
                            "coordinate_role": "CANONICAL_POI",
                            "provenance": "controlled_route_fixture",
                        },
                    }
                },
            )
        )
        workspace = TripWorkspace(
            workspace_id=revision.workspace_id,
            room_id="race-room",
            city="北京",
            trip_date_range=date_range,
            current_itinerary_revision=1,
            created_by="race-user",
        )
        itinerary_repository = PostgresItineraryRepository(pool)
        await itinerary_repository.create_workspace(workspace, revision)
        async with pool.acquire() as conn:
            for place_id, name, coords in (
                ("race-place-1", "故宫", {"lng": 116.397, "lat": 39.916}),
                ("race-place-2", "景山公园", {"lng": 116.396, "lat": 39.925}),
                ("race-hotel", "集成测试酒店", {"lng": 116.39, "lat": 39.92}),
            ):
                await conn.execute(
                    """
                    INSERT INTO room_places(room_id, place_id, place_data)
                    VALUES ('race-room', $1, $2::jsonb)
                    """,
                    place_id,
                    json.dumps(
                        {
                            "place_id": place_id,
                            "name": name,
                            "city": "北京",
                            "category": "hotel" if place_id == "race-hotel" else "attraction",
                            "opening_hours": "00:00-23:59"
                            if place_id == "race-hotel"
                            else "08:00-20:00",
                            "coords": coords,
                            "provider": "integration_fixture",
                            "retrieval_observed_at": datetime(
                                2026, 8, 20, tzinfo=timezone.utc
                            ).isoformat(),
                        },
                        ensure_ascii=False,
                    ),
                )

        audit_repository = PostgresAuditRepository(pool)
        source_report = await AuditApplicationService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
        ).run_current_audit(
            workspace.workspace_id,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        repair_repository = PostgresRepairRepository(pool)
        options = await BoundedRepairSearch(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
            repair_repository=repair_repository,
            route_refresher=ProviderRepairRouteEvidenceRefresher(
                ControlledRepairRouteProvider()
            ),
        ).propose(source_report.report_id, now=datetime(2026, 8, 20, tzinfo=timezone.utc))
        option = options[0]
        rejection_target = options[1]
        rejected = await repair_repository.reject_option(
            rejection_target.repair_id,
            actor_user_id="race-user",
            reason="  deterministic rejection  ",
        )
        replayed_rejection = await repair_repository.reject_option(
            rejection_target.repair_id,
            actor_user_id="race-user",
            reason="deterministic rejection",
        )
        assert replayed_rejection == rejected
        with pytest.raises(RepairStaleError):
            await repair_repository.reject_option(
                rejection_target.repair_id,
                actor_user_id="other-user",
                reason="deterministic rejection",
            )
        with pytest.raises(RepairStaleError):
            await repair_repository.reject_option(
                rejection_target.repair_id,
                actor_user_id="race-user",
                reason="different rejection",
            )

        outcomes = await asyncio.gather(
            repair_repository.apply_option(
                option.repair_id,
                actor_user_id="race-user",
                if_match_revision=1,
                idempotency_key="race-apply",
            ),
            repair_repository.reject_option(
                option.repair_id,
                actor_user_id="race-user",
                reason="race reject",
            ),
            return_exceptions=True,
        )

        successes = [item for item in outcomes if isinstance(item, (RepairApplyResult, RepairOption))]
        stale = [item for item in outcomes if isinstance(item, RepairStaleError)]
        assert len(successes) == 1
        assert len(stale) == 1

        stored_option = await repair_repository.get_option(option.repair_id)
        stored_workspace = await itinerary_repository.get_workspace(workspace.workspace_id)
        revision_2 = await itinerary_repository.get_revision(workspace.workspace_id, 2)
        if stored_option.status == RepairStatus.APPLIED:
            assert stored_workspace.current_itinerary_revision == 2
            assert revision_2 is not None
        else:
            assert stored_option.status == RepairStatus.REJECTED
            assert stored_workspace.current_itinerary_revision == 1
            assert revision_2 is None

        await pool.execute(
            "INSERT INTO rooms(room_id, thread_id, trip_city, trip_days) "
            "VALUES ('sibling-room', 'sibling-thread', '北京', 2)"
        )
        await pool.execute(
            "INSERT INTO room_members(room_id, user_id) VALUES ('sibling-room', 'race-user')"
        )
        async with pool.acquire() as conn:
            for place_id, name, coords in (
                ("race-place-1", "故宫", {"lng": 116.397, "lat": 39.916}),
                ("race-place-2", "景山公园", {"lng": 116.396, "lat": 39.925}),
                ("race-hotel", "集成测试酒店", {"lng": 116.39, "lat": 39.92}),
            ):
                await conn.execute(
                    """
                    INSERT INTO room_places(room_id, place_id, place_data)
                    VALUES ('sibling-room', $1, $2::jsonb)
                    """,
                    place_id,
                    json.dumps(
                        {
                            "place_id": place_id,
                            "name": name,
                            "city": "北京",
                            "category": "hotel" if place_id == "race-hotel" else "attraction",
                            "opening_hours": "00:00-23:59"
                            if place_id == "race-hotel"
                            else "08:00-20:00",
                            "coords": coords,
                            "provider": "integration_fixture",
                            "retrieval_observed_at": datetime(
                                2026, 8, 20, tzinfo=timezone.utc
                            ).isoformat(),
                        },
                        ensure_ascii=False,
                    ),
                )
        sibling_revision = with_content_hash(
            ItineraryRevisionContent.model_validate(
                {
                    **revision.model_dump(exclude={"content_hash"}),
                    "itinerary_id": "sibling-itinerary",
                    "workspace_id": "sibling-workspace",
                }
            )
        )
        sibling_workspace = workspace.model_copy(update={
            "workspace_id": "sibling-workspace",
            "room_id": "sibling-room",
            "current_report_id": None,
        })
        await itinerary_repository.create_workspace(sibling_workspace, sibling_revision)
        sibling_report = await AuditApplicationService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
        ).run_current_audit(
            sibling_workspace.workspace_id,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        sibling_options = await BoundedRepairSearch(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
            repair_repository=repair_repository,
            route_refresher=ProviderRepairRouteEvidenceRefresher(
                ControlledRepairRouteProvider()
            ),
        ).propose(sibling_report.report_id, now=datetime(2026, 8, 20, tzinfo=timezone.utc))
        assert len(sibling_options) == 2

        sibling_apply_pools = [
            await asyncpg.create_pool(database_dsn, min_size=1, max_size=1)
            for _ in sibling_options
        ]
        sibling_apply_repositories = [
            PostgresRepairRepository(sibling_pool) for sibling_pool in sibling_apply_pools
        ]

        blocker = await pool.acquire()
        blocker_transaction = blocker.transaction()
        await blocker_transaction.start()
        try:
            await blocker.execute(
                "SELECT workspace_id FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                sibling_workspace.workspace_id,
            )
            apply_tasks = [
                asyncio.create_task(sibling_repository.apply_option(
                    sibling_option.repair_id,
                    actor_user_id="race-user",
                    if_match_revision=1,
                    idempotency_key=f"sibling-apply-{index}",
                ))
                for index, (sibling_option, sibling_repository) in enumerate(
                    zip(sibling_options, sibling_apply_repositories, strict=True)
                )
            ]
            waiting = 0
            for _ in range(100):
                waiting = await blocker.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND wait_event_type = 'Lock'
                    """
                )
                if waiting >= 2:
                    break
                await asyncio.sleep(0.01)
        finally:
            await blocker_transaction.commit()
            await pool.release(blocker)

        sibling_outcomes = await asyncio.gather(*apply_tasks, return_exceptions=True)
        assert waiting >= 2
        sibling_successes = [item for item in sibling_outcomes if isinstance(item, RepairApplyResult)]
        sibling_conflicts = [item for item in sibling_outcomes if isinstance(item, ItineraryDomainError)]
        assert len(sibling_successes) == 1
        assert len(sibling_conflicts) == 1
        assert sibling_conflicts[0].status_code == 409
        assert not any(isinstance(item, asyncpg.DeadlockDetectedError) for item in sibling_outcomes)
        stored_siblings = await repair_repository.list_options(sibling_report.report_id)
        assert sorted(item.status for item in stored_siblings) == [RepairStatus.APPLIED, RepairStatus.STALE]
        stored_sibling_workspace = await itinerary_repository.get_workspace(sibling_workspace.workspace_id)
        assert stored_sibling_workspace.current_itinerary_revision == 2
    finally:
        for sibling_pool in sibling_apply_pools:
            await sibling_pool.close()
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
