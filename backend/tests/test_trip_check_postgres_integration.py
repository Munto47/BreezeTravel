from __future__ import annotations

import asyncio
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
from app.importing.models import ImportSourceType, ImportStatus, ItineraryImport
from app.itineraries.errors import IdempotencyKeyReusedError
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import PostgresItineraryRepository
from app.operations.repositories import PostgresCreationCommandRepository
from app.repairs.repositories import PostgresRepairRepository
from app.repairs.search import BoundedRepairSearch, ProviderRepairRouteEvidenceRefresher
from app.trip_check.advice import PostgresAdviceRepository
from app.trip_check.briefs import (
    PostgresTripBriefRepository,
    TripBriefApplicationService,
    TripBriefParser,
)
from app.trip_check.errors import RunConfigMismatchError, TripCheckRunNotResumableError
from app.trip_check.executor import TripCheckExecutor
from app.trip_check.models import RunBudget, RunSpec, TripCheckRun, TripCheckRunStatus, TripCheckStage
from app.trip_check.runs import PostgresTripCheckRunRepository, TripCheckRunService


pytestmark = pytest.mark.integration


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


def _run_spec(*, fault_profile: str, provider_version: str = "controlled-fixture-v1") -> RunSpec:
    return RunSpec(
        commit_sha="3bd93ea",
        prompt_version="none-p1",
        model_version="none-p1",
        provider_version=provider_version,
        rule_set_version="audit-v1",
        execution_mode="fixture",
        dataset_hash="a" * 64,
        snapshot_hash="b" * 64,
        fault_profile=fault_profile,
        random_seed=7,
        budget=RunBudget(timeout_seconds=30),
    )


class UnexpectedRouteProvider:
    async def fetch(self, **kwargs):
        del kwargs
        raise AssertionError("single-stop PostgreSQL fixture must not call a route Provider")


def _executor(
    pool,
    *,
    runs: PostgresTripCheckRunRepository,
    itineraries: PostgresItineraryRepository,
    briefs: PostgresTripBriefRepository,
) -> TripCheckExecutor:
    audits = PostgresAuditRepository(pool)
    return TripCheckExecutor(
        run_repository=runs,
        itinerary_repository=itineraries,
        audit_repository=audits,
        advice_repository=PostgresAdviceRepository(pool),
        brief_repository=briefs,
        repair_search=BoundedRepairSearch(
            itinerary_repository=itineraries,
            audit_repository=audits,
            repair_repository=PostgresRepairRepository(pool),
            route_refresher=ProviderRepairRouteEvidenceRefresher(UnexpectedRouteProvider()),
        ),
        command_repository=PostgresCreationCommandRepository(pool),
    )


