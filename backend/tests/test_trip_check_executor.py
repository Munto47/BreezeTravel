from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from app.audit.repositories import InMemoryAuditRepository
from app.constraints.geo_routes import RouteResult
from app.importing.models import ImportSourceType, ImportStatus, ItineraryImport
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.repairs.repositories import InMemoryRepairRepository
from app.repairs.search import BoundedRepairSearch, ProviderRepairRouteEvidenceRefresher
from app.trip_check.advice import InMemoryAdviceRepository
from app.trip_check.briefs import InMemoryTripBriefRepository, TripBriefApplicationService, TripBriefParser
from app.trip_check.executor import TripCheckAdoptionReconciler, TripCheckExecutor
from app.trip_check.models import RunBudget, RunSpec, TripCheckRunStatus, TripCheckStage
from app.trip_check.runs import InMemoryTripCheckRunRepository, TripCheckRunService


class ControlledRouteProvider:
    async def fetch(self, *, origin, destination, mode, city):
        return RouteResult(
            status="ok",
            duration_minutes=15,
            distance_km=2.0,
            transfer_count=None,
            source="controlled_trip_check_fixture",
            response_hash="c" * 64,
            observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )


def _run_spec(*, fault_profile: str = "none") -> RunSpec:
    return RunSpec(
        commit_sha="acacc94",
        prompt_version="none-p1",
        model_version="none-p1",
        provider_version="controlled-fixture-v1",
        rule_set_version="audit-v1",
        execution_mode="fixture",
        dataset_hash="a" * 64,
        snapshot_hash="b" * 64,
        fault_profile=fault_profile,
        random_seed=7,
        budget=RunBudget(timeout_seconds=30),
    )


async def _setup(*, fault_profile: str = "none"):
    date_range = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
    revision = with_content_hash(
        ItineraryRevisionContent(
            itinerary_id="trip-check-itinerary",
            workspace_id="trip-check-workspace",
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
                            stop_id="s1",
                            place_id="p1",
                            day_index=0,
                            order_index=0,
                            start_time="09:00",
                            end_time="12:00",
                            visit_duration_minutes=180,
                            raw_name="故宫博物院",
                        ),
                        ItineraryStop(
                            stop_id="s2",
                            place_id="p2",
                            day_index=0,
                            order_index=1,
                            start_time="11:00",
                            end_time="13:00",
                            visit_duration_minutes=120,
                            raw_name="景山公园",
                        ),
                    ],
                ),
                ItineraryDay(day_index=1, date=date_range.end),
            ],
            change_summary={
                "map_stop_projections": {
                    "s1": {"coords": {"lng": 116.397, "lat": 39.918}},
                    "s2": {"coords": {"lng": 116.396, "lat": 39.925}},
                }
            },
            created_by="trip-check-user",
        )
    )
    workspace = TripWorkspace(
        workspace_id=revision.workspace_id,
        room_id="trip-check-room",
        city=revision.city,
        trip_date_range=date_range,
        created_by="trip-check-user",
    )
    itinerary_repository = InMemoryItineraryRepository()
    await itinerary_repository.create_workspace(workspace, revision)
    brief_repository = InMemoryTripBriefRepository()
    imported = ItineraryImport(
        import_id="trip-check-import",
        workspace_id=workspace.workspace_id,
        source_type=ImportSourceType.MANUAL_TEXT,
        raw_text="北京2人，第1天09:00-12:00故宫博物院，11:00-13:00景山公园；第2天颐和园",
        parse_version="test",
        status=ImportStatus.READY,
        created_by="trip-check-user",
    )
    draft = TripBriefParser().parse(
        workspace=workspace,
        itinerary_import=imported,
        actor_user_id="trip-check-user",
    )
    await brief_repository.save_import_brief(draft)
    brief, _ = await TripBriefApplicationService(brief_repository).confirm(
        workspace_id=workspace.workspace_id,
        revision=1,
        actor_user_id="trip-check-user",
        idempotency_key="confirm-brief",
    )

    audit_repository = InMemoryAuditRepository(
        itinerary_repository.workspaces,
        place_records={
            workspace.workspace_id: {
                "p1": {"place_id": "p1", "name": "故宫博物院", "city": "北京", "opening_hours": "08:00-17:00"},
                "p2": {"place_id": "p2", "name": "景山公园", "city": "北京", "opening_hours": "06:00-21:00"},
            }
        },
    )
    audit_repository.current_revisions[workspace.workspace_id] = 1
    repair_repository = InMemoryRepairRepository(itinerary_repository, audit_repository)
    command_repository = InMemoryCreationCommandRepository()
    run_repository = InMemoryTripCheckRunRepository(lease_seconds=0)
    advice_repository = InMemoryAdviceRepository()
    run, _ = await TripCheckRunService(
        run_repository=run_repository,
        itinerary_repository=itinerary_repository,
        brief_repository=brief_repository,
    ).create(
        workspace_id=workspace.workspace_id,
        itinerary_revision=1,
        brief_revision=brief.revision,
        run_spec=_run_spec(fault_profile=fault_profile),
        actor_user_id="trip-check-user",
        idempotency_key=f"create-{fault_profile}",
    )
    executor = TripCheckExecutor(
        run_repository=run_repository,
        itinerary_repository=itinerary_repository,
        audit_repository=audit_repository,
        advice_repository=advice_repository,
        brief_repository=brief_repository,
        repair_search=BoundedRepairSearch(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
            repair_repository=repair_repository,
            route_refresher=ProviderRepairRouteEvidenceRefresher(ControlledRouteProvider()),
        ),
        command_repository=command_repository,
    )
    return (
        executor,
        run_repository,
        audit_repository,
        advice_repository,
        brief_repository,
        itinerary_repository,
        repair_repository,
        run,
    )


