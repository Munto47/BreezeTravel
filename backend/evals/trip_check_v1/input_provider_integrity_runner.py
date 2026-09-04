from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.itineraries.hash_service import sha256_canonical, with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
)
from app.trip_check.models import RunBudget, RunSpec, TripCheckRun, TripCheckRunStatus, TripCheckStage
from app.trip_check.provider_integrity import (
    DEFAULT_SNAPSHOT_PATH,
    ROUTE_MODES,
    TripCheckProviderIntegrityCollector,
    provider_snapshot_sha256,
    validate_product_route_observations,
)


BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = BACKEND_ROOT / "evidence" / "trip_check_v1" / "p3" / "provider_integrity"
FIXED_AT = datetime(2026, 8, 23, tzinfo=timezone.utc)
CITIES = ("北京", "上海", "杭州")
COORDINATES = {
    "北京": ((116.397, 39.918), (116.407, 39.928)),
    "上海": ((121.490, 31.241), (121.493, 31.227)),
    "杭州": ((120.148, 30.244), (120.102, 30.240)),
}
FAULT_CASES = (
    ("route_mode_unavailable", "PROVIDER_ROUTE_MODE_UNAVAILABLE"),
    ("weather_unavailable", "PROVIDER_WEATHER_UNAVAILABLE"),
    ("risk_unavailable", "PROVIDER_RISK_UNAVAILABLE"),
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_revision(city: str, *, live: bool = False):
    start = date.today() + timedelta(days=1) if live else date(2026, 9, 1)
    dates = TripDateRange(start=start, end=start + timedelta(days=1))
    left, right = COORDINATES[city]
    return with_content_hash(
        ItineraryRevisionContent(
            itinerary_id=f"p3-{city}-itinerary",
            workspace_id=f"p3-{city}-workspace",
            revision=1,
            source_type=RevisionSource.IMPORT,
            city=city,
            date_range=dates,
            days=[
                ItineraryDay(
                    day_index=0,
                    date=dates.start,
                    stops=[
                        ItineraryStop(
                            stop_id="stop-1",
                            place_id=f"{city}-place-1",
                            day_index=0,
                            order_index=0,
                            raw_name="受控地点一",
                        ),
                        ItineraryStop(
                            stop_id="stop-2",
                            place_id=f"{city}-place-2",
                            day_index=0,
                            order_index=1,
                            raw_name="受控地点二",
                        ),
                    ],
                ),
                ItineraryDay(day_index=1, date=dates.end),
            ],
            change_summary={
                "map_stop_projections": {
                    "stop-1": {"coords": {"lng": left[0], "lat": left[1]}},
                    "stop-2": {"coords": {"lng": right[0], "lat": right[1]}},
                }
            },
            created_by="p3-provider-runner",
            created_at=datetime.now(timezone.utc) if live else FIXED_AT,
        )
    )


def _build_run(
    *,
    city: str,
    commit_sha: str,
    execution_mode: str,
    fault_profile: str,
    live: bool = False,
) -> TripCheckRun:
    dataset_hash = _sha256(DEFAULT_SNAPSHOT_PATH)
    run_spec = RunSpec(
        commit_sha=commit_sha,
        prompt_version="none-p3",
        model_version="none-p3",
        provider_version="p3-provider-integrity-v1",
        rule_set_version="audit-v1",
        execution_mode=execution_mode,
        dataset_hash=dataset_hash,
        snapshot_hash=provider_snapshot_sha256(),
        fault_profile=fault_profile,
        random_seed=7,
        budget=RunBudget(
            max_tokens=0,
            max_provider_queries=6,
            max_retries=0,
            timeout_seconds=30,
            max_cost_usd=0,
        ),
    )
    created_at = datetime.now(timezone.utc) if live else FIXED_AT
    return TripCheckRun(
        run_id=f"p3-{execution_mode}-{city}-{fault_profile}",
        workspace_id=f"p3-{city}-workspace",
        itinerary_revision=1,
        brief_id=f"p3-{city}-brief",
        brief_revision=1,
        stage=TripCheckStage.COLLECT_EVIDENCE,
        run_spec=run_spec,
        config_hash=sha256_canonical(run_spec.model_dump(mode="json")),
        status=TripCheckRunStatus.RUNNING,
        created_by="p3-provider-runner",
        created_at=created_at,
        updated_at=created_at,
    )


def _case_status(
    *,
    result,
    fault_category: str | None,
    network_call_count: int,
) -> tuple[str, list[str]]:
    failures: list[str] = []
    operations = {item.operation for item in result.provider_receipts}
    expected_operations = {
        "route.walking",
        "route.transit",
        "route.bicycling",
        "route.driving",
        "weather.daily",
        "risk.news_search",
    }
    if operations != expected_operations:
        failures.append("PROVIDER_OPERATION_SET_MISMATCH")
    if len(result.provider_receipts) != 6 or result.provider_attempt_count != 6:
        failures.append("PROVIDER_RECEIPT_COUNT_MISMATCH")
    if network_call_count != 0:
        failures.append("SNAPSHOT_NETWORK_CALL_DETECTED")
    if fault_category is None:
        if result.provider_failures or result.partial_failures:
            failures.append("UNEXPECTED_PARTIAL_FAILURE")
        route_modes = {
            str(item.value.get("mode"))
            for item in result.observations
            if item.subject_type == "ROUTE_OPTION"
        }
        if route_modes != set(ROUTE_MODES):
            failures.append("ROUTE_MODE_COVERAGE_MISMATCH")
        if not any(item.fact_type == "WEATHER" for item in result.observations):
            failures.append("WEATHER_FACT_MISSING")
        if not any(item.fact_type == "RISK_SOURCE" for item in result.observations):
            failures.append("RISK_SOURCE_MISSING")
    else:
        categories = {item.error_category for item in result.provider_failures}
        if categories != {fault_category}:
            failures.append("FAULT_CATEGORY_MISMATCH")
        if not any(
            item.freshness_status is not None and item.freshness_status.value == "UNAVAILABLE"
            for item in result.observations
        ):
            failures.append("UNAVAILABLE_FACT_MISSING")
        if not any(
            item.freshness_status is None or item.freshness_status.value != "UNAVAILABLE"
            for item in result.observations
        ):
            failures.append("SUCCESSFUL_FACTS_NOT_PRESERVED")
    return ("PASS" if not failures else "FAIL"), failures


async def run_snapshot_matrix(*, commit_sha: str, output: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    network_calls = 0

    def forbidden_session():
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("snapshot replay attempted to create a network session")

    collector = TripCheckProviderIntegrityCollector(
        settings=Settings(runtime_profile="test"),
        session_factory=forbidden_session,
    )
    cases: list[dict[str, Any]] = []
    definitions = [
        (f"{city}-snapshot", city, "none", None) for city in CITIES
    ] + [
        (f"北京-{profile}", "北京", profile, category) for profile, category in FAULT_CASES
    ]
    for case_id, city, fault_profile, category in definitions:
        before = network_calls
        run = _build_run(
            city=city,
            commit_sha=commit_sha,
            execution_mode="snapshot",
            fault_profile=fault_profile,
        )
        revision = _build_revision(city)
        result = await collector.collect(run, revision, {})
        case_network_calls = network_calls - before
        status, failures = _case_status(
            result=result,
            fault_category=category,
            network_call_count=case_network_calls,
        )
        case_dir = output / "cases" / case_id
        _write_json(case_dir / "run_spec.json", run.run_spec.model_dump(mode="json"))
        _write_json(case_dir / "revision.json", revision.model_dump(mode="json"))
        _write_json(case_dir / "evidence_observations.json", [item.model_dump(mode="json") for item in result.observations])
        _write_json(case_dir / "provider_receipts.json", [item.model_dump(mode="json") for item in result.provider_receipts])
        _write_json(case_dir / "partial_failures.json", [item.model_dump(mode="json") for item in result.partial_failures])
        replay = await collector.collect(run, revision, {})
        replay_equal = replay == result
        case = {
            "case_id": case_id,
            "city": city,
            "fault_profile": fault_profile,
            "status": "PASS" if status == "PASS" and replay_equal else "FAIL",
            "failures": failures + ([] if replay_equal else ["SNAPSHOT_REPLAY_MISMATCH"]),
            "provider_attempt_count": result.provider_attempt_count,
            "receipt_count": len(result.provider_receipts),
            "observation_count": len(result.observations),
            "network_call_count": case_network_calls,
            "replay_equal": replay_equal,
            "result_hash": sha256_canonical(result.model_dump(mode="json")),
        }
        _write_json(case_dir / "case_result.json", case)
        cases.append(case)
    artifacts = [
        {
            "path": path.relative_to(output).as_posix(),
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(item for item in output.rglob("*") if item.is_file())
        if path.name != "provider_integrity_manifest.json"
    ]
    manifest = {
        "schema_version": "trip-check-p3-provider-integrity-manifest-v1",
        "goal_id": "TC-P3-G01-input-provider-integrity",
        "subject_commit": commit_sha,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution_mode": "snapshot",
        "status": "PASS" if all(item["status"] == "PASS" for item in cases) else "FAIL",
        "canonical_case_count": len(cases),
        "canonical_cases_passed": sum(item["status"] == "PASS" for item in cases),
        "cities": list(CITIES),
        "route_modes": list(ROUTE_MODES),
        "snapshot_path": DEFAULT_SNAPSHOT_PATH.relative_to(BACKEND_ROOT).as_posix(),
        "snapshot_sha256": provider_snapshot_sha256(),
        "network_call_count": network_calls,
        "cases": cases,
        "artifact_index": artifacts,
        "non_claims": [
            "Snapshot replay is not live Provider evidence.",
            "Risk fixture sources are controlled structure evidence, not current travel warnings.",
        ],
    }
    _write_json(output / "provider_integrity_manifest.json", manifest)
    return manifest


def live_credentials_ready(settings: Settings | None = None) -> tuple[bool, list[str]]:
    current = settings or get_settings()
    missing: list[str] = []
    if not current.amap_api_key:
        missing.append("AMAP_API_KEY")
    if not (
        current.qweather_api_key
        or (current.qweather_private_key and current.qweather_key_id and current.qweather_project_id)
    ):
        missing.append("QWEATHER_CREDENTIALS")
    return not missing, missing


async def run_live_matrix(
    *,
    commit_sha: str,
    output: Path,
    settings: Settings | None = None,
    max_live_calls: int = 18,
) -> dict[str, Any]:
    if max_live_calls != 18:
        raise ValueError("the P3 live Provider matrix budget is fixed at 18 calls")
    current = settings or get_settings()
    ready, missing = live_credentials_ready(current)
    if not ready:
        manifest = {
            "schema_version": "trip-check-p3-live-provider-manifest-v1",
            "subject_commit": commit_sha,
            "status": "NOT_RUN",
            "reason": "LIVE_PROVIDER_CREDENTIALS_MISSING",
            "missing_credentials": missing,
            "query_budget": 18,
            "actual_network_call_count": 0,
            "actual_receipt_count": 0,
        }
        _write_json(output / "live_provider_manifest.json", manifest)
        return manifest
    collector = TripCheckProviderIntegrityCollector(settings=current, max_live_calls=max_live_calls)
    cases: list[dict[str, Any]] = []
    receipt_count = 0
    for city in CITIES:
        run = _build_run(
            city=city,
            commit_sha=commit_sha,
            execution_mode="live",
            fault_profile="none",
            live=True,
        )
        revision = _build_revision(city, live=True)
        result = await collector.collect(run, revision, {})
        route_failures, route_metrics = validate_product_route_observations(
            result.observations
        )
        receipt_count += len(result.provider_receipts)
        case_dir = output / "live" / city
        _write_json(case_dir / "run_spec.json", run.run_spec.model_dump(mode="json"))
        _write_json(case_dir / "provider_receipts.json", [item.model_dump(mode="json") for item in result.provider_receipts])
        _write_json(case_dir / "evidence_observations.json", [item.model_dump(mode="json") for item in result.observations])
        cases.append(
            {
                "city": city,
                "status": (
                    "PASS"
                    if not result.provider_failures and not route_failures
                    else "FAIL"
                ),
                "receipt_count": len(result.provider_receipts),
                "failure_categories": sorted(
                    {
                        *[item.error_category for item in result.provider_failures],
                        *route_failures,
                    }
                ),
                "route_fact_metrics": route_metrics,
            }
        )
    manifest = {
        "schema_version": "trip-check-p3-live-provider-manifest-v1",
        "subject_commit": commit_sha,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "PASS"
            if receipt_count == 18
            and collector.live_call_count == 18
            and all(item["status"] == "PASS" for item in cases)
            else "FAIL"
        ),
        "query_budget": 18,
        "actual_network_call_count": collector.live_call_count,
        "actual_receipt_count": receipt_count,
        "hidden_retry_count": max(0, collector.live_call_count - receipt_count),
        "cases": cases,
    }
    _write_json(output / "live_provider_manifest.json", manifest)
    return manifest