@pytest.mark.asyncio
async def test_postgres_trip_check_termination_restart_replay_and_lease_cas():
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")

    database_name = f"breezetravel_trip_check_{uuid4().hex[:10]}"
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

        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=6)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users(user_id, nickname) VALUES ('trip-check-pg-user', 'TripCheck PG')"
            )
            await conn.execute(
                """
                INSERT INTO rooms(room_id, thread_id, trip_city, trip_days)
                VALUES ('trip-check-pg-room', 'trip-check-pg-thread', '北京', 2)
                """
            )

        date_range = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
        revision = with_content_hash(
            ItineraryRevisionContent(
                itinerary_id="trip-check-pg-itinerary",
                workspace_id="trip-check-pg-workspace",
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
                                stop_id="trip-check-pg-stop",
                                place_id="trip-check-pg-place",
                                day_index=0,
                                order_index=0,
                                start_time="09:00",
                                end_time="11:00",
                                visit_duration_minutes=120,
                                raw_name="故宫博物院",
                            )
                        ],
                    ),
                    ItineraryDay(day_index=1, date=date_range.end),
                ],
                created_by="trip-check-pg-user",
            )
        )
        workspace = TripWorkspace(
            workspace_id=revision.workspace_id,
            room_id="trip-check-pg-room",
            city=revision.city,
            trip_date_range=date_range,
            current_itinerary_revision=1,
            created_by="trip-check-pg-user",
        )
        itineraries = PostgresItineraryRepository(pool)
        await itineraries.create_workspace(workspace, revision)

        briefs = PostgresTripBriefRepository(pool)
        itinerary_import = ItineraryImport(
            import_id="trip-check-pg-import",
            workspace_id=workspace.workspace_id,
            source_type=ImportSourceType.MANUAL_TEXT,
            raw_text="北京2人。第1天 09:00-11:00 故宫博物院；第2天休息。",
            parse_version="controlled-pg-test",
            status=ImportStatus.READY,
            created_by="trip-check-pg-user",
        )
        draft = TripBriefParser().parse(
            workspace=workspace,
            itinerary_import=itinerary_import,
            actor_user_id="trip-check-pg-user",
        )
        await briefs.save_import_brief(draft)
        brief, _ = await TripBriefApplicationService(briefs).confirm(
            workspace_id=workspace.workspace_id,
            revision=draft.revision,
            actor_user_id="trip-check-pg-user",
            idempotency_key="trip-check-pg-confirm",
        )

        runs = PostgresTripCheckRunRepository(pool, lease_seconds=0)
        service = TripCheckRunService(
            run_repository=runs,
            itinerary_repository=itineraries,
            brief_repository=briefs,
        )
        spec = _run_spec(fault_profile="terminate_after_evidence")
        created, replayed = await service.create(
            workspace_id=workspace.workspace_id,
            itinerary_revision=1,
            brief_revision=brief.revision,
            run_spec=spec,
            actor_user_id="trip-check-pg-user",
            idempotency_key="trip-check-pg-create",
        )
        assert replayed is False
        replay, replayed = await service.create(
            workspace_id=workspace.workspace_id,
            itinerary_revision=1,
            brief_revision=brief.revision,
            run_spec=spec,
            actor_user_id="trip-check-pg-user",
            idempotency_key="trip-check-pg-create",
        )
        assert replayed is True
        assert replay.run_id == created.run_id
        with pytest.raises(IdempotencyKeyReusedError):
            await service.create(
                workspace_id=workspace.workspace_id,
                itinerary_revision=1,
                brief_revision=brief.revision,
                run_spec=_run_spec(
                    fault_profile="terminate_after_evidence",
                    provider_version="different-controlled-fixture",
                ),
                actor_user_id="trip-check-pg-user",
                idempotency_key="trip-check-pg-create",
            )

        interrupted = await _executor(
            pool,
            runs=runs,
            itineraries=itineraries,
            briefs=briefs,
        ).execute(created.run_id)
        assert interrupted.stage == TripCheckStage.AUDIT
        assert interrupted.status == TripCheckRunStatus.RUNNING
        first_events = await runs.list_events(created.run_id)
        last_event_id = first_events[-1].event_id
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM evidence_snapshots WHERE workspace_id = $1",
                workspace.workspace_id,
            ) == 1
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_check_side_effect_receipts WHERE run_id = $1",
                created.run_id,
            ) == 1
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM itinerary_revisions WHERE workspace_id = $1",
                workspace.workspace_id,
            ) == 1

        with pytest.raises(RunConfigMismatchError):
            await service.resume(
                run_id=created.run_id,
                expected_version=interrupted.version,
                config_hash="f" * 64,
                actor_user_id="trip-check-pg-user",
                idempotency_key="trip-check-pg-bad-config",
            )

        await pool.close()
        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=6)
        itineraries = PostgresItineraryRepository(pool)
        briefs = PostgresTripBriefRepository(pool)
        runs = PostgresTripCheckRunRepository(pool, lease_seconds=0)
        service = TripCheckRunService(
            run_repository=runs,
            itinerary_repository=itineraries,
            brief_repository=briefs,
        )
        resumed, replayed = await service.resume(
            run_id=created.run_id,
            expected_version=interrupted.version,
            config_hash=interrupted.config_hash,
            actor_user_id="trip-check-pg-user",
            idempotency_key="trip-check-pg-resume",
        )
        assert replayed is False
        completed = await _executor(
            pool,
            runs=runs,
            itineraries=itineraries,
            briefs=briefs,
        ).execute(created.run_id, lease_owner=resumed.lease_owner)
        assert completed.stage == TripCheckStage.WAIT_ADOPTION
        assert completed.status == TripCheckRunStatus.WAITING
        assert [item.stage for item in await runs.list_events(created.run_id, after_event_id=last_event_id)]
        before_replay = {}
        async with pool.acquire() as conn:
            for table in (
                "itinerary_revisions",
                "evidence_snapshots",
                "audit_reports",
                "advice_bundles",
                "trip_check_side_effect_receipts",
            ):
                before_replay[table] = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {table} WHERE workspace_id = $1"
                    if table not in {"advice_bundles", "trip_check_side_effect_receipts"}
                    else (
                        "SELECT COUNT(*) FROM advice_bundles WHERE workspace_id = $1"
                        if table == "advice_bundles"
                        else "SELECT COUNT(*) FROM trip_check_side_effect_receipts WHERE run_id = $1"
                    ),
                    workspace.workspace_id
                    if table != "trip_check_side_effect_receipts"
                    else created.run_id,
                )
            assert before_replay == {
                "itinerary_revisions": 1,
                "evidence_snapshots": 1,
                "audit_reports": 1,
                "advice_bundles": 1,
                "trip_check_side_effect_receipts": 3,
            }

        replayed_run = await _executor(
            pool,
            runs=runs,
            itineraries=itineraries,
            briefs=briefs,
        ).execute(created.run_id)
        assert replayed_run == completed
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM trip_check_side_effect_receipts WHERE run_id = $1",
                created.run_id,
            ) == before_replay["trip_check_side_effect_receipts"]

        contender, _ = await service.create(
            workspace_id=workspace.workspace_id,
            itinerary_revision=1,
            brief_revision=brief.revision,
            run_spec=_run_spec(fault_profile="none"),
            actor_user_id="trip-check-pg-user",
            idempotency_key="trip-check-pg-contender",
        )
        now = datetime.now(timezone.utc)
        outcomes = await asyncio.gather(
            runs.claim_for_execution(contender.run_id, now=now),
            runs.claim_for_execution(contender.run_id, now=now),
            return_exceptions=True,
        )
        assert sum(isinstance(item, TripCheckRun) for item in outcomes) == 1
        assert sum(isinstance(item, TripCheckRunNotResumableError) for item in outcomes) == 1
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
