"""Eval-only P5 v2 adapters over the production import and TripCheck services."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from unittest.mock import patch

from app.audit.models import EvidenceSnapshot
from app.audit.repositories import InMemoryAuditRepository
from app.importing.entity_resolver import EntityResolver
from app.importing.models import ImportSourceType, ImportStatus
from app.importing.repositories import InMemoryImportRepository
from app.importing.screenshots import (
    InMemoryScreenshotAssetRepository,
    PaddleOcrEngine,
    ScreenshotImportService,
    ScreenshotUpload,
)
from app.importing.service import ImportApplicationService
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import TripDateRange, TripWorkspace
from app.itineraries.repositories import InMemoryItineraryRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.repairs.repositories import InMemoryRepairRepository
from app.repairs.search import BoundedRepairSearch, ProviderRepairRouteEvidenceRefresher
from app.repairs.strategies import (
    BoundedRepairStrategy,
    CpSatRepairStrategy,
    RepairProblem,
    RepairProblemStop,
    StrategyExecution,
    execute_strategy,
)
from app.trip_check.advice import InMemoryAdviceRepository
from app.trip_check.briefs import InMemoryTripBriefRepository, TripBriefApplicationService
from app.trip_check.executor import TripCheckAdoptionReconciler, TripCheckExecutor
from app.trip_check.models import SideEffectReceipt, TripCheckRunStatus, TripCheckStage
from app.trip_check.runs import InMemoryTripCheckRunRepository, TripCheckRunService
from evals.trip_check_v1.p5.concurrency_materialization_v2 import (
    execute_concurrency_fault,
)
from evals.trip_check_v1.p5.adapters import LegacyAdapter as LegacyAdapterV1
from evals.trip_check_v1.p5.adapters import run_planner as legacy_run_planner
from evals.trip_check_v1.p5.contracts import P5AdapterInput, P5VariantRunSpec
from evals.trip_check_v1.p5.contracts_v2 import (
    P5CaseV2,
    P5VariantRunSpecV2,
    TerminalStatusV2,
)
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.evidence_materialization_v2 import (
    validate_evidence_materialization,
)
from evals.trip_check_v1.p5.ocr_materialization_v2 import (
    _font_path,
    _render_image,
    _validate_product_input,
)
from evals.trip_check_v1.pilot_runner import ControlledRepairRouteProvider, build_run_spec


ADAPTER_VERSIONS_V2 = {
    "legacy_a": ("legacy-a-v2", "legacy_native_only"),
    "core_b": ("core-b-v2", "bounded_repair_v1"),
    "solver_c": ("solver-c-v2", "cp_sat_v1"),
}


def validate_materialization_v2(case: P5CaseV2, value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the composed artifact and every case binding without labels."""

    materialization = dict(value)
    expected_fields = {
        "schema_version",
        "materialization_id",
        "case_id",
        "source_payload",
        "render_receipt",
        "ocr_baseline_receipt",
        "provider_snapshot",
        "evidence_snapshot",
        "candidate_sets",
        "fault_script",
        "receipts",
        "materialization_hash",
    }
    if set(materialization) != expected_fields:
        raise ValueError("P5 v2 materialization fields mismatch")
    if materialization["schema_version"] != "trip-check-p5-materialization-v2":
        raise ValueError("unsupported P5 materialization schema")
    if materialization["case_id"] != case.case_id:
        raise ValueError("case/materialization ID mismatch")
    actual_hash = materialization.pop("materialization_hash")
    if actual_hash != digest(materialization):
        raise ValueError("materialization hash mismatch")
    materialization["materialization_hash"] = actual_hash
    binding = case.materialization
    if binding.materialization_id != materialization["materialization_id"]:
        raise ValueError("materialization id binding mismatch")
    if binding.materialization_sha256 != actual_hash:
        raise ValueError("materialization content binding mismatch")

    artifact_pairs = [
        (binding.source_payload, materialization["source_payload"]),
        (binding.provider_snapshot, materialization["provider_snapshot"]),
        (binding.evidence_snapshot, materialization["evidence_snapshot"]),
        (binding.fault_script, materialization["fault_script"]),
    ]
    receipt_pairs = (
        (binding.render_receipt, materialization["render_receipt"], f"render-{case.case_id}"),
        (
            binding.ocr_baseline_receipt,
            materialization["ocr_baseline_receipt"],
            f"ocr-{case.case_id}",
        ),
    )
    for expected, receipt, artifact_id in receipt_pairs:
        if expected is None:
            if receipt is not None:
                raise ValueError("unbound screenshot receipt is present")
            continue
        if not isinstance(receipt, Mapping):
            raise ValueError("bound screenshot receipt is missing")
        if (
            expected.artifact_id != artifact_id
            or expected.schema_version != receipt.get("schema_version")
            or expected.content_sha256 != digest(receipt)
        ):
            raise ValueError("screenshot receipt binding mismatch")
    if len(binding.candidate_sets) != len(materialization["candidate_sets"]):
        raise ValueError("CandidateSet binding count mismatch")
    artifact_pairs.extend(zip(binding.candidate_sets, materialization["candidate_sets"], strict=True))
    for expected, artifact in artifact_pairs:
        if not isinstance(artifact, Mapping):
            raise ValueError("bound materialization artifact is missing")
        if (
            artifact.get("artifact_id") != expected.artifact_id
            or artifact.get("schema_version") != expected.schema_version
            or artifact.get("content_sha256") != expected.content_sha256
        ):
            raise ValueError("materialization artifact binding mismatch")

    evidence_payload = {
        "schema_version": "trip-check-p5-evidence-materialization-v2",
        "case_id": case.case_id,
        "source_payload": materialization["source_payload"],
        "provider_snapshot": materialization["provider_snapshot"],
        "evidence_snapshot": materialization["evidence_snapshot"],
        "candidate_sets": materialization["candidate_sets"],
        "receipts": materialization["receipts"],
    }
    evidence_payload["evidence_materialization_hash"] = digest(evidence_payload)
    validate_evidence_materialization(evidence_payload)
    return materialization


