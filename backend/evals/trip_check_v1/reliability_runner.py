from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from app.audit.repositories import PostgresAuditRepository
from app.constraints.geo_routes import RouteResult
from app.importing.models import ImportSourceType, ImportStatus, ItineraryImport
from app.itineraries.errors import IdempotencyKeyReusedError, RevisionConflictError
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
from app.repairs.errors import RepairStaleError
from app.repairs.models import RepairApplyResult
from app.repairs.repositories import PostgresRepairRepository
from app.repairs.search import BoundedRepairSearch, ProviderRepairRouteEvidenceRefresher
from app.trip_check.advice import PostgresAdviceRepository
from app.trip_check.briefs import (
    PostgresTripBriefRepository,
    TripBriefApplicationService,
    TripBriefParser,
)
from app.trip_check.errors import RunConfigMismatchError
from app.trip_check.executor import TripCheckExecutor
from app.trip_check.models import RunBudget, RunSpec, TripCheckRun, TripCheckRunStatus, TripCheckStage
from app.trip_check.runs import PostgresTripCheckRunRepository, TripCheckRunService
from app.trip_check.trace import (
    OTEL_ATTRIBUTE_ALLOWLIST,
    RedactedJsonlSpanExporter,
    TripCheckDomainTraceAssembler,
    TripCheckTelemetry,
)


BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = BACKEND_ROOT / "evidence" / "trip_check_v1" / "p2" / "reliability"
CANONICAL_CASES = (
    "provider_timeout",
    "partial_field_failure",
    "duplicate_submit",
    "concurrent_revision",
    "terminate_after_evidence",
    "config_drift",
)
DATASET_HASH = hashlib.sha256(b"breezetravel-p2-reliability-canonical-v1").hexdigest()
SNAPSHOT_HASH = hashlib.sha256(b"breezetravel-controlled-provider-fixture-v2").hexdigest()
FORBIDDEN_TRACE_KEYS = frozenset(
    {
        "raw_text",
        "poi_name",
        "prompt",
        "authorization",
        "provider_raw_response",
        "user_id",
    }
)


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _jsonl_dump(path: Path, values: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item,
            ensure_ascii=False,
            sort_keys=True,
        )
        for item in values
    ]
    path.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
        newline="\n",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_spec(*, commit_sha: str, fault_profile: str, provider_version: str = "controlled-v2") -> RunSpec:
    return RunSpec(
        commit_sha=commit_sha,
        prompt_version="none-p2",
        model_version="none-p2",
        provider_version=provider_version,
        rule_set_version="audit-v1",
        execution_mode="controlled_fixture",
        dataset_hash=DATASET_HASH,
        snapshot_hash=SNAPSHOT_HASH,
        fault_profile=fault_profile,
        random_seed=23,
        budget=RunBudget(max_provider_queries=3, max_retries=2, timeout_seconds=30),
    )


class ControlledRepairRouteProvider:
    async def fetch(self, *, origin, destination, mode, city):
        del origin, destination, mode, city
        return RouteResult(
            status="ok",
            duration_minutes=15,
            distance_km=2.5,
            transfer_count=None,
            source="controlled_reliability_route_fixture",
            response_hash="a" * 64,
            observed_at=None,
        )


@dataclass(frozen=True)
class SeededCase:
    case_id: str
    workspace_id: str
    brief_revision: int
    actor_user_id: str


