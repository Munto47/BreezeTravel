from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.audit.repositories import InMemoryAuditRepository
from app.constraints.geo_routes import RouteResult
from app.importing.entity_resolver import EntityResolver
from app.importing.models import ImportSourceType, ImportStatus
from app.importing.repositories import InMemoryImportRepository
from app.importing.service import ImportApplicationService
from app.itineraries.hash_service import canonical_json, sha256_canonical
from app.itineraries.models import ResolutionStatus, TripDateRange, TripWorkspace
from app.itineraries.repositories import InMemoryItineraryRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.repairs.objective import introduces_new_high_violation
from app.repairs.repositories import InMemoryRepairRepository
from app.repairs.search import BoundedRepairSearch, ProviderRepairRouteEvidenceRefresher
from app.trip_check.advice import InMemoryAdviceRepository
from app.trip_check.briefs import InMemoryTripBriefRepository, TripBriefApplicationService
from app.trip_check.executor import TripCheckAdoptionReconciler, TripCheckExecutor
from app.trip_check.models import RunBudget, RunSpec, TripCheckRunStatus
from app.trip_check.runs import InMemoryTripCheckRunRepository, TripCheckRunService


BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PILOT_PATH = BACKEND_ROOT / "evals" / "trip_check_v1" / "pilot.jsonl"
FIXTURE_OBSERVED_AT = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
FIXTURE_SNAPSHOT_HASH = hashlib.sha256(b"breezetravel-controlled-fixture-v1").hexdigest()


_PLACE_IDENTITIES: dict[str, tuple[str, str, float, float]] = {
    "故宫博物院": ("bj-forbidden-city", "北京", 116.397, 39.918),
    "故宫": ("bj-forbidden-city", "北京", 116.397, 39.918),
    "天坛公园": ("bj-temple-of-heaven", "北京", 116.417, 39.883),
    "颐和园": ("bj-summer-palace", "北京", 116.273, 39.999),
    "天安门广场": ("bj-tiananmen", "北京", 116.397, 39.903),
    "长城（八达岭）": ("bj-badaling", "北京", 116.017, 40.354),
    "八达岭长城": ("bj-badaling", "北京", 116.017, 40.354),
    "南锣鼓巷": ("bj-nanluoguxiang", "北京", 116.404, 39.937),
    "三里屯太古里": ("bj-sanlitun", "北京", 116.455, 39.933),
    "景山公园": ("bj-jingshan", "北京", 116.396, 39.925),
    "中国国家博物馆": ("bj-national-museum", "北京", 116.407, 39.905),
    "首都博物馆": ("bj-capital-museum", "北京", 116.342, 39.905),
    "外滩": ("sh-bund", "上海", 121.490, 31.241),
    "上海迪士尼乐园": ("sh-disney", "上海", 121.657, 31.144),
    "豫园": ("sh-yuyuan", "上海", 121.493, 31.227),
    "东方明珠广播电视塔": ("sh-oriental-pearl", "上海", 121.500, 31.240),
    "东方明珠": ("sh-oriental-pearl", "上海", 121.500, 31.240),
    "田子坊": ("sh-tianzifang", "上海", 121.475, 31.211),
    "西湖风景名胜区": ("hz-west-lake", "杭州", 120.148, 30.244),
    "西湖": ("hz-west-lake", "杭州", 120.148, 30.244),
    "灵隐寺": ("hz-lingyin", "杭州", 120.102, 30.240),
    "雷峰塔": ("hz-leifeng", "杭州", 120.149, 30.231),
    "西溪湿地国家公园": ("hz-xixi", "杭州", 120.063, 30.268),
    "河坊街·清河坊": ("hz-hefang", "杭州", 120.174, 30.238),
    "龙井村（茶园）": ("hz-longjing", "杭州", 120.101, 30.224),
}