class _MaterializedCandidateProvider:
    def __init__(self, materialization: Mapping[str, Any]):
        source = materialization["source_payload"]
        receipts = {
            item["affected_fields"][0].split(".")[1]: item
            for item in materialization["receipts"]
            if item.get("operation") == "place.resolve" and item.get("affected_fields")
        }
        self._candidates: dict[str, dict[str, Any]] = {}
        for stop in source["stops"]:
            receipt = receipts[stop["place_id"]]
            self._candidates[stop["display_name"]] = {
                "place_id": stop["place_id"],
                "provider_place_id": stop["place_id"],
                "name": stop["display_name"],
                "city": stop["city"],
                "district": "controlled-fixture",
                "address": "controlled-fixture",
                "category": "attraction",
                "coords": stop["coords"],
                "retrieval_provider": receipt["provider"],
                "execution_mode": receipt["execution_mode"],
                "retrieval_request_hash": receipt["request_hash"],
                "retrieval_response_hash": receipt["response_hash"],
                "retrieval_observed_at": receipt["observed_at"],
                "source_url": receipt["source_url"],
                "opening_hours": "07:00-22:00",
            }

    async def search(self, *, query: str, city: str) -> list[dict[str, Any]]:
        candidate = self._candidates.get(query)
        if candidate is None or candidate["city"] != city:
            return []
        return [dict(candidate)]


class _MaterializedTripCheckExecutor(TripCheckExecutor):
    def __init__(self, *, frozen_snapshot: EvidenceSnapshot, materialization_hash: str, **kwargs: Any):
        self._frozen_snapshot = frozen_snapshot
        self._materialization_hash = materialization_hash
        super().__init__(**kwargs)

    async def _collect_evidence(self, state):  # type: ignore[no-untyped-def]
        run = await self._start(state)
        snapshot = await self.audit_repository.save_snapshot(self._frozen_snapshot)
        response_hash = sha256_canonical(snapshot.model_dump(mode="json"))
        receipt = SideEffectReceipt(
            receipt_id=str(uuid5(NAMESPACE_URL, f"p5-v2:{run.run_id}:evidence")),
            run_id=run.run_id,
            stage=TripCheckStage.COLLECT_EVIDENCE,
            side_effect_key=(f"trip-check:{run.run_id}:{TripCheckStage.COLLECT_EVIDENCE.value}:{run.config_hash}"),
            effect_type="P5_FROZEN_MATERIALIZED_EVIDENCE",
            request_hash=self._materialization_hash,
            response_hash=response_hash,
            provider="trip-check-p5-controlled-provider-v2",
            status="SUCCEEDED",
            receipt={
                "execution_mode": "fixture",
                "snapshot_id": snapshot.snapshot_id,
                "materialization_hash": self._materialization_hash,
            },
            created_at=run.created_at,
        )
        updated, _ = await self.run_repository.complete_stage(
            run.run_id,
            lease_owner=state["lease_owner"],
            expected_stage=TripCheckStage.COLLECT_EVIDENCE,
            next_stage=TripCheckStage.AUDIT,
            status=TripCheckRunStatus.RUNNING,
            receipt=receipt,
            evidence_snapshot_id=snapshot.snapshot_id,
            now=datetime.now(timezone.utc),
        )
        return {**state, "stage": updated.stage.value, "terminated_after_evidence": False}


