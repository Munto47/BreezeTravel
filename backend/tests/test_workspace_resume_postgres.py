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

from app.audit.models import (
    AuditFinding,
    AuditReport,
    AuditSeverity,
    AuditStatus,
    EvidenceSnapshot,
)
from app.audit.repositories import PostgresAuditRepository
from app.importing.models import ImportSourceType, ImportStatus, ItineraryImport
from app.importing.repositories import PostgresImportRepository
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import PostgresItineraryRepository, insert_revision_record
from app.itineraries.resume_repository import (
    PostgresWorkspaceResumeRepository,
    WorkspaceResumeNotFound,
    WorkspaceStateInconsistent,
)
from app.repairs.models import RepairOperation, RepairOperationType, RepairOption
from app.repairs.repositories import PostgresRepairRepository


pytestmark = pytest.mark.integration
NOW = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


def _revision(workspace_id: str, revision: int, source_type: RevisionSource):
    date_range = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
    return with_content_hash(ItineraryRevisionContent(
        itinerary_id="resume-pg-itinerary",
        workspace_id=workspace_id,
        revision=revision,
        parent_revision=revision - 1 if revision > 1 else None,
        source_type=source_type,
        city="北京",
        date_range=date_range,
        days=[
            ItineraryDay(
                day_index=0,
                date=date_range.start,
                stops=[ItineraryStop(
                    stop_id="resume-pg-stop",
                    place_id="resume-pg-place",
                    day_index=0,
                    order_index=0,
                    start_time="09:00" if revision == 1 else "09:30",
                    end_time="11:00" if revision == 1 else "11:30",
                )],
            ),
            ItineraryDay(day_index=1, date=date_range.end, stops=[]),
        ],
        change_summary={"repair_id": "resume-pg-repair"} if revision == 2 else {},
        created_by="resume-pg-owner",
        created_at=NOW,
    ))


class BlockingResumeRepository(PostgresWorkspaceResumeRepository):
    def __init__(self, pool, snapshot_open: asyncio.Event, continue_read: asyncio.Event):
        super().__init__(pool)
        self.snapshot_open = snapshot_open
        self.continue_read = continue_read

    async def _after_workspace_read(self, conn, workspace) -> None:
        self.snapshot_open.set()
        await self.continue_read.wait()