def _candidate(*, query: str, identity: tuple[str, str, float, float], profile: str) -> dict[str, Any]:
    place_id, city, lng, lat = identity
    request = {"query": query, "target_city": city, "fixture_profile": profile}
    response = {"place_id": place_id, "name": query, "city": city, "coords": [lng, lat]}
    return {
        "place_id": place_id,
        "provider_place_id": place_id,
        "name": query,
        "city": city,
        "district": "controlled-fixture",
        "address": "controlled-fixture",
        "category": "attraction",
        "coords": {"lng": lng, "lat": lat},
        "retrieval_provider": "controlled_p1_fixture",
        "execution_mode": "fixture",
        "retrieval_request_hash": sha256_canonical(request),
        "retrieval_response_hash": sha256_canonical(response),
        "retrieval_observed_at": FIXTURE_OBSERVED_AT,
        "opening_hours": "07:00-22:00",
    }


class ControlledPilotCandidateProvider:
    def __init__(self, fixture_profile: str):
        self.fixture_profile = fixture_profile
        self.receipts: list[dict[str, Any]] = []

    async def search(self, *, query: str, city: str) -> list[dict]:
        identity = _PLACE_IDENTITIES.get(query)
        if identity is None:
            return []
        candidate = _candidate(query=query, identity=identity, profile=self.fixture_profile)
        self.receipts.append(
            {
                "query": query,
                "target_city": city,
                "returned_city": identity[1],
                "place_id": identity[0],
                "request_hash": candidate["retrieval_request_hash"],
                "response_hash": candidate["retrieval_response_hash"],
                "execution_mode": "fixture",
            }
        )
        return [candidate]


class ControlledRepairRouteProvider:
    async def fetch(self, *, origin, destination, mode, city):
        del origin, destination, mode, city
        return RouteResult(
            status="ok",
            duration_minutes=20,
            distance_km=3.0,
            transfer_count=None,
            source="controlled_p1_route_fixture",
            response_hash="d" * 64,
            observed_at=FIXTURE_OBSERVED_AT,
        )


def load_cases(path: Path = DEFAULT_PILOT_PATH) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_run_spec(*, commit_sha: str, dataset_hash: str, fault_profile: str = "none") -> RunSpec:
    return RunSpec(
        commit_sha=commit_sha,
        prompt_version="none-p1",
        model_version="none-p1",
        provider_version="controlled-fixture-v1",
        rule_set_version="audit-v1",
        execution_mode="fixture",
        dataset_hash=dataset_hash,
        snapshot_hash=FIXTURE_SNAPSHOT_HASH,
        fault_profile=fault_profile,
        random_seed=7,
        budget=RunBudget(
            max_tokens=0,
            max_provider_queries=0,
            max_retries=1,
            timeout_seconds=30,
            max_cost_usd=0,
        ),
    )


def _reason_codes_from_import(itinerary_import) -> set[str]:
    codes: set[str] = set()
    for resolution in itinerary_import.resolutions:
        if any(item.reason.value == "WRONG_CITY" for item in resolution.rejected_candidates):
            codes.add("PLACE_CITY_MISMATCH")
    return codes


def _wrong_poi_auto_accepts(itinerary_import, target_city: str) -> int:
    count = 0
    for resolution in itinerary_import.resolutions:
        if resolution.resolution_status != ResolutionStatus.AUTO_MATCHED:
            continue
        selected = next(
            (item for item in resolution.candidates if item.place_id == resolution.canonical_place_id),
            None,
        )
        if selected is None or selected.city != target_city or "NAME_EXACT" not in selected.reasons:
            count += 1
    return count


def _semantic_reason_codes(report, snapshot) -> set[str]:
    """Normalize rule-level diagnostics into the frozen pilot taxonomy."""

    codes = {item.reason_code for item in report.findings}
    route_minutes = {
        fact.subject_id: int(fact.value["duration_minutes"])
        for fact in snapshot.facts
        if fact.subject_type == "ROUTE_EDGE"
        and fact.fact_type == "ROUTE_TIME"
        and isinstance(fact.value, dict)
        and isinstance(fact.value.get("duration_minutes"), (int, float))
    }
    for finding in report.findings:
        if finding.reason_code not in {"ROUTE_GAP_INSUFFICIENT", "ROUTE_GAP_TIME_UNKNOWN"}:
            continue
        edge_id = str(finding.input_values.get("edge_id") or "")
        duration = finding.input_values.get("route_duration_minutes")
        if not isinstance(duration, (int, float)):
            duration = route_minutes.get(edge_id)
        if not isinstance(duration, (int, float)):
            continue
        if duration >= 60:
            codes.add("TRAVEL_TIME_GAP")
        elif finding.reason_code == "ROUTE_GAP_INSUFFICIENT":
            codes.add("TIME_CHAIN_CONFLICT")
    return codes