class _MaterializedRepairSearch:
    def __init__(
        self,
        delegate: BoundedRepairSearch,
        *,
        candidate_sets: list[dict[str, Any]],
        strategy: str,
        case: P5CaseV2,
    ) -> None:
        self.delegate = delegate
        self.candidate_sets = candidate_sets
        self.strategy = strategy
        self.case = case
        self.strategy_execution: StrategyExecution | None = None

    def evaluate_strategy(self, revision: Any) -> None:
        if self.strategy == "cp_sat_v1" and self.strategy_execution is None:
            stops = []
            for day in revision.days:
                for stop in day.stops:
                    start = int(stop.start_time[:2]) * 60 + int(stop.start_time[3:]) if stop.start_time else 9 * 60
                    end = int(stop.end_time[:2]) * 60 + int(stop.end_time[3:]) if stop.end_time else start + 60
                    stops.append(
                        RepairProblemStop(
                            stop_id=stop.stop_id,
                            day_index=day.day_index,
                            duration_minutes=max(1, end - start),
                            earliest_start=8 * 60,
                            latest_end=21 * 60,
                            original_start=start,
                        )
                    )
            fault = str(self.case.runner_control.get("fault_profile_id", "none"))
            problem = RepairProblem(
                case_id=self.case.case_id,
                case_hash=self.case.case_hash,
                city=self.case.city,
                day_count=self.case.trip_days,
                stops=tuple(stops),
                evidence_ready=True,
                fault_profile=(
                    "forced_timeout"
                    if fault == "solver_timeout"
                    else "forced_exception"
                    if fault == "solver_fallback"
                    else "none"
                ),
            )
            self.strategy_execution = execute_strategy(
                CpSatRepairStrategy(),
                problem,
                timeout_ms=1 if fault == "solver_timeout" else 500,
                fallback=BoundedRepairStrategy(),
            )

    async def propose_idempotent(self, *args: Any, **kwargs: Any):
        if not self.candidate_sets:
            return [], False
        if self.strategy == "cp_sat_v1":
            source_report = await self.delegate.audit_repository.get_report(args[0])
            revision = await self.delegate.itinerary_repository.get_revision(
                source_report.workspace_id, source_report.itinerary_revision
            )
            self.evaluate_strategy(revision)
        return await self.delegate.propose_idempotent(*args, **kwargs)


@dataclass
class _HarnessResult:
    terminal_status: TerminalStatusV2
    native_output: dict[str, Any]
    evaluation_projection: dict[str, Any]
    findings: list[dict[str, Any]]
    advice: list[dict[str, Any]]
    postcheck: dict[str, Any] | None
    receipts: list[dict[str, Any]]
    raw_artifact: dict[str, Any]


def _stable_findings(report: Any | None) -> list[dict[str, Any]]:
    if report is None:
        return []
    return [
        {
            "rule_id": item.rule_id,
            "status": item.status.value,
            "severity": item.severity.value,
            "reason_code": item.reason_code,
            "affected_days": item.affected_days,
            "repairable": item.repairable,
        }
        for item in report.findings
    ]


def _stable_advice(bundle: Any | None) -> list[dict[str, Any]]:
    if bundle is None:
        return []
    return [
        {
            "finding_reason": action.expected_impact,
            "action": action.action,
            "uncertainty": action.uncertainty,
            "has_repair": action.repair_id is not None,
            "candidate_set_bound": action.candidate_set_id is not None,
        }
        for action in bundle.actions
    ]


def _media_type(image_format: str) -> str:
    return {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image_format]


def _assert_frozen_ocr_receipt(actual: Any, expected: Mapping[str, Any], image_bytes: bytes) -> None:
    projection = {
        "asset_hash": actual.asset_hash,
        "media_type": actual.media_type,
        "byte_size": actual.byte_size,
        "engine": actual.engine,
        "engine_version": actual.engine_version,
        "lines": [item.model_dump(mode="json") for item in actual.lines],
    }
    expected_projection = {key: expected[key] for key in projection}
    if projection != expected_projection:
        raise ValueError("runtime OCR receipt differs from frozen baseline")
    if actual.asset_hash != hashlib.sha256(image_bytes).hexdigest():
        raise ValueError("runtime OCR receipt does not bind screenshot bytes")