@pytest.mark.asyncio
async def test_postgres_resume_survives_pool_restart_and_reads_one_consistent_snapshot():
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")

    database_name = f"breezetravel_workspace_resume_{uuid4().hex[:10]}"
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
        workspace_id = "resume-pg-workspace"
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users(user_id, nickname) VALUES
                    ('resume-pg-owner', 'Owner'),
                    ('resume-pg-outsider', 'Outsider')
                """
            )
            await conn.execute(
                """
                INSERT INTO rooms(room_id, thread_id, trip_city, trip_days)
                VALUES ('resume-pg-room', 'resume-pg-thread', '北京', 2)
                """
            )
            await conn.execute(
                """
                INSERT INTO room_members(room_id, user_id)
                VALUES ('resume-pg-room', 'resume-pg-owner')
                """
            )

        revision_1 = _revision(workspace_id, 1, RevisionSource.IMPORT)
        workspace = TripWorkspace(
            workspace_id=workspace_id,
            room_id="resume-pg-room",
            city="北京",
            trip_date_range=revision_1.date_range,
            current_itinerary_revision=1,
            created_by="resume-pg-owner",
            created_at=NOW,
            updated_at=NOW,
        )
        itineraries = PostgresItineraryRepository(pool)
        await itineraries.create_workspace(workspace, revision_1)

        itinerary_import = ItineraryImport(
            import_id="resume-pg-import",
            workspace_id=workspace_id,
            source_type=ImportSourceType.AI_TEXT,
            raw_text="第1天：故宫",
            parse_version="parser-v1",
            status=ImportStatus.READY,
            state_version=2,
            created_by="resume-pg-owner",
            created_at=NOW,
            updated_at=NOW,
        )
        await PostgresImportRepository(pool).create_import_bundle(
            itinerary_import,
            basis={"current_import_id": None},
        )

        source_snapshot = EvidenceSnapshot(
            snapshot_id="resume-pg-source-snapshot",
            workspace_id=workspace_id,
            itinerary_revision=1,
            policy_version="policy-v1",
            created_at=NOW,
        )
        finding = AuditFinding(
            finding_id="resume-pg-finding",
            rule_id="route.buffer",
            rule_version="1.0.0",
            status=AuditStatus.VIOLATED,
            severity=AuditSeverity.MEDIUM,
            reason_code="BUFFER_TOO_SHORT",
            message="需要增加换乘缓冲",
            repairable=True,
        )
        source_report = AuditReport(
            report_id="resume-pg-source-report",
            workspace_id=workspace_id,
            itinerary_id=revision_1.itinerary_id,
            itinerary_revision=1,
            task_id="resume-pg-task",
            task_revision=1,
            evidence_snapshot_id=source_snapshot.snapshot_id,
            audit_rule_set_version="rules-v1",
            report_input_hash="a" * 64,
            overall_status=AuditStatus.VIOLATED,
            findings=[finding],
            created_at=NOW,
        )
        audits = PostgresAuditRepository(pool)
        await audits.save_audit_bundle(
            source_snapshot,
            source_report,
            basis={
                "current_itinerary_revision": 1,
                "current_task_spec_revision": None,
                "current_member_constraint_revision": None,
                "current_report_id": None,
            },
        )

        revision_2 = _revision(workspace_id, 2, RevisionSource.REPAIR)
        postcheck_snapshot = EvidenceSnapshot(
            snapshot_id="resume-pg-postcheck-snapshot",
            workspace_id=workspace_id,
            itinerary_revision=2,
            policy_version="policy-v1",
            created_at=NOW,
        )
        postcheck_report = AuditReport(
            report_id="resume-pg-postcheck-report",
            workspace_id=workspace_id,
            itinerary_id=revision_2.itinerary_id,
            itinerary_revision=2,
            task_id="resume-pg-task",
            task_revision=1,
            evidence_snapshot_id=postcheck_snapshot.snapshot_id,
            audit_rule_set_version="rules-v1",
            report_input_hash="b" * 64,
            overall_status=AuditStatus.SATISFIED,
            created_at=NOW,
        )
        await audits.save_preview_bundle(postcheck_snapshot, postcheck_report)
        option = RepairOption(
            repair_id="resume-pg-repair",
            source_report_id=source_report.report_id,
            base_itinerary_revision=1,
            operations=[RepairOperation(
                operation=RepairOperationType.ADJUST_TIME,
                payload={"stop_id": "resume-pg-stop", "start_time": "09:30"},
                rationale="add route buffer",
            )],
            targeted_finding_ids=[finding.finding_id],
            edit_cost=1,
            risk_cost=0,
            new_unknown_count=0,
            result_preview=revision_2,
            postcheck_report_id=postcheck_report.report_id,
            created_at=NOW,
        )
        await PostgresRepairRepository(pool).save_option(option)

        snapshot_open = asyncio.Event()
        continue_read = asyncio.Event()
        blocked_repository = BlockingResumeRepository(pool, snapshot_open, continue_read)
        in_flight = asyncio.create_task(
            blocked_repository.get_resume(workspace_id, "resume-pg-owner")
        )
        await asyncio.wait_for(snapshot_open.wait(), timeout=5)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE repair_options SET status = 'REJECTED' WHERE repair_id = $1",
                option.repair_id,
            )
        continue_read.set()
        consistent = await asyncio.wait_for(in_flight, timeout=5)

        # The transaction began before the concurrent decision and therefore
        # returns the old, internally consistent proposal set.
        assert [item.repair_id for item in consistent.proposed_repairs] == [option.repair_id]
        assert consistent.current_report == source_report
        old_etag = consistent.strong_etag()

        after_decision = await PostgresWorkspaceResumeRepository(pool).get_resume(
            workspace_id,
            "resume-pg-owner",
        )
        assert after_decision.proposed_repairs == []
        assert after_decision.strong_etag() != old_etag

        # Materialize the already-postchecked preview as an applied repair.
        async with pool.acquire() as conn, conn.transaction():
            await insert_revision_record(conn, revision_2)
            await conn.execute(
                """
                UPDATE repair_options
                SET status = 'APPLIED', decided_by = $2, decided_at = $3
                WHERE repair_id = $1
                """,
                option.repair_id,
                "resume-pg-owner",
                NOW,
            )
            await conn.execute(
                """
                UPDATE trip_workspaces
                SET current_itinerary_revision = 2,
                    current_report_id = $2,
                    updated_at = $3
                WHERE workspace_id = $1
                """,
                workspace_id,
                postcheck_report.report_id,
                NOW,
            )

        applied = await PostgresWorkspaceResumeRepository(pool).get_resume(
            workspace_id,
            "resume-pg-owner",
        )
        assert applied.current_revision == revision_2
        assert applied.current_import == itinerary_import
        assert applied.current_report == postcheck_report
        assert applied.current_evidence == postcheck_snapshot
        assert applied.proposed_repairs == []
        assert applied.applied_repair is not None
        assert applied.applied_repair.repair_id == option.repair_id
        assert applied.write_etags.itinerary == '"2"'
        assert applied.write_etags.import_ == '"2"'

        # Recreate the client pool to model an application restart. No local
        # repository state is needed to recover the same workspace snapshot.
        await pool.close()
        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=4)
        restarted = await PostgresWorkspaceResumeRepository(pool).get_resume(
            workspace_id,
            "resume-pg-owner",
        )
        assert restarted == applied
        assert restarted.strong_etag() == applied.strong_etag()

        repository = PostgresWorkspaceResumeRepository(pool)
        with pytest.raises(WorkspaceResumeNotFound):
            await repository.get_resume(workspace_id, "resume-pg-outsider")
        with pytest.raises(WorkspaceResumeNotFound):
            await repository.get_resume("missing-workspace", "resume-pg-outsider")

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE trip_workspaces SET current_report_id = 'missing-report' WHERE workspace_id = $1",
                workspace_id,
            )
        with pytest.raises(WorkspaceStateInconsistent):
            await repository.get_resume(workspace_id, "resume-pg-owner")
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