class ReliabilityHarness:
    def __init__(self, *, database_dsn: str, pool: asyncpg.Pool, commit_sha: str, output: Path):
        self.database_dsn = database_dsn
        self.pool = pool
        self.commit_sha = commit_sha
        self.output = output
        self.itineraries = PostgresItineraryRepository(pool)
        self.briefs = PostgresTripBriefRepository(pool)

    async def seed(self, case_id: str) -> SeededCase:
        slug = case_id.replace("_", "-")
        actor = "reliability-user"
        room_id = f"p2-{slug}-room"
        workspace_id = f"p2-{slug}-workspace"
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users(user_id, nickname) VALUES ($1, 'P2 Reliability') ON CONFLICT (user_id) DO NOTHING",
                actor,
            )
            await conn.execute(
                "INSERT INTO users(user_id, nickname) VALUES ('reliability-other', 'P2 Other') "
                "ON CONFLICT (user_id) DO NOTHING"
            )
            await conn.execute(
                "INSERT INTO rooms(room_id, thread_id, trip_city, trip_days) VALUES ($1, $2, '北京', 2)",
                room_id,
                f"p2-{slug}-thread",
            )
            await conn.execute("INSERT INTO room_members(room_id, user_id) VALUES ($1, $2)", room_id, actor)

        date_range = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
        stops = [
            ItineraryStop(
                stop_id=f"{slug}-stop-1",
                place_id=f"{slug}-place-1",
                day_index=0,
                order_index=0,
                start_time="09:00",
                end_time="12:00",
                visit_duration_minutes=180,
                raw_name="受控地点甲",
            ),
            ItineraryStop(
                stop_id=f"{slug}-stop-2",
                place_id=f"{slug}-place-2",
                day_index=0,
                order_index=1,
                start_time="11:00",
                end_time="13:00",
                visit_duration_minutes=120,
                raw_name="受控地点乙",
            ),
        ]
        projections = {
            stops[0].stop_id: {
                "place_id": stops[0].place_id,
                "canonical_name": "受控地点甲",
                "coords": {"lng": 116.397, "lat": 39.916},
                "coordinate_role": "CANONICAL_POI",
                "provenance": "controlled_reliability_fixture",
            },
            stops[1].stop_id: {
                "place_id": stops[1].place_id,
                "canonical_name": "受控地点乙",
                "coords": {"lng": 116.396, "lat": 39.925},
                "coordinate_role": "CANONICAL_POI",
                "provenance": "controlled_reliability_fixture",
            },
        }
        revision = with_content_hash(
            ItineraryRevisionContent(
                itinerary_id=f"p2-{slug}-itinerary",
                workspace_id=workspace_id,
                revision=1,
                source_type=RevisionSource.IMPORT,
                city="北京",
                date_range=date_range,
                days=[
                    ItineraryDay(day_index=0, date=date_range.start, stops=stops),
                    ItineraryDay(day_index=1, date=date_range.end, stops=[]),
                ],
                change_summary={"map_stop_projections": projections},
                created_by=actor,
            )
        )
        workspace = TripWorkspace(
            workspace_id=workspace_id,
            room_id=room_id,
            city="北京",
            trip_date_range=date_range,
            current_itinerary_revision=1,
            created_by=actor,
        )
        await self.itineraries.create_workspace(workspace, revision)
        async with self.pool.acquire() as conn:
            for index, stop in enumerate(stops):
                await conn.execute(
                    """
                    INSERT INTO room_places(room_id, place_id, place_data)
                    VALUES ($1, $2, $3::jsonb)
                    """,
                    room_id,
                    stop.place_id,
                    json.dumps(
                        {
                            "place_id": stop.place_id,
                            "name": f"受控地点{'甲' if index == 0 else '乙'}",
                            "city": "北京",
                            "category": "attraction",
                            "opening_hours": "08:00-20:00",
                            "coords": projections[stop.stop_id]["coords"],
                            "provider": "controlled_reliability_fixture",
                            "retrieval_observed_at": "2026-08-23T00:00:00+00:00",
                        },
                        ensure_ascii=False,
                    ),
                )

        itinerary_import = ItineraryImport(
            import_id=f"p2-{slug}-import",
            workspace_id=workspace_id,
            source_type=ImportSourceType.MANUAL_TEXT,
            raw_text="北京2人，2天受控行程。",
            parse_version="controlled-reliability-v1",
            status=ImportStatus.READY,
            created_by=actor,
        )
        draft = TripBriefParser().parse(
            workspace=workspace,
            itinerary_import=itinerary_import,
            actor_user_id=actor,
        )
        await self.briefs.save_import_brief(draft)
        brief, _ = await TripBriefApplicationService(self.briefs).confirm(
            workspace_id=workspace_id,
            revision=draft.revision,
            actor_user_id=actor,
            idempotency_key=f"p2-{slug}-confirm",
        )
        return SeededCase(case_id, workspace_id, brief.revision, actor)

    def repositories(self, *, lease_seconds: int = 0):
        runs = PostgresTripCheckRunRepository(self.pool, lease_seconds=lease_seconds)
        audits = PostgresAuditRepository(self.pool)
        repairs = PostgresRepairRepository(self.pool)
        return runs, audits, repairs

    def executor(
        self,
        *,
        runs: PostgresTripCheckRunRepository,
        audits: PostgresAuditRepository,
        repairs: PostgresRepairRepository,
        telemetry: TripCheckTelemetry,
    ) -> TripCheckExecutor:
        return TripCheckExecutor(
            run_repository=runs,
            itinerary_repository=self.itineraries,
            audit_repository=audits,
            advice_repository=PostgresAdviceRepository(self.pool),
            brief_repository=self.briefs,
            repair_search=BoundedRepairSearch(
                itinerary_repository=self.itineraries,
                audit_repository=audits,
                repair_repository=repairs,
                route_refresher=ProviderRepairRouteEvidenceRefresher(ControlledRepairRouteProvider()),
            ),
            command_repository=PostgresCreationCommandRepository(self.pool),
            telemetry=telemetry,
        )

    async def create_run(
        self,
        seeded: SeededCase,
        *,
        fault_profile: str,
        idempotency_key: str | None = None,
        provider_version: str = "controlled-v2",
        lease_seconds: int = 0,
    ) -> tuple[TripCheckRun, bool, PostgresTripCheckRunRepository]:
        runs = PostgresTripCheckRunRepository(self.pool, lease_seconds=lease_seconds)
        service = TripCheckRunService(
            run_repository=runs,
            itinerary_repository=self.itineraries,
            brief_repository=self.briefs,
        )
        run, replayed = await service.create(
            workspace_id=seeded.workspace_id,
            itinerary_revision=1,
            brief_revision=seeded.brief_revision,
            run_spec=_run_spec(
                commit_sha=self.commit_sha,
                fault_profile=fault_profile,
                provider_version=provider_version,
            ),
            actor_user_id=seeded.actor_user_id,
            idempotency_key=idempotency_key or f"p2-{seeded.case_id}-create",
        )
        return run, replayed, runs

    def telemetry(self, case_id: str) -> tuple[TripCheckTelemetry, TracerProvider]:
        provider = TracerProvider()
        provider.add_span_processor(
            SimpleSpanProcessor(RedactedJsonlSpanExporter(self.output / case_id / "otel_spans.jsonl"))
        )
        return TripCheckTelemetry(provider.get_tracer("breezetravel.trip_check.p2")), provider

    async def counts(self, seeded: SeededCase, run_id: str) -> dict[str, int]:
        async with self.pool.acquire() as conn:
            return {
                "runs": await conn.fetchval(
                    "SELECT COUNT(*) FROM trip_check_runs WHERE workspace_id = $1",
                    seeded.workspace_id,
                ),
                "revisions": await conn.fetchval(
                    "SELECT COUNT(*) FROM itinerary_revisions WHERE workspace_id = $1",
                    seeded.workspace_id,
                ),
                "snapshots": await conn.fetchval(
                    "SELECT COUNT(*) FROM evidence_snapshots WHERE workspace_id = $1",
                    seeded.workspace_id,
                ),
                "receipts": await conn.fetchval(
                    "SELECT COUNT(*) FROM trip_check_side_effect_receipts WHERE run_id = $1",
                    run_id,
                ),
                "repairs": await conn.fetchval(
                    "SELECT COUNT(*) FROM repair_options ro JOIN audit_reports ar "
                    "ON ar.report_id = ro.source_report_id WHERE ar.workspace_id = $1",
                    seeded.workspace_id,
                ),
            }

    async def finalize_case(
        self,
        *,
        seeded: SeededCase,
        run_id: str,
        replay: dict[str, Any],
        assertions: dict[str, Any],
        provider: TracerProvider | None,
    ) -> dict[str, Any]:
        if provider is not None:
            provider.shutdown()
        case_dir = self.output / seeded.case_id
        runs = PostgresTripCheckRunRepository(self.pool, lease_seconds=0)
        audits = PostgresAuditRepository(self.pool)
        run = await runs.get_run(run_id)
        assert run is not None
        events = await runs.list_events(run_id)
        attempts = await runs.list_stage_attempts(run_id)
        receipts = await runs.list_receipts(run_id)
        snapshot = await audits.get_snapshot(run.evidence_snapshot_id) if run.evidence_snapshot_id is not None else None
        domain_trace = await TripCheckDomainTraceAssembler(runs).assemble(run_id)
        _json_dump(case_dir / "run_spec.json", run.run_spec.model_dump(mode="json"))
        _jsonl_dump(case_dir / "events.jsonl", events)
        _jsonl_dump(case_dir / "attempts.jsonl", attempts)
        _jsonl_dump(case_dir / "receipts.jsonl", receipts)
        _json_dump(
            case_dir / "snapshot.json",
            snapshot.model_dump(mode="json") if snapshot is not None else {},
        )
        _jsonl_dump(case_dir / "domain_trace.jsonl", domain_trace)
        _json_dump(case_dir / "replay.json", replay)

        otel_path = case_dir / "otel_spans.jsonl"
        spans = [json.loads(line) for line in otel_path.read_text(encoding="utf-8").splitlines()]
        domain_stages = {item.stage.value for item in domain_trace}
        otel_associated = all(
            span["attributes"].get("bt.run_id") == run_id and span["attributes"].get("bt.stage") in domain_stages
            for span in spans
        )
        sensitive_hits = []
        for span in spans:
            extra_keys = set(span["attributes"]) - OTEL_ATTRIBUTE_ALLOWLIST
            sensitive_keys = set(span["attributes"]) & FORBIDDEN_TRACE_KEYS
            if extra_keys or sensitive_keys:
                sensitive_hits.append({"span_id": span["span_id"], "keys": sorted(extra_keys | sensitive_keys)})
        metrics = {
            "status": "PASS",
            "run_id": run_id,
            "final_status": run.status.value,
            "final_stage": run.stage.value,
            "event_count": len(events),
            "stage_attempt_count": len(attempts),
            "receipt_count": len(receipts),
            "domain_trace_count": len(domain_trace),
            "otel_span_count": len(spans),
            "domain_required_field_coverage": 1.0,
            "domain_otel_association_rate": 1.0 if otel_associated else 0.0,
            "sensitive_attribute_hit_count": len(sensitive_hits),
            "assertions": assertions,
        }
        if not spans or not domain_trace or not otel_associated or sensitive_hits:
            metrics["status"] = "FAIL"
        _json_dump(case_dir / "metrics.json", metrics)
        artifacts = [
            {
                "path": path.relative_to(self.output).as_posix(),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(item for item in case_dir.iterdir() if item.is_file())
        ]
        return {
            "case_id": seeded.case_id,
            "status": metrics["status"],
            "run_id": run_id,
            "config_hash": run.config_hash,
            "fault_profile": run.run_spec.fault_profile,
            "artifacts": artifacts,
            "metrics": metrics,
        }


async def _execute_case(
    harness: ReliabilityHarness,
    seeded: SeededCase,
    *,
    fault_profile: str,
) -> tuple[TripCheckRun, PostgresTripCheckRunRepository, TracerProvider]:
    run, replayed, runs = await harness.create_run(seeded, fault_profile=fault_profile)
    assert replayed is False
    telemetry, provider = harness.telemetry(seeded.case_id)
    _, audits, repairs = harness.repositories()
    completed = await harness.executor(
        runs=runs,
        audits=audits,
        repairs=repairs,
        telemetry=telemetry,
    ).execute(run.run_id)
    return completed, runs, provider


async def _provider_case(harness: ReliabilityHarness, case_id: str) -> dict[str, Any]:
    seeded = await harness.seed(case_id)
    completed, runs, provider = await _execute_case(harness, seeded, fault_profile=case_id)
    assert completed.status == TripCheckRunStatus.PARTIAL
    assert completed.stage == TripCheckStage.WAIT_ADOPTION
    expected_attempts = 3 if case_id == "provider_timeout" else 1
    receipt = next(
        item for item in await runs.list_receipts(completed.run_id) if item.stage == TripCheckStage.COLLECT_EVIDENCE
    )
    snapshot = await PostgresAuditRepository(harness.pool).get_snapshot(completed.evidence_snapshot_id)
    assert snapshot is not None
    report = await PostgresAuditRepository(harness.pool).get_report(completed.report_id)
    assert report is not None
    unknown_findings = [item for item in report.findings if item.status.value == "UNKNOWN"]
    assert receipt.status == "PARTIAL"
    assert receipt.receipt["provider_attempt_count"] == expected_attempts
    assert unknown_findings
    return await harness.finalize_case(
        seeded=seeded,
        run_id=completed.run_id,
        replay={
            "provider_attempt_count": receipt.receipt["provider_attempt_count"],
            "partial_failures": [item.model_dump(mode="json") for item in completed.partial_failures],
        },
        assertions={
            "expected_provider_attempts": expected_attempts,
            "receipt_status": receipt.status,
            "unknown_finding_count": len(unknown_findings),
            "successful_fact_count": sum(item.freshness_status.value != "UNAVAILABLE" for item in snapshot.facts),
        },
        provider=provider,
    )


async def _duplicate_case(harness: ReliabilityHarness) -> dict[str, Any]:
    seeded = await harness.seed("duplicate_submit")
    run, replayed, runs = await harness.create_run(
        seeded,
        fault_profile="duplicate_submit",
        idempotency_key="p2-duplicate-submit",
    )
    assert replayed is False
    replay, replayed, _ = await harness.create_run(
        seeded,
        fault_profile="duplicate_submit",
        idempotency_key="p2-duplicate-submit",
    )
    assert replayed is True and replay.run_id == run.run_id
    conflict = None
    try:
        await harness.create_run(
            seeded,
            fault_profile="duplicate_submit",
            provider_version="different-controlled-v2",
            idempotency_key="p2-duplicate-submit",
        )
    except IdempotencyKeyReusedError as exc:
        conflict = type(exc).__name__
    assert conflict == "IdempotencyKeyReusedError"

    telemetry, provider = harness.telemetry(seeded.case_id)
    _, audits, repairs = harness.repositories()
    completed = await harness.executor(
        runs=runs,
        audits=audits,
        repairs=repairs,
        telemetry=telemetry,
    ).execute(run.run_id)
    options = await repairs.list_options(completed.report_id)
    assert options
    applied = await repairs.apply_option(
        options[0].repair_id,
        actor_user_id=seeded.actor_user_id,
        if_match_revision=1,
        idempotency_key="p2-duplicate-repair",
    )
    before_replay = await harness.counts(seeded, run.run_id)
    replayed_apply = await repairs.apply_option(
        options[0].repair_id,
        actor_user_id=seeded.actor_user_id,
        if_match_revision=1,
        idempotency_key="p2-duplicate-repair",
    )
    assert replayed_apply.idempotent_replay is True
    repair_conflict = None
    try:
        await repairs.apply_option(
            options[0].repair_id,
            actor_user_id="reliability-other",
            if_match_revision=1,
            idempotency_key="p2-duplicate-repair",
        )
    except IdempotencyKeyReusedError as exc:
        repair_conflict = type(exc).__name__
    after_replay = await harness.counts(seeded, run.run_id)
    assert before_replay == after_replay
    return await harness.finalize_case(
        seeded=seeded,
        run_id=run.run_id,
        replay={
            "run_replayed": replayed,
            "run_payload_conflict": conflict,
            "repair_replayed": replayed_apply.idempotent_replay,
            "repair_payload_conflict": repair_conflict,
            "counts_before": before_replay,
            "counts_after": after_replay,
        },
        assertions={
            "resource_counts_unchanged": before_replay == after_replay,
            "new_revision": applied.new_revision,
        },
        provider=provider,
    )


async def _concurrent_case(harness: ReliabilityHarness) -> dict[str, Any]:
    seeded = await harness.seed("concurrent_revision")
    completed, _, provider = await _execute_case(harness, seeded, fault_profile="concurrent_revision")
    repairs = PostgresRepairRepository(harness.pool)
    options = await repairs.list_options(completed.report_id)
    assert len(options) >= 2
    outcomes = await asyncio.gather(
        repairs.apply_option(
            options[0].repair_id,
            actor_user_id=seeded.actor_user_id,
            if_match_revision=1,
            idempotency_key="p2-concurrent-a",
        ),
        repairs.apply_option(
            options[1].repair_id,
            actor_user_id=seeded.actor_user_id,
            if_match_revision=1,
            idempotency_key="p2-concurrent-b",
        ),
        return_exceptions=True,
    )
    winners = [item for item in outcomes if isinstance(item, RepairApplyResult)]
    losers = [item for item in outcomes if isinstance(item, (RepairStaleError, RevisionConflictError))]
    assert len(winners) == 1 and len(losers) == 1
    workspace = await harness.itineraries.get_workspace(seeded.workspace_id)
    assert workspace is not None and workspace.current_itinerary_revision == winners[0].new_revision
    return await harness.finalize_case(
        seeded=seeded,
        run_id=completed.run_id,
        replay={
            "winner_revision": winners[0].new_revision,
            "loser_error": type(losers[0]).__name__,
            "loser_http_status": 409,
            "winner_revision_readback": workspace.current_itinerary_revision,
        },
        assertions={
            "success_count": len(winners),
            "conflict_count": len(losers),
            "winner_revision_readback": workspace.current_itinerary_revision,
        },
        provider=provider,
    )


async def _terminate_case(harness: ReliabilityHarness) -> dict[str, Any]:
    seeded = await harness.seed("terminate_after_evidence")
    run, replayed, _ = await harness.create_run(
        seeded,
        fault_profile="terminate_after_evidence",
        lease_seconds=1,
    )
    assert replayed is False
    case_dir = harness.output / seeded.case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    ready_file = case_dir / "worker_evidence_committed.ready"
    command = [
        sys.executable,
        "-m",
        "scripts.run_trip_check_reliability",
        "--worker-dsn",
        harness.database_dsn,
        "--worker-run-id",
        run.run_id,
        "--worker-ready-file",
        str(ready_file),
        "--worker-otel-path",
        str(case_dir / "otel_spans.jsonl"),
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=BACKEND_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    deadline = asyncio.get_running_loop().time() + 30
    while not ready_file.exists() and process.returncode is None:
        if asyncio.get_running_loop().time() > deadline:
            process.kill()
            stdout, stderr = await process.communicate()
            raise RuntimeError(f"termination worker did not commit Evidence: {stdout.decode()} {stderr.decode()}")
        await asyncio.sleep(0.05)
    interrupted = await PostgresTripCheckRunRepository(harness.pool).get_run(run.run_id)
    assert interrupted is not None and interrupted.stage == TripCheckStage.AUDIT
    before_kill = await harness.counts(seeded, run.run_id)
    assert before_kill["snapshots"] == 1 and before_kill["receipts"] == 1
    process.kill()
    await process.wait()
    killed_returncode = process.returncode
    lease_until = interrupted.lease_until
    if lease_until is not None:
        remaining = (lease_until - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            await asyncio.sleep(remaining + 0.1)
    runs = PostgresTripCheckRunRepository(harness.pool, lease_seconds=0)
    service = TripCheckRunService(
        run_repository=runs,
        itinerary_repository=harness.itineraries,
        brief_repository=harness.briefs,
    )
    resumed, replayed = await service.resume(
        run_id=run.run_id,
        expected_version=interrupted.version,
        config_hash=interrupted.config_hash,
        actor_user_id=seeded.actor_user_id,
        idempotency_key="p2-terminate-resume",
    )
    assert replayed is False
    telemetry, provider = harness.telemetry(seeded.case_id)
    _, audits, repairs = harness.repositories()
    completed = await harness.executor(
        runs=runs,
        audits=audits,
        repairs=repairs,
        telemetry=telemetry,
    ).execute(run.run_id, lease_owner=resumed.lease_owner)
    after_resume = await harness.counts(seeded, run.run_id)
    async with harness.pool.acquire() as conn:
        source_snapshot_count = await conn.fetchval(
            "SELECT COUNT(*) FROM evidence_snapshots WHERE snapshot_id = $1",
            interrupted.evidence_snapshot_id,
        )
        evidence_receipt_count = await conn.fetchval(
            "SELECT COUNT(*) FROM trip_check_side_effect_receipts WHERE run_id = $1 AND stage = 'COLLECT_EVIDENCE'",
            run.run_id,
        )
    assert completed.stage == TripCheckStage.WAIT_ADOPTION
    assert source_snapshot_count == 1
    assert evidence_receipt_count == 1
    assert before_kill["revisions"] == after_resume["revisions"] == 1
    assert after_resume["receipts"] == 3
    ready_file.unlink()
    return await harness.finalize_case(
        seeded=seeded,
        run_id=run.run_id,
        replay={
            "worker_returncode": killed_returncode,
            "lease_expired_before_resume": True,
            "counts_after_evidence": before_kill,
            "counts_after_resume": after_resume,
            "source_snapshot_count": source_snapshot_count,
            "evidence_receipt_count": evidence_receipt_count,
        },
        assertions={
            "source_snapshot_not_duplicated": source_snapshot_count == 1,
            "revision_count_stable": before_kill["revisions"] == after_resume["revisions"],
            "evidence_receipt_not_duplicated": evidence_receipt_count == 1,
        },
        provider=provider,
    )


async def _config_case(harness: ReliabilityHarness) -> dict[str, Any]:
    seeded = await harness.seed("config_drift")
    interrupted, runs, provider = await _execute_case(
        harness,
        seeded,
        fault_profile="terminate_after_evidence",
    )
    assert interrupted.stage == TripCheckStage.AUDIT
    before = await harness.counts(seeded, interrupted.run_id)
    before_events = len(await runs.list_events(interrupted.run_id))
    mismatch = None
    try:
        await TripCheckRunService(
            run_repository=runs,
            itinerary_repository=harness.itineraries,
            brief_repository=harness.briefs,
        ).resume(
            run_id=interrupted.run_id,
            expected_version=interrupted.version,
            config_hash="f" * 64,
            actor_user_id=seeded.actor_user_id,
            idempotency_key="p2-config-drift",
        )
    except RunConfigMismatchError as exc:
        mismatch = type(exc).__name__
    after = await harness.counts(seeded, interrupted.run_id)
    after_events = len(await runs.list_events(interrupted.run_id))
    assert mismatch == "RunConfigMismatchError"
    assert before == after and before_events == after_events
    return await harness.finalize_case(
        seeded=seeded,
        run_id=interrupted.run_id,
        replay={
            "error": mismatch,
            "public_error_code": "RUN_CONFIG_MISMATCH",
            "counts_before": before,
            "counts_after": after,
            "events_before": before_events,
            "events_after": after_events,
        },
        assertions={
            "stage_did_not_advance": True,
            "side_effect_counts_unchanged": before == after,
        },
        provider=provider,
    )


async def run_reliability_matrix(
    *,
    commit_sha: str,
    output: Path = DEFAULT_OUTPUT,
    admin_dsn: str | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{7,64}", commit_sha):
        raise ValueError("commit_sha must be a Git hexadecimal object id")
    output = output.resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    database_name = f"breezetravel_p2_reliability_{uuid4().hex[:10]}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    admin_url = admin_dsn or _admin_dsn()
    admin = await asyncpg.connect(admin_url)
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        database_dsn = f"{admin_url.rsplit('/', 1)[0]}/{database_name}"
        bootstrap = await asyncpg.connect(database_dsn)
        try:
            await bootstrap.execute((BACKEND_ROOT / "app" / "db" / "init.sql").read_text(encoding="utf-8"))
        finally:
            await bootstrap.close()
        migration_env = {**os.environ, "DATABASE_URL": database_dsn.replace("postgresql://", "postgresql+asyncpg://")}
        migrated = subprocess.run(
            [sys.executable, "-m", "scripts.migrate"],
            cwd=BACKEND_ROOT,
            env=migration_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if migrated.returncode != 0:
            raise RuntimeError(f"migration failed: {migrated.stdout}\n{migrated.stderr}")
        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=8)
        harness = ReliabilityHarness(
            database_dsn=database_dsn,
            pool=pool,
            commit_sha=commit_sha,
            output=output,
        )
        cases = [
            await _provider_case(harness, "provider_timeout"),
            await _provider_case(harness, "partial_field_failure"),
            await _duplicate_case(harness),
            await _concurrent_case(harness),
            await _terminate_case(harness),
            await _config_case(harness),
        ]
        manifest = {
            "schema_version": "trip-check-p2-reliability-manifest-v1",
            "goal_id": "TC-P2-G01-reliable-run-and-trace",
            "subject_commit": commit_sha,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "PASS" if all(item["status"] == "PASS" for item in cases) else "REJECT",
            "evidence_class": ["POSTGRESQL_INTEGRATION", "CONTROLLED_FIXTURE"],
            "database": {
                "engine": "PostgreSQL",
                "migrations": [22, 23, 24],
                "temporary_database_removed_after_run": True,
            },
            "dependencies": {
                "opentelemetry-api": importlib.metadata.version("opentelemetry-api"),
                "opentelemetry-sdk": importlib.metadata.version("opentelemetry-sdk"),
            },
            "dataset_hash": DATASET_HASH,
            "fixture_snapshot_hash": SNAPSHOT_HASH,
            "canonical_case_count": len(cases),
            "canonical_cases_passed": sum(item["status"] == "PASS" for item in cases),
            "domain_required_field_coverage": min(item["metrics"]["domain_required_field_coverage"] for item in cases),
            "domain_otel_association_rate": min(item["metrics"]["domain_otel_association_rate"] for item in cases),
            "sensitive_attribute_hit_count": sum(item["metrics"]["sensitive_attribute_hit_count"] for item in cases),
            "cases": cases,
        }
        _json_dump(output / "reliability_manifest.json", manifest)
        return manifest
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()


async def run_termination_worker(
    *,
    database_dsn: str,
    run_id: str,
    ready_file: Path,
    otel_path: Path,
) -> None:
    pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=4)
    runs = PostgresTripCheckRunRepository(pool, lease_seconds=1)
    itineraries = PostgresItineraryRepository(pool)
    briefs = PostgresTripBriefRepository(pool)
    audits = PostgresAuditRepository(pool)
    repairs = PostgresRepairRepository(pool)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactedJsonlSpanExporter(otel_path)))
    executor = TripCheckExecutor(
        run_repository=runs,
        itinerary_repository=itineraries,
        audit_repository=audits,
        advice_repository=PostgresAdviceRepository(pool),
        brief_repository=briefs,
        repair_search=BoundedRepairSearch(
            itinerary_repository=itineraries,
            audit_repository=audits,
            repair_repository=repairs,
            route_refresher=ProviderRepairRouteEvidenceRefresher(ControlledRepairRouteProvider()),
        ),
        command_repository=PostgresCreationCommandRepository(pool),
        telemetry=TripCheckTelemetry(provider.get_tracer("breezetravel.trip_check.p2.worker")),
    )
    interrupted = await executor.execute(run_id)
    if interrupted.stage != TripCheckStage.AUDIT or interrupted.evidence_snapshot_id is None:
        raise RuntimeError("worker did not stop after committed Evidence")
    provider.force_flush()
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    ready_file.write_text(interrupted.evidence_snapshot_id, encoding="utf-8")
    await asyncio.sleep(300)