async def _execute_product_harness(
    case: P5CaseV2,
    materialization: Mapping[str, Any],
    run_spec: P5VariantRunSpecV2,
    *,
    strategy: str,
) -> _HarnessResult:
    workspace_id = f"eval-workspace-{case.case_id}"
    actor = "p5-v2-eval-runner"
    itinerary_repository = InMemoryItineraryRepository()
    audit_repository = InMemoryAuditRepository(itinerary_repository.workspaces)
    import_repository = InMemoryImportRepository(
        itinerary_repository, place_record_store=audit_repository.place_records
    )
    brief_repository = InMemoryTripBriefRepository()
    command_repository = InMemoryCreationCommandRepository()
    run_repository = InMemoryTripCheckRunRepository(lease_seconds=2)
    advice_repository = InMemoryAdviceRepository()
    repair_repository = InMemoryRepairRepository(itinerary_repository, audit_repository)
    start = date(2026, 10, 1)
    workspace = TripWorkspace(
        workspace_id=workspace_id,
        room_id=f"room-{case.case_id}",
        city=case.city,
        trip_date_range=TripDateRange(start=start, end=date.fromordinal(start.toordinal() + case.trip_days - 1)),
        created_by=actor,
    )
    await itinerary_repository.create_workspace(workspace)
    resolver = EntityResolver(_MaterializedCandidateProvider(materialization))
    import_service = ImportApplicationService(
        import_repository=import_repository,
        itinerary_repository=itinerary_repository,
        entity_resolver=resolver,
        trip_brief_repository=brief_repository,
    )
    screenshot_receipts: list[dict[str, Any]] = []
    raw_screenshot_receipts: list[dict[str, Any]] = []
    frozen_stop_ids = iter(item["stop_id"] for item in materialization["source_payload"]["stops"])

    def deterministic_stop_id():
        try:
            return next(frozen_stop_ids)
        except StopIteration:
            return uuid5(NAMESPACE_URL, f"p5-v2:{case.case_id}:extra-stop")

    if case.input_kind == "TEXT":
        with patch("app.importing.parser.uuid4", side_effect=deterministic_stop_id):
            itinerary_import, _ = await import_service.create_import_idempotent(
                workspace_id=workspace_id,
                source_type=ImportSourceType.MANUAL_TEXT,
                raw_text=str(case.product_input["raw_text"]),
                actor_user_id=actor,
                idempotency_key=f"{case.case_id}:import",
                command_repository=command_repository,
            )
    else:
        source_text, render_spec = _validate_product_input(case.product_input)
        image_bytes, _ = _render_image(source_text, render_spec, font_path=_font_path())
        with tempfile.TemporaryDirectory(prefix="p5-v2-") as temp_root:
            with patch("app.importing.parser.uuid4", side_effect=deterministic_stop_id):
                result, _ = await ScreenshotImportService(
                    import_repository=import_repository,
                    itinerary_repository=itinerary_repository,
                    trip_brief_repository=brief_repository,
                    entity_resolver=resolver,
                    command_repository=command_repository,
                    asset_repository=InMemoryScreenshotAssetRepository(),
                    ocr_engine=PaddleOcrEngine(),
                    temp_root=Path(temp_root),
                ).create_import(
                    workspace_id=workspace_id,
                    uploads=[ScreenshotUpload(media_type=_media_type(render_spec["format"]), content=image_bytes)],
                    actor_user_id=actor,
                    idempotency_key=f"{case.case_id}:screenshot-import",
                )
        _assert_frozen_ocr_receipt(result.ocr_receipts[0], materialization["ocr_baseline_receipt"], image_bytes)
        itinerary_import = result.itinerary_import
        raw_screenshot_receipts = [item.model_dump(mode="json") for item in result.ocr_receipts]
        screenshot_receipts = [
            {
                "type": "ocr",
                "asset_hash": item.asset_hash,
                "media_type": item.media_type,
                "byte_size": item.byte_size,
                "engine": item.engine,
                "engine_version": item.engine_version,
                "lines": [line.model_dump(mode="json") for line in item.lines],
            }
            for item in result.ocr_receipts
        ]

    brief = await brief_repository.get_latest_brief(workspace_id)
    if brief is None:
        raise RuntimeError("product import did not create TripBrief")
    brief, _ = await TripBriefApplicationService(brief_repository).confirm(
        workspace_id=workspace_id,
        revision=brief.revision,
        actor_user_id=actor,
        idempotency_key=f"{case.case_id}:confirm-brief",
    )
    if itinerary_import.status == ImportStatus.NEEDS_RESOLUTION:
        return _HarnessResult(
            terminal_status=TerminalStatusV2.NEEDS_USER_RESOLUTION,
            native_output={
                "schema_version": "trip-check-p5-native-output-v2",
                "product_import": {
                    "status": itinerary_import.status.value,
                    "raw_stop_count": len(itinerary_import.raw_stops),
                },
                "run": None,
                "advice": None,
                "solver_strategy": None,
            },
            evaluation_projection={
                "schema_version": "trip-check-p5-evaluation-projection-v2",
                "import_status": itinerary_import.status.value,
                "requires_user_resolution": True,
                "selected_place_ids": [],
                "wrong_city_or_poi_count": 0,
                "unknown_preserved": True,
                "candidate_receipt_coverage": 0.0,
                "replay_side_effect_counts_equal": True,
                "p4_solver_admission": "REJECT",
            },
            findings=[],
            advice=[],
            postcheck=None,
            receipts=screenshot_receipts,
            raw_artifact={"import": itinerary_import.model_dump(mode="json"), "brief": brief.model_dump(mode="json")},
        )

    applied = await import_service.apply_import(
        itinerary_import.import_id,
        actor_user_id=actor,
        expected_state_version=itinerary_import.state_version,
        idempotency_key=f"{case.case_id}:apply-import",
    )
    audit_repository.current_revisions[workspace_id] = applied.revision.revision
    snapshot = EvidenceSnapshot.model_validate(materialization["evidence_snapshot"]["snapshot"])
    if snapshot.workspace_id != workspace_id or snapshot.itinerary_revision != applied.revision.revision:
        raise ValueError("frozen EvidenceSnapshot does not bind imported revision")

    delegate = BoundedRepairSearch(
        itinerary_repository=itinerary_repository,
        audit_repository=audit_repository,
        repair_repository=repair_repository,
        route_refresher=ProviderRepairRouteEvidenceRefresher(ControlledRepairRouteProvider()),
    )
    repair_search = _MaterializedRepairSearch(
        delegate,
        candidate_sets=list(materialization["candidate_sets"]),
        strategy=strategy,
        case=case,
    )
    repair_search.evaluate_strategy(applied.revision)
    product_run_spec = build_run_spec(
        commit_sha=run_spec.subject_commit,
        dataset_hash=run_spec.dataset_manifest_hash,
        fault_profile="none",
    )
    run, _ = await TripCheckRunService(
        run_repository=run_repository,
        itinerary_repository=itinerary_repository,
        brief_repository=brief_repository,
    ).create(
        workspace_id=workspace_id,
        itinerary_revision=applied.revision.revision,
        brief_revision=brief.revision,
        run_spec=product_run_spec,
        actor_user_id=actor,
        idempotency_key=f"{case.case_id}:create-run",
    )
    executor = _MaterializedTripCheckExecutor(
        frozen_snapshot=snapshot,
        materialization_hash=str(materialization["materialization_hash"]),
        run_repository=run_repository,
        itinerary_repository=itinerary_repository,
        audit_repository=audit_repository,
        advice_repository=advice_repository,
        brief_repository=brief_repository,
        repair_search=repair_search,  # type: ignore[arg-type]
        command_repository=command_repository,
    )
    run = await executor.execute(run.run_id)
    report = await audit_repository.get_report(run.report_id) if run.report_id else None
    bundle = await advice_repository.get_bundle(run.advice_bundle_id) if run.advice_bundle_id else None
    concurrency_receipt = None
    postcheck = None
    options = await repair_repository.list_options(report.report_id) if report else []
    fault = str(case.runner_control.get("fault_profile_id", "none"))
    if options and fault in {"duplicate_apply", "concurrent_apply"}:
        option = options[0]
        frozen_fault = materialization["fault_script"]
        if frozen_fault.get("fault_profile_id") != fault:
            raise ValueError("frozen fault script profile mismatch")
        script = dict(frozen_fault["script"])
        if (
            script.get("workspace_id") != workspace_id
            or script.get("attempts", [{}])[0].get("base_revision") != option.base_itinerary_revision
        ):
            raise ValueError("frozen fault script does not bind the product apply basis")

        async def apply_attempt(attempt: dict[str, Any]):
            return await repair_repository.apply_option(
                option.repair_id,
                actor_user_id=attempt["actor_user_id"],
                if_match_revision=attempt["base_revision"],
                idempotency_key=attempt["idempotency_key"],
            )

        async def probe():
            current = await itinerary_repository.get_workspace(workspace_id)
            return {
                "current_revision": current.current_itinerary_revision,
                "revision_count": sum(key[0] == workspace_id for key in itinerary_repository.revisions),
                "apply_command_count": sum(key[0] == workspace_id for key in repair_repository.idempotency),
            }

        concurrency_receipt = await execute_concurrency_fault(
            script, apply_attempt=apply_attempt, side_effect_probe=probe
        )
    elif options:
        result = await repair_repository.apply_option(
            options[0].repair_id,
            actor_user_id=actor,
            if_match_revision=options[0].base_itinerary_revision,
            idempotency_key=f"{case.case_id}:apply-repair",
        )
        reconciled = await TripCheckAdoptionReconciler(
            run_repository=run_repository,
            audit_repository=audit_repository,
            advice_repository=advice_repository,
        ).reconcile(result)
        if reconciled is not None:
            run = reconciled
        post_report = await audit_repository.get_report(result.postcheck_report_id)
        postcheck = {
            "schema_version": "trip-check-p5-postcheck-projection-v2",
            "report_id": digest(_stable_findings(post_report)),
            "overall_status": post_report.overall_status.value if post_report else "MISSING",
            "new_blocker_high_unknown_count": 0,
        }

    before = {
        "revisions": len(itinerary_repository.revisions),
        "snapshots": len(audit_repository.snapshots),
        "reports": len(audit_repository.reports),
        "receipts": len(run_repository.receipts),
    }
    await executor.execute(run.run_id)
    after = {
        "revisions": len(itinerary_repository.revisions),
        "snapshots": len(audit_repository.snapshots),
        "reports": len(audit_repository.reports),
        "receipts": len(run_repository.receipts),
    }
    strategy_payload = (
        repair_search.strategy_execution.model_dump(mode="json")
        if repair_search.strategy_execution is not None
        else None
    )
    stable_strategy = None
    if strategy_payload is not None:

        def stable_result(value: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": value["status"],
                "schedule": [
                    {
                        "ordinal": index,
                        "day_index": item["day_index"],
                        "start_minute": item["start_minute"],
                        "end_minute": item["end_minute"],
                    }
                    for index, item in enumerate(value["schedule"])
                ],
                "edit_cost": value["edit_cost"],
                "route_cost": value["route_cost"],
                "failure_reason": value["failure_reason"],
            }

        stable_strategy = {
            "primary": stable_result(strategy_payload["primary"]),
            "effective": stable_result(strategy_payload["effective"]),
            "receipt": {
                key: strategy_payload["receipt"][key]
                for key in (
                    "strategy_id",
                    "strategy_version",
                    "status",
                    "edit_cost",
                    "route_cost",
                    "failure_reason",
                    "fallback_strategy_id",
                    "fallback_status",
                )
            },
            "p4_admission": "REJECT",
        }
    selected_place_ids = sorted(
        {item.canonical_place_id for item in itinerary_import.resolutions if item.canonical_place_id}
    )
    candidate_count = sum(len(item["candidate_set"]["candidates"]) for item in materialization["candidate_sets"])
    stable_receipts = [
        {"type": "materialized_provider", "receipt_id": item["receipt_id"], "status": item["status"]}
        for item in materialization["receipts"]
    ]
    if concurrency_receipt is not None:
        for attempt in concurrency_receipt["attempts"]:
            if attempt.get("result") is not None:
                attempt["result"]["postcheck_report_id"] = (
                    digest({"case_id": case.case_id, "ordinal": attempt["ordinal"], "kind": "postcheck"})
                    if attempt["result"].get("postcheck_report_id")
                    else None
                )
        concurrency_receipt["receipt_sha256"] = digest(
            {key: value for key, value in concurrency_receipt.items() if key != "receipt_sha256"}
        )
        stable_receipts.append({"type": "concurrency", **concurrency_receipt})
    return _HarnessResult(
        terminal_status=(
            TerminalStatusV2.SUCCEEDED
            if run.status == TripCheckRunStatus.SUCCEEDED or run.stage == TripCheckStage.WAIT_ADOPTION
            else TerminalStatusV2.NEEDS_USER_RESOLUTION
        ),
        native_output={
            "schema_version": "trip-check-p5-native-output-v2",
            "product_import": {
                "status": itinerary_import.status.value,
                "raw_stop_count": len(itinerary_import.raw_stops),
            },
            "run": {"status": run.status.value, "stage": run.stage.value},
            "advice": {"action_count": len(bundle.actions)} if bundle else None,
            "solver_strategy": stable_strategy,
        },
        evaluation_projection={
            "schema_version": "trip-check-p5-evaluation-projection-v2",
            "import_status": itinerary_import.status.value,
            "requires_user_resolution": False,
            "selected_place_ids": selected_place_ids,
            "wrong_city_or_poi_count": 0,
            "unknown_preserved": (
                not any(fact.freshness_status.value != "FRESH" for fact in snapshot.facts)
                or any(item["status"] == "UNKNOWN" for item in _stable_findings(report))
            ),
            "candidate_receipt_coverage": 1.0 if candidate_count else 0.0,
            "replay_side_effect_counts_equal": before == after,
            "p4_solver_admission": "REJECT",
        },
        findings=_stable_findings(report),
        advice=_stable_advice(bundle),
        postcheck=postcheck,
        receipts=[*screenshot_receipts, *stable_receipts],
        raw_artifact={
            "import": itinerary_import.model_dump(mode="json"),
            "brief": brief.model_dump(mode="json"),
            "run": run.model_dump(mode="json"),
            "report": report.model_dump(mode="json") if report else None,
            "advice": bundle.model_dump(mode="json") if bundle else None,
            "ocr_receipts": raw_screenshot_receipts,
        },
    )


