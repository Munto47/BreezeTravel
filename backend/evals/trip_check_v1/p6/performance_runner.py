"""Fail-closed P6 G5 local candidate performance evidence runner."""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

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
from app.trip_check.executor import TripCheckExecutor
from app.trip_check.models import RunBudget, RunSpec, TripCheckRunStatus, TripCheckStage
from app.trip_check.provider_integrity import provider_snapshot_sha256
from app.trip_check.runs import InMemoryTripCheckRunRepository, TripCheckRunService
from evals.trip_check_v1.p6.contracts_v1 import (
    P6ContractError,
    digest,
    file_sha256,
    read_actual_repo_state,
    validate_candidate_run_spec,
    validate_gate_receipt,
)
from evals.trip_check_v1.p6.local_browser_runner import _validate_report


PERFORMANCE_THRESHOLDS_MS = {
    "first_feedback_p95_ms": 1_000.0,
    "parse_confirmation_p95_ms": 3_000.0,
    "three_image_ocr_p95_ms": 12_000.0,
    "base_report_p95_ms": 30_000.0,
    "risk_report_p95_ms": 45_000.0,
}
INTERNAL_SAMPLE_COUNT = 20


class _ControlledRouteProvider:
    async def fetch(self, **kwargs: Any) -> RouteResult:
        del kwargs
        return RouteResult(
            status="ok",
            duration_minutes=15,
            distance_km=2.0,
            transfer_count=None,
            source="controlled_trip_check_fixture",
            response_hash="c" * 64,
            observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P6ContractError(reason) from exc
    if not isinstance(value, dict):
        raise P6ContractError(reason)
    return value


def _write_json_new(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except OSError as exc:
        raise P6ContractError("P6_G5_PERFORMANCE_ARTIFACT_WRITE_FAILED") from exc


def _p95(values: list[float]) -> float:
    if not values:
        raise P6ContractError("P6_G5_PERFORMANCE_SAMPLES_MISSING")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _walk_specs(suites: object) -> list[Mapping[str, Any]]:
    if not isinstance(suites, list):
        raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
    specs: list[Mapping[str, Any]] = []
    for suite in suites:
        if not isinstance(suite, Mapping):
            raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
        current = suite.get("specs", [])
        if not isinstance(current, list) or any(not isinstance(item, Mapping) for item in current):
            raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
        specs.extend(current)
        specs.extend(_walk_specs(suite.get("suites", [])))
    return specs


def _browser_performance(report: Mapping[str, Any]) -> tuple[list[float], list[float]]:
    values: dict[str, list[float]] = {
        "p6_first_feedback_ms": [],
        "p6_parse_confirmation_ui_ms": [],
    }
    for spec in _walk_specs(report.get("suites")):
        tests = spec.get("tests")
        if not isinstance(tests, list):
            raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
        for test in tests:
            if not isinstance(test, Mapping):
                raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
            annotations = test.get("annotations")
            if not isinstance(annotations, list):
                raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
            for annotation in annotations:
                if not isinstance(annotation, Mapping):
                    raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
                kind = annotation.get("type")
                if kind not in values:
                    continue
                try:
                    measured = float(annotation["description"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise P6ContractError("P6_G5_BROWSER_PERFORMANCE_INVALID") from exc
                if not math.isfinite(measured) or measured < 0:
                    raise P6ContractError("P6_G5_BROWSER_PERFORMANCE_INVALID")
                values[kind].append(measured)
    if any(len(items) != 3 for items in values.values()):
        raise P6ContractError("P6_G5_BROWSER_PERFORMANCE_SAMPLES_MISSING")
    return values["p6_first_feedback_ms"], values["p6_parse_confirmation_ui_ms"]


def _workspace_bundle() -> tuple[TripWorkspace, ItineraryRevisionContent, ItineraryImport]:
    date_range = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
    revision = with_content_hash(
        ItineraryRevisionContent(
            itinerary_id="p6-performance-itinerary",
            workspace_id="p6-performance-workspace",
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
                            stop_id="p6-performance-stop-1",
                            place_id="p6-performance-place-1",
                            day_index=0,
                            order_index=0,
                            start_time="09:00",
                            end_time="12:00",
                            visit_duration_minutes=180,
                            raw_name="故宫博物院",
                        ),
                        ItineraryStop(
                            stop_id="p6-performance-stop-2",
                            place_id="p6-performance-place-2",
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
                    "p6-performance-stop-1": {"coords": {"lng": 116.397, "lat": 39.918}},
                    "p6-performance-stop-2": {"coords": {"lng": 116.396, "lat": 39.925}},
                }
            },
            created_by="p6-performance-user",
        )
    )
    workspace = TripWorkspace(
        workspace_id=revision.workspace_id,
        room_id="p6-performance-room",
        city="北京",
        trip_date_range=date_range,
        created_by="p6-performance-user",
    )
    itinerary_import = ItineraryImport(
        import_id="p6-performance-import",
        workspace_id=workspace.workspace_id,
        source_type=ImportSourceType.MANUAL_TEXT,
        raw_text="北京2人，第1天09:00-12:00故宫博物院，11:00-13:00景山公园；第2天颐和园",
        parse_version="p6-performance-v1",
        status=ImportStatus.READY,
        created_by="p6-performance-user",
    )
    return workspace, revision, itinerary_import


async def _parse_confirmation_sample() -> float:
    workspace, _, itinerary_import = _workspace_bundle()
    repository = InMemoryTripBriefRepository()
    started = perf_counter()
    draft = TripBriefParser().parse(
        workspace=workspace,
        itinerary_import=itinerary_import,
        actor_user_id="p6-performance-user",
    )
    await repository.save_import_brief(draft)
    confirmed, replayed = await TripBriefApplicationService(repository).confirm(
        workspace_id=workspace.workspace_id,
        revision=draft.revision,
        actor_user_id="p6-performance-user",
        idempotency_key="p6-performance-confirm",
    )
    duration = (perf_counter() - started) * 1000
    if replayed or confirmed.confirmed_at is None:
        raise P6ContractError("P6_G5_PARSE_CONFIRMATION_INVALID")
    return duration


async def _report_sample(*, subject_commit: str, risk: bool) -> float:
    workspace, revision, itinerary_import = _workspace_bundle()
    itineraries = InMemoryItineraryRepository()
    await itineraries.create_workspace(workspace, revision)
    briefs = InMemoryTripBriefRepository()
    draft = TripBriefParser().parse(
        workspace=workspace,
        itinerary_import=itinerary_import,
        actor_user_id="p6-performance-user",
    )
    await briefs.save_import_brief(draft)
    brief, _ = await TripBriefApplicationService(briefs).confirm(
        workspace_id=workspace.workspace_id,
        revision=draft.revision,
        actor_user_id="p6-performance-user",
        idempotency_key="p6-performance-confirm",
    )
    audits = InMemoryAuditRepository(
        itineraries.workspaces,
        place_records={
            workspace.workspace_id: {
                "p6-performance-place-1": {
                    "place_id": "p6-performance-place-1",
                    "name": "故宫博物院",
                    "city": "北京",
                    "opening_hours": "08:00-17:00",
                },
                "p6-performance-place-2": {
                    "place_id": "p6-performance-place-2",
                    "name": "景山公园",
                    "city": "北京",
                    "opening_hours": "06:00-21:00",
                },
            }
        },
    )
    audits.current_revisions[workspace.workspace_id] = 1
    repairs = InMemoryRepairRepository(itineraries, audits)
    runs = InMemoryTripCheckRunRepository(lease_seconds=0)
    advice = InMemoryAdviceRepository()
    run_spec = RunSpec(
        commit_sha=subject_commit,
        prompt_version="none-p6",
        model_version="none-p6",
        provider_version="p3-provider-integrity-v1" if risk else "controlled-fixture-v1",
        rule_set_version="audit-v1",
        execution_mode="snapshot" if risk else "fixture",
        dataset_hash="a" * 64,
        snapshot_hash=provider_snapshot_sha256() if risk else "b" * 64,
        fault_profile="none",
        random_seed=7,
        budget=RunBudget(
            timeout_seconds=45,
            max_retries=0,
            max_provider_queries=6 if risk else 0,
        ),
    )
    created, replayed = await TripCheckRunService(
        run_repository=runs,
        itinerary_repository=itineraries,
        brief_repository=briefs,
    ).create(
        workspace_id=workspace.workspace_id,
        itinerary_revision=1,
        brief_revision=brief.revision,
        run_spec=run_spec,
        actor_user_id="p6-performance-user",
        idempotency_key="p6-performance-create",
    )
    if replayed:
        raise P6ContractError("P6_G5_REPORT_EXECUTION_INVALID")
    executor = TripCheckExecutor(
        run_repository=runs,
        itinerary_repository=itineraries,
        audit_repository=audits,
        advice_repository=advice,
        brief_repository=briefs,
        repair_search=BoundedRepairSearch(
            itinerary_repository=itineraries,
            audit_repository=audits,
            repair_repository=repairs,
            route_refresher=ProviderRepairRouteEvidenceRefresher(_ControlledRouteProvider()),
        ),
        command_repository=InMemoryCreationCommandRepository(),
    )
    started = perf_counter()
    finished = await executor.execute(created.run_id)
    duration = (perf_counter() - started) * 1000
    if (
        finished.stage != TripCheckStage.WAIT_ADOPTION
        or finished.status != TripCheckRunStatus.WAITING
        or not finished.report_id
        or not finished.evidence_snapshot_id
        or (risk and not any(item.stage == TripCheckStage.COLLECT_EVIDENCE for item in runs.receipts.values()))
    ):
        raise P6ContractError("P6_G5_REPORT_EXECUTION_INVALID")
    return duration


async def _default_scenario_runner(subject_commit: str) -> dict[str, list[float]]:
    return {
        "parse_confirmation_engine_ms": [
            await _parse_confirmation_sample() for _ in range(INTERNAL_SAMPLE_COUNT)
        ],
        "base_report_ms": [
            await _report_sample(subject_commit=subject_commit, risk=False)
            for _ in range(INTERNAL_SAMPLE_COUNT)
        ],
        "risk_report_ms": [
            await _report_sample(subject_commit=subject_commit, risk=True)
            for _ in range(INTERNAL_SAMPLE_COUNT)
        ],
    }


async def run_performance_evidence(
    *,
    candidate_run_spec_path: Path,
    browser_report_path: Path,
    g1_receipt_path: Path,
    output_root: Path,
    repo_root: Path,
    formal: bool = True,
    scenario_runner: Callable[[str], Awaitable[dict[str, list[float]]]] = _default_scenario_runner,
) -> dict[str, Any]:
    spec = validate_candidate_run_spec(
        _load_json(candidate_run_spec_path, "P6_CANDIDATE_RUN_SPEC_INVALID")
    )
    repo_resolved = repo_root.resolve(strict=True)
    output_resolved = output_root.resolve(strict=False)
    try:
        output_resolved.relative_to(repo_resolved)
    except ValueError:
        pass
    else:
        raise P6ContractError("P6_G5_PERFORMANCE_EXTERNAL_ROOT_REQUIRED")
    if formal:
        expected_repo = {
            "subject_commit": spec["subject_commit"],
            "upstream_ref": spec["upstream_ref"],
            "upstream_commit": spec["upstream_commit"],
            "dirty_tree": False,
        }
        if read_actual_repo_state(repo_resolved) != expected_repo:
            raise P6ContractError("P6_G5_PERFORMANCE_REPO_BINDING_INVALID")
        if output_resolved != (Path(spec["evidence_root"]) / "g5" / "performance").resolve(strict=False):
            raise P6ContractError("P6_G5_PERFORMANCE_OUTPUT_ROOT_INVALID")
        if browser_report_path.resolve(strict=True) != (
            Path(spec["evidence_root"]) / "g5" / "local" / "playwright-report.json"
        ).resolve(strict=True):
            raise P6ContractError("P6_G5_PERFORMANCE_BROWSER_BINDING_INVALID")
        if g1_receipt_path.resolve(strict=True) != (
            Path(spec["evidence_root"]) / "g1" / "g1_receipt.json"
        ).resolve(strict=True):
            raise P6ContractError("P6_G5_PERFORMANCE_G1_BINDING_INVALID")
        if output_resolved.exists() and any(output_resolved.iterdir()):
            raise P6ContractError("P6_G5_PERFORMANCE_OUTPUT_NOT_EMPTY")
    browser_report = _load_json(browser_report_path, "P6_G5_LOCAL_REPORT_INVALID")
    _validate_report(browser_report, spec["subject_commit"])
    first_feedback, parse_ui = _browser_performance(browser_report)
    g1_receipt = validate_gate_receipt(
        _load_json(g1_receipt_path, "P6_G5_PERFORMANCE_G1_RECEIPT_INVALID"),
        "g1",
        spec,
    )
    g1_metrics = g1_receipt["metrics"]
    if (
        g1_metrics.get("ocr_image_sample_count") != 60
        or g1_metrics.get("three_image_batch_sample_count") != 20
        or not isinstance(g1_metrics.get("three_image_ocr_p95_ms"), (int, float))
        or g1_metrics.get("gpu_runtime_binding_count") != 1
        or g1_metrics.get("gpu_device_count", 0) < 1
        or (
            g1_metrics.get("gpu_compute_capability_major", 0),
            g1_metrics.get("gpu_compute_capability_minor", 0),
        ) < (8, 9)
        or g1_metrics.get("cudnn_version_warning_disclosed_count") != 1
    ):
        raise P6ContractError("P6_G5_PERFORMANCE_OCR_METRICS_INVALID")
    internal = await scenario_runner(spec["subject_commit"])
    required = {"parse_confirmation_engine_ms", "base_report_ms", "risk_report_ms"}
    if set(internal) != required or any(
        not isinstance(values, list)
        or len(values) != INTERNAL_SAMPLE_COUNT
        or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
            for value in values
        )
        for values in internal.values()
    ):
        raise P6ContractError("P6_G5_PERFORMANCE_INTERNAL_SAMPLES_INVALID")
    samples: dict[str, Any] = {
        "schema_version": "trip-check-p6-performance-samples-v1",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "first_feedback_ms": first_feedback,
        "parse_confirmation_ui_ms": parse_ui,
        **internal,
        "three_image_ocr_p95_ms_from_g1": float(g1_metrics["three_image_ocr_p95_ms"]),
        "g1_receipt_hash": g1_receipt["receipt_hash"],
        "browser_report_sha256": file_sha256(browser_report_path),
    }
    samples["sample_set_hash"] = digest(samples)
    metrics = {
        "first_feedback_p95_ms": round(_p95(first_feedback), 3),
        "parse_confirmation_p95_ms": round(
            max(_p95(parse_ui), _p95(internal["parse_confirmation_engine_ms"])),
            3,
        ),
        "three_image_ocr_p95_ms": round(float(g1_metrics["three_image_ocr_p95_ms"]), 3),
        "base_report_p95_ms": round(_p95(internal["base_report_ms"]), 3),
        "risk_report_p95_ms": round(_p95(internal["risk_report_ms"]), 3),
    }
    failures = [
        key for key, threshold in PERFORMANCE_THRESHOLDS_MS.items()
        if metrics[key] > threshold
    ]
    output_resolved.mkdir(parents=True, exist_ok=True)
    _write_json_new(output_resolved / "performance_samples.json", samples)
    receipt: dict[str, Any] = {
        "schema_version": "trip-check-p6-performance-receipt-v1",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS" if not failures else "FAIL",
        "evidence_level": "browser_local",
        "thresholds_ms": PERFORMANCE_THRESHOLDS_MS,
        "metrics": metrics,
        "sample_counts": {
            "first_feedback": len(first_feedback),
            "parse_confirmation_ui": len(parse_ui),
            "parse_confirmation_engine": len(internal["parse_confirmation_engine_ms"]),
            "three_image_ocr_batches": int(g1_metrics["three_image_batch_sample_count"]),
            "base_report": len(internal["base_report_ms"]),
            "risk_report": len(internal["risk_report_ms"]),
        },
        "threshold_failures": failures,
        "performance_threshold_failure_count": len(failures),
        "sample_set_hash": samples["sample_set_hash"],
        "sample_file_sha256": file_sha256(output_resolved / "performance_samples.json"),
        "controlled_snapshot": True,
        "human_evidence": False,
    }
    receipt["receipt_hash"] = digest(receipt)
    _write_json_new(output_resolved / "performance_receipt.json", receipt)
    if failures and formal:
        raise P6ContractError("P6_G5_PERFORMANCE_THRESHOLDS_FAILED")
    return receipt