async def execute_case(case: dict[str, Any], *, commit_sha: str, dataset_hash: str) -> dict[str, Any]:
    case_id = case["case_id"]
    workspace_id = f"pilot-{case_id.lower()}"
    actor = "controlled-pilot-runner"
    itinerary_repository = InMemoryItineraryRepository()
    audit_repository = InMemoryAuditRepository(itinerary_repository.workspaces)
    import_repository = InMemoryImportRepository(
        itinerary_repository,
        place_record_store=audit_repository.place_records,
    )
    brief_repository = InMemoryTripBriefRepository()
    command_repository = InMemoryCreationCommandRepository()
    run_repository = InMemoryTripCheckRunRepository(lease_seconds=1)
    advice_repository = InMemoryAdviceRepository()
    repair_repository = InMemoryRepairRepository(itinerary_repository, audit_repository)
    provider = ControlledPilotCandidateProvider(case["fixture_profile"])
    resolver = EntityResolver(
        provider,
        auto_match_threshold=(1.1 if case["fixture_profile"] == "ambiguous_alias_fixture_v1" else 0.90),
    )
    start = date(2026, 10, 1)
    workspace = TripWorkspace(
        workspace_id=workspace_id,
        room_id=f"room-{case_id.lower()}",
        city=case["city"],
        trip_date_range=TripDateRange(
            start=start,
            end=date.fromordinal(start.toordinal() + case["days"] - 1),
        ),
        created_by=actor,
    )
    await itinerary_repository.create_workspace(workspace)
    import_service = ImportApplicationService(
        import_repository=import_repository,
        itinerary_repository=itinerary_repository,
        entity_resolver=resolver,
        trip_brief_repository=brief_repository,
    )
    itinerary_import, _ = await import_service.create_import_idempotent(
        workspace_id=workspace_id,
        source_type=ImportSourceType.MANUAL_TEXT,
        raw_text=case["raw_text"],
        actor_user_id=actor,
        idempotency_key=f"{case_id}:create-import",
        command_repository=command_repository,
    )
    brief = await brief_repository.get_latest_brief(workspace_id)
    if brief is None:
        raise RuntimeError(f"{case_id}: import did not create TripBrief")
    brief, _ = await TripBriefApplicationService(brief_repository).confirm(
        workspace_id=workspace_id,
        revision=brief.revision,
        actor_user_id=actor,
        idempotency_key=f"{case_id}:confirm-brief",
    )
    requires_resolution = itinerary_import.status == ImportStatus.NEEDS_RESOLUTION
    reason_codes = _reason_codes_from_import(itinerary_import)
    wrong_poi_auto_accepts = _wrong_poi_auto_accepts(itinerary_import, case["city"])
    case_artifacts: dict[str, Any] = {
        "import": itinerary_import.model_dump(mode="json"),
        "brief": brief.model_dump(mode="json"),
        "provider_receipts": provider.receipts,
    }
    terminal_state = "NEEDS_USER_RESOLUTION" if requires_resolution else "NOT_STARTED"
    new_high_count = 0
    new_unknown_count = 0
    run_id: str | None = None
    if not requires_resolution:
        applied = await import_service.apply_import(
            itinerary_import.import_id,
            actor_user_id=actor,
            expected_state_version=itinerary_import.state_version,
            idempotency_key=f"{case_id}:apply-import",
        )
        # The production repositories read the same trip_workspaces row. The
        # isolated audit fake keeps this CAS token separately, so bind it to
        # the just-committed import revision before executing the real engine.
        audit_repository.current_revisions[workspace_id] = applied.revision.revision
        run_spec = build_run_spec(commit_sha=commit_sha, dataset_hash=dataset_hash)
        run, _ = await TripCheckRunService(
            run_repository=run_repository,
            itinerary_repository=itinerary_repository,
            brief_repository=brief_repository,
        ).create(
            workspace_id=workspace_id,
            itinerary_revision=applied.revision.revision,
            brief_revision=brief.revision,
            run_spec=run_spec,
            actor_user_id=actor,
            idempotency_key=f"{case_id}:create-run",
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
                route_refresher=ProviderRepairRouteEvidenceRefresher(
                    ControlledRepairRouteProvider()
                ),
            ),
            command_repository=command_repository,
        )
        run = await executor.execute(run.run_id)
        run_id = run.run_id
        if run.report_id:
            source_report = await audit_repository.get_report(run.report_id)
            if source_report is None:
                raise RuntimeError(f"{case_id}: run report disappeared")
            source_snapshot = await audit_repository.get_snapshot(source_report.evidence_snapshot_id)
            if source_snapshot is None:
                raise RuntimeError(f"{case_id}: run Evidence disappeared")
            reason_codes.update(_semantic_reason_codes(source_report, source_snapshot))
            options = await repair_repository.list_options(source_report.report_id)
            for option in options:
                postcheck = await audit_repository.get_report(option.postcheck_report_id)
                if postcheck is None:
                    raise RuntimeError(f"{case_id}: repair postcheck disappeared")
                new_high_count += int(introduces_new_high_violation(source_report, postcheck))
                new_unknown_count += option.new_unknown_count
            bundle = (
                await advice_repository.get_bundle(run.advice_bundle_id)
                if run.advice_bundle_id
                else None
            )
            exposed_repair_id = next(
                (
                    action.repair_id
                    for action in (bundle.actions if bundle is not None else [])
                    if action.repair_id is not None
                ),
                None,
            )
            selected = next(
                (option for option in options if option.repair_id == exposed_repair_id),
                None,
            )
            if selected is not None:
                applied_repair = await repair_repository.apply_option(
                    selected.repair_id,
                    actor_user_id=actor,
                    if_match_revision=selected.base_itinerary_revision,
                    idempotency_key=f"{case_id}:apply-repair",
                )
                reconciled = await TripCheckAdoptionReconciler(
                    run_repository=run_repository,
                    audit_repository=audit_repository,
                    advice_repository=advice_repository,
                ).reconcile(applied_repair)
                if reconciled is None:
                    raise RuntimeError(f"{case_id}: applied repair was not linked to its run")
                run = reconciled
        before_replay = {
            "revision_count": len(itinerary_repository.revisions),
            "snapshot_count": len(audit_repository.snapshots),
            "report_count": len(audit_repository.reports),
            "receipt_count": len(run_repository.receipts),
        }
        replayed_run = await executor.execute(run.run_id)
        after_replay = {
            "revision_count": len(itinerary_repository.revisions),
            "snapshot_count": len(audit_repository.snapshots),
            "report_count": len(audit_repository.reports),
            "receipt_count": len(run_repository.receipts),
        }
        terminal_state = (
            "SUCCEEDED"
            if replayed_run.status == TripCheckRunStatus.SUCCEEDED
            else f"{replayed_run.status.value}:{replayed_run.stage.value}"
        )
        case_artifacts.update(
            {
                "run_spec": run_spec.model_dump(mode="json"),
                "run": replayed_run.model_dump(mode="json"),
                "events": [
                    item.model_dump(mode="json")
                    for item in await run_repository.list_events(run.run_id)
                ],
                "receipts": [
                    item.model_dump(mode="json")
                    for (receipt_run_id, _), item in run_repository.receipts.items()
                    if receipt_run_id == run.run_id
                ],
                "snapshots": [
                    item.model_dump(mode="json")
                    for item in audit_repository.snapshots.values()
                    if item.workspace_id == workspace_id
                ],
                "reports": [
                    item.model_dump(mode="json")
                    for item in audit_repository.reports.values()
                    if item.workspace_id == workspace_id
                ],
                "revisions": [
                    item.model_dump(mode="json")
                    for (item_workspace_id, _), item in itinerary_repository.revisions.items()
                    if item_workspace_id == workspace_id
                ],
                "advice": (
                    await advice_repository.get_bundle(replayed_run.advice_bundle_id)
                ).model_dump(mode="json")
                if replayed_run.advice_bundle_id
                else None,
                "replay": {
                    "before": before_replay,
                    "after": after_replay,
                    "side_effect_counts_equal": before_replay == after_replay,
                },
            }
        )

    expected = case["expected"]
    missing_reason_codes = sorted(set(expected["required_reason_codes"]) - reason_codes)
    passed = all(
        (
            requires_resolution == expected["requires_user_resolution"],
            not missing_reason_codes,
            wrong_poi_auto_accepts <= expected["wrong_poi_auto_accept_max"],
            new_high_count <= expected["repair_new_high_max"],
            new_unknown_count <= expected["repair_new_unknown_max"],
        )
    )
    return {
        "schema_version": "trip-check-pilot-result-v1",
        "case_id": case_id,
        "city": case["city"],
        "fixture_profile": case["fixture_profile"],
        "input_sha256": hashlib.sha256(case["raw_text"].encode("utf-8")).hexdigest(),
        "run_id": run_id,
        "terminal_state": terminal_state,
        "requires_user_resolution": requires_resolution,
        "observed_reason_codes": sorted(reason_codes),
        "missing_reason_codes": missing_reason_codes,
        "wrong_poi_auto_accept_count": wrong_poi_auto_accepts,
        "repair_new_high_count": new_high_count,
        "repair_new_unknown_count": new_unknown_count,
        "passed": passed,
        "artifacts": case_artifacts,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def run_pilot(
    *,
    commit_sha: str,
    dataset_path: Path = DEFAULT_PILOT_PATH,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    dataset_hash = _sha256_file(dataset_path)
    cases = load_cases(dataset_path)
    results = [
        await execute_case(case, commit_sha=commit_sha, dataset_hash=dataset_hash)
        for case in cases
    ]
    city_counts = dict(sorted(Counter(item["city"] for item in results).items()))
    metrics = {
        "case_count": len(results),
        "passed_count": sum(item["passed"] for item in results),
        "failed_count": sum(not item["passed"] for item in results),
        "city_counts": city_counts,
        "resolution_required_count": sum(item["requires_user_resolution"] for item in results),
        "run_count": sum(item["run_id"] is not None for item in results),
        "succeeded_run_count": sum(item["terminal_state"] == "SUCCEEDED" for item in results),
        "wrong_poi_auto_accept_count": sum(item["wrong_poi_auto_accept_count"] for item in results),
        "repair_new_high_count": sum(item["repair_new_high_count"] for item in results),
        "repair_new_unknown_count": sum(item["repair_new_unknown_count"] for item in results),
    }
    report: dict[str, Any] = {
        "schema_version": "trip-check-pilot-execution-v1",
        "status": "PASS" if metrics["failed_count"] == 0 else "REJECT",
        "evidence_class": "CONTROLLED_FIXTURE",
        "live_provider_evidence": False,
        "public_e2e_evidence": False,
        "human_evidence": False,
        "commit_sha": commit_sha,
        "dataset_path": dataset_path.relative_to(BACKEND_ROOT).as_posix(),
        "dataset_sha256": dataset_hash,
        "fixture_snapshot_sha256": FIXTURE_SNAPSHOT_HASH,
        "reason_code_mapping_version": "trip-check-pilot-taxonomy-v1",
        "metrics": metrics,
        "results": [{key: value for key, value in item.items() if key != "artifacts"} for item in results],
    }
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        results_path = output_dir / "results.jsonl"
        results_path.write_text(
            "".join(
                json.dumps({key: value for key, value in item.items() if key != "artifacts"}, ensure_ascii=False)
                + "\n"
                for item in results
            ),
            encoding="utf-8",
        )
        _write_json(output_dir / "metrics.json", metrics)
        for item in results:
            case_dir = output_dir / "runs" / item["case_id"]
            for name, artifact in item["artifacts"].items():
                _write_json(case_dir / f"{name}.json", artifact)
        manifest = {
            **report,
            "results_sha256": _sha256_file(results_path),
            "metrics_sha256": _sha256_file(output_dir / "metrics.json"),
            "artifact_tree_hash": sha256_canonical(
                {
                    item["case_id"]: {
                        name: sha256_canonical(artifact)
                        for name, artifact in item["artifacts"].items()
                    }
                    for item in results
                }
            ),
        }
        _write_json(output_dir / "pilot_manifest.json", manifest)
        report["manifest_path"] = str((output_dir / "pilot_manifest.json").resolve())
        report["manifest_sha256"] = _sha256_file(output_dir / "pilot_manifest.json")
    return report


def result_digest(result: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest()