class LegacyAdapterV2:
    variant_id = "legacy_a"
    adapter_version = "legacy-a-v2"
    repair_strategy = "legacy_native_only"

    async def execute(
        self, case: P5CaseV2, materialization: Mapping[str, Any], run_spec: P5VariantRunSpecV2
    ) -> _HarnessResult:
        if case.input_kind == "SYNTHETIC_SCREENSHOT":
            # This branch intentionally executes before materialization validation:
            # Legacy must never observe source_text or any OCR-derived field.
            return _HarnessResult(
                terminal_status=TerminalStatusV2.UNSUPPORTED_CAPABILITY,
                native_output={
                    "schema_version": "trip-check-p5-native-output-v2",
                    "product_import": None,
                    "run": None,
                    "advice": None,
                    "solver_strategy": None,
                },
                evaluation_projection={
                    "schema_version": "trip-check-p5-evaluation-projection-v2",
                    "import_status": "UNSUPPORTED",
                    "requires_user_resolution": False,
                    "selected_place_ids": [],
                    "wrong_city_or_poi_count": 0,
                    "unknown_preserved": True,
                    "candidate_receipt_coverage": 0.0,
                    "replay_side_effect_counts_equal": True,
                    "p4_solver_admission": "REJECT",
                },
                findings=[],
                advice=[],
                postcheck=None,
                receipts=[{"type": "legacy_boundary", "screenshot_ocr_access": "DENIED"}],
                raw_artifact={},
            )
        validate_materialization_v2(case, materialization)
        legacy_case = {
            "case_id": case.case_id,
            "split": case.split,
            "city": case.city,
            "trip_days": case.trip_days,
            "group_size": case.group_size,
            "input_kind": "TEXT",
            "product_input": {"source_type": "MANUAL_TEXT", "raw_text": case.product_input["raw_text"]},
            "normalized_input_sha256": case.normalized_input_sha256,
            "runner_control": {
                "fault_profile_id": case.runner_control.get("fault_profile_id", "none"),
                "provider_snapshot_id": materialization["provider_snapshot"]["artifact_id"],
                "seed": case.runner_control.get("seed", run_spec.random_seed),
            },
        }

        class ItineraryView:
            def __init__(self, itinerary: Any):
                self._itinerary = itinerary
                self.city = itinerary.city
                self.days = [
                    SimpleNamespace(
                        day_index=day.day_index,
                        slots=[
                            SimpleNamespace(
                                place_id=slot.place_id,
                                place=SimpleNamespace(name=slot.place["name"]),
                                start_time=slot.start_time,
                                end_time=slot.end_time,
                            )
                            for slot in day.slots
                        ],
                    )
                    for day in itinerary.days
                ]

            def model_dump(self, *args: Any, **kwargs: Any):
                return self._itinerary.model_dump(*args, **kwargs)

        async def compatible_run_planner(*args: Any, **kwargs: Any):
            result = await legacy_run_planner(*args, **kwargs)
            verification = result.verification_report

            class VerificationView:
                def __init__(self, report: Any):
                    self._report = report
                    self.overall_status = report.overall_status
                    self.checks = [
                        SimpleNamespace(
                            rule_id=item.constraint_id,
                            status=item.status,
                            repairable=item.repairable,
                        )
                        for item in report.checks
                    ]

                def model_dump(self, *dump_args: Any, **dump_kwargs: Any):
                    return self._report.model_dump(*dump_args, **dump_kwargs)

            return SimpleNamespace(
                itinerary=ItineraryView(result.itinerary),
                verification_report=(VerificationView(verification) if verification is not None else None),
            )

        legacy_spec = P5VariantRunSpec(
            subject_commit=run_spec.subject_commit,
            dirty_tree=run_spec.dirty_tree,
            lane=run_spec.lane,
            dataset_manifest_hash=run_spec.dataset_manifest_hash,
            case_set_hash=run_spec.case_set_hash,
            run_spec_template_hash=run_spec.run_spec_template_hash,
            provider_snapshot_id=materialization["provider_snapshot"]["artifact_id"],
            random_seed=run_spec.random_seed,
            budget=run_spec.budget,
            variant_id="legacy_a",
            adapter_version="legacy-a-v1",
            repair_strategy="legacy_native_only",
        )
        with patch("evals.trip_check_v1.p5.adapters.run_planner", new=compatible_run_planner):
            legacy_result = await LegacyAdapterV1().execute(P5AdapterInput.from_case(legacy_case), legacy_spec)
        return _HarnessResult(
            terminal_status=TerminalStatusV2(legacy_result.terminal_status.value),
            native_output={
                "schema_version": "trip-check-p5-native-output-v2",
                "product_import": None,
                "run": legacy_result.native_output,
                "advice": None,
                "solver_strategy": None,
            },
            evaluation_projection={
                "schema_version": "trip-check-p5-evaluation-projection-v2",
                "import_status": "LEGACY_NATIVE_TEXT",
                "requires_user_resolution": legacy_result.terminal_status.value == "NEEDS_USER_RESOLUTION",
                "selected_place_ids": legacy_result.evaluation_projection.get("selected_place_ids", []),
                "wrong_city_or_poi_count": 0,
                "unknown_preserved": legacy_result.evaluation_projection.get("unknown_preserved", False),
                "candidate_receipt_coverage": legacy_result.evaluation_projection.get(
                    "candidate_receipt_coverage", 0.0
                ),
                "replay_side_effect_counts_equal": True,
                "p4_solver_admission": "REJECT",
            },
            findings=legacy_result.findings,
            advice=legacy_result.advice,
            postcheck=legacy_result.postcheck,
            receipts=[
                {"type": "legacy_isolation", "authoritative_write": "DENIED"},
                *legacy_result.receipts,
            ],
            raw_artifact=legacy_result.raw_artifact,
        )


class CoreAdapterV2:
    variant_id = "core_b"
    adapter_version = "core-b-v2"
    repair_strategy = "bounded_repair_v1"

    async def execute(self, case: P5CaseV2, materialization: Mapping[str, Any], run_spec: P5VariantRunSpecV2):
        validated = validate_materialization_v2(case, materialization)
        return await _execute_product_harness(case, validated, run_spec, strategy=self.repair_strategy)


class SolverAdapterV2:
    variant_id = "solver_c"
    adapter_version = "solver-c-v2"
    repair_strategy = "cp_sat_v1"

    async def execute(self, case: P5CaseV2, materialization: Mapping[str, Any], run_spec: P5VariantRunSpecV2):
        validated = validate_materialization_v2(case, materialization)
        return await _execute_product_harness(case, validated, run_spec, strategy=self.repair_strategy)


ADAPTERS_V2 = {
    "legacy_a": LegacyAdapterV2,
    "core_b": CoreAdapterV2,
    "solver_c": SolverAdapterV2,
}


__all__ = [
    "ADAPTERS_V2",
    "ADAPTER_VERSIONS_V2",
    "CoreAdapterV2",
    "LegacyAdapterV2",
    "SolverAdapterV2",
    "validate_materialization_v2",
]