def test_trip_check_executor_reaches_advice_with_replayable_stage_receipts():
    async def scenario():
        executor, runs, audits, advice, _, _, _, created = await _setup()
        finished = await executor.execute(created.run_id)
        assert finished.stage == TripCheckStage.WAIT_ADOPTION
        assert finished.status == TripCheckRunStatus.WAITING
        assert finished.evidence_snapshot_id
        assert finished.report_id
        assert finished.advice_bundle_id
        assert finished.completed_stages[-3:] == [
            TripCheckStage.COLLECT_EVIDENCE,
            TripCheckStage.AUDIT,
            TripCheckStage.BUILD_ADVICE,
        ]
        assert len(
            [item for item in audits.snapshots.values() if item.itinerary_revision == created.itinerary_revision]
        ) == 1
        assert len(runs.receipts) == 3
        bundle = await advice.get_bundle(finished.advice_bundle_id)
        report = await audits.get_report(finished.report_id)
        assert bundle is not None and report is not None
        non_pass = {item.finding_id for item in report.findings if item.status.value != "SATISFIED"}
        assert {item.finding_id for item in bundle.actions} == non_pass
        assert any(item.repair_id for item in bundle.actions)

    asyncio.run(scenario())


def test_terminate_after_evidence_resumes_without_duplicate_snapshot_or_receipt():
    async def scenario():
        executor, runs, audits, _, briefs, itineraries, _, created = await _setup(
            fault_profile="terminate_after_evidence"
        )
        interrupted = await executor.execute(created.run_id)
        assert interrupted.stage == TripCheckStage.AUDIT
        assert interrupted.status == TripCheckRunStatus.RUNNING
        assert len(
            [item for item in audits.snapshots.values() if item.itinerary_revision == created.itinerary_revision]
        ) == 1
        assert len(runs.receipts) == 1

        resumed, replayed = await TripCheckRunService(
            run_repository=runs,
            itinerary_repository=itineraries,
            brief_repository=briefs,
        ).resume(
            run_id=created.run_id,
            expected_version=interrupted.version,
            config_hash=interrupted.config_hash,
            actor_user_id="trip-check-user",
            idempotency_key="resume-after-evidence",
        )
        assert replayed is False
        completed = await executor.execute(created.run_id, lease_owner=resumed.lease_owner)
        assert completed.stage == TripCheckStage.WAIT_ADOPTION
        assert len(
            [item for item in audits.snapshots.values() if item.itinerary_revision == created.itinerary_revision]
        ) == 1
        assert len(runs.receipts) == 3
        assert len(await itineraries.list_revisions(created.workspace_id)) == 1

    asyncio.run(scenario())


def test_adopted_repair_completes_postcheck_once_without_duplicate_revision():
    async def scenario():
        executor, runs, audits, advice, _, itineraries, repairs, created = await _setup()
        waiting = await executor.execute(created.run_id)
        bundle = await advice.get_bundle(waiting.advice_bundle_id)
        assert bundle is not None
        repair_id = next(item.repair_id for item in bundle.actions if item.repair_id)
        applied = await repairs.apply_option(
            repair_id,
            actor_user_id="trip-check-user",
            if_match_revision=1,
            idempotency_key="apply-trip-check-repair",
        )
        reconciler = TripCheckAdoptionReconciler(
            run_repository=runs,
            audit_repository=audits,
            advice_repository=advice,
        )
        completed = await reconciler.reconcile(applied)
        assert completed is not None
        assert completed.stage == TripCheckStage.POSTCHECK
        assert completed.status == TripCheckRunStatus.SUCCEEDED
        assert completed.report_id == applied.postcheck_report_id
        assert len(await itineraries.list_revisions(created.workspace_id)) == 2
        assert len(advice.lineage) == 1

        replay = await repairs.apply_option(
            repair_id,
            actor_user_id="trip-check-user",
            if_match_revision=1,
            idempotency_key="apply-trip-check-repair",
        )
        replayed_run = await reconciler.reconcile(replay)
        assert replayed_run == completed
        assert len(await itineraries.list_revisions(created.workspace_id)) == 2
        assert len(advice.lineage) == 1
        assert len(runs.receipts) == 4

    asyncio.run(scenario())
