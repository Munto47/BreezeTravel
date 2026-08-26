"""Run one live Beijing smoke through the application's concrete adapters.

The output is a local-authorized technical receipt.  It is not public E2E,
human validation, or release approval.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.agents.planner.nodes import weather_fetcher
from app.config import settings
from app.importing.entity_resolver import AmapEntityCandidateProvider
from app.itineraries.route_refresh import AmapRouteEvidenceProvider
from app.schemas.place import Coordinates


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO_ROOT / "backend" / "evidence" / "real_provider_local_authorized" / "adapter_smoke.json"
DEFAULT_SUMMARY = REPO_ROOT / "backend" / "evidence" / "real_provider_local_authorized" / "summary.json"
EVIDENCE_CLASS = "real_provider_local_authorized"


def _hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _timed(call: Callable[[], Awaitable[Any]]) -> tuple[Any, float, str, Exception | None]:
    started = time.perf_counter()
    try:
        result = await call()
        return result, round((time.perf_counter() - started) * 1000, 2), _now(), None
    except Exception as exc:  # Adapter smoke must retain a receipt on failure.
        return None, round((time.perf_counter() - started) * 1000, 2), _now(), exc


async def _entity_smoke() -> dict[str, Any]:
    request = {
        "adapter": "app.importing.entity_resolver.AmapEntityCandidateProvider.search",
        "query": "故宫博物院",
        "city": "北京",
    }
    provider = AmapEntityCandidateProvider()
    result, latency, completed_at, error = await _timed(
        lambda: provider.search(query=request["query"], city=request["city"]),
    )
    candidates = result or []
    first = candidates[0] if candidates else {}
    is_live = str(first.get("execution_mode") or "").lower() == "live"
    response_hash = first.get("retrieval_response_hash")
    status = "ok" if candidates and is_live and response_hash else "error"
    return {
        "case_id": "adapter_amap_entity_beijing",
        "adapter_path": request["adapter"],
        "status": status,
        "request_hash": _hash(request),
        "response_hash": response_hash or (_hash(candidates) if candidates else None),
        "response_hash_scope": "raw_provider_payload" if response_hash else "normalized_adapter_output",
        "observed_at": first.get("retrieval_observed_at") or completed_at,
        "latency_ms": latency,
        "result_count": len(candidates),
        "error_category": type(error).__name__ if error else (None if status == "ok" else "not_live_or_empty"),
        "sample_result": {
            "name": first.get("name"),
            "provider_place_id": first.get("place_id"),
            "execution_mode": first.get("execution_mode"),
            "retrieval_provider": first.get("retrieval_provider"),
        },
    }


async def _route_smoke() -> dict[str, Any]:
    request = {
        "adapter": "app.itineraries.route_refresh.AmapRouteEvidenceProvider.fetch",
        "city": "北京",
        "mode": "driving",
        "origin": {"label": "故宫博物院", "lng": 116.397029, "lat": 39.918058},
        "destination": {"label": "天坛公园", "lng": 116.417312, "lat": 39.887977},
    }
    provider = AmapRouteEvidenceProvider()
    result, latency, completed_at, error = await _timed(
        lambda: provider.fetch(
            origin=Coordinates(**{key: request["origin"][key] for key in ("lng", "lat")}),
            destination=Coordinates(**{key: request["destination"][key] for key in ("lng", "lat")}),
            mode=request["mode"],
            city=request["city"],
        ),
    )
    ok = result is not None and result.status == "ok" and bool(result.response_hash)
    return {
        "case_id": "adapter_amap_route_beijing",
        "adapter_path": request["adapter"],
        "status": "ok" if ok else "error",
        "request_hash": _hash(request),
        "response_hash": result.response_hash if result else None,
        "response_hash_scope": "raw_provider_payload",
        "observed_at": result.observed_at.isoformat().replace("+00:00", "Z") if result and result.observed_at else completed_at,
        "latency_ms": latency,
        "result_count": 1 if ok else 0,
        "error_category": type(error).__name__ if error else (result.failure_reason if result and not ok else None),
        "sample_result": {
            "source": result.source if result else None,
            "duration_minutes": result.duration_minutes if result else None,
            "distance_km": result.distance_km if result else None,
        },
    }


async def _weather_smoke() -> dict[str, Any]:
    request = {
        "adapter": "app.agents.planner.nodes.weather_fetcher.run",
        "city": "北京",
        "start_date": date.today().isoformat(),
        "trip_days": 1,
        "center_lat": 39.918058,
        "center_lng": 116.397029,
        "provider_host": settings.qweather_api_host,
        "auth_type": settings.qweather_auth_type,
    }
    state = {
        "start_date": request["start_date"],
        "trip_days": request["trip_days"],
        "center_lat": request["center_lat"],
        "center_lng": request["center_lng"],
        "trace": [],
    }
    result, latency, completed_at, error = await _timed(lambda: weather_fetcher.run(state))
    forecast = (result or {}).get("weather_forecast") or {}
    normalized = {
        str(index): value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        for index, value in forecast.items()
    }
    trace = list((result or {}).get("trace") or [])
    ok = bool(normalized) and any("获取到" in line for line in trace)
    first = normalized.get("0") or (next(iter(normalized.values())) if normalized else {})
    return {
        "case_id": "adapter_qweather_planner_beijing",
        "adapter_path": request["adapter"],
        "status": "ok" if ok else "error",
        "request_hash": _hash(request),
        "response_hash": _hash(normalized) if normalized else None,
        "response_hash_scope": "normalized_adapter_output",
        "observed_at": completed_at,
        "latency_ms": latency,
        "result_count": len(normalized),
        "error_category": type(error).__name__ if error else (None if ok else "provider_output_empty"),
        "sample_result": {
            "date": first.get("date"),
            "condition": first.get("condition"),
            "precip_mm": first.get("precip_mm"),
            "trace": trace,
        },
    }


def _preflight() -> list[str]:
    errors = []
    if settings.runtime_profile != "local_real":
        errors.append("runtime_profile_not_local_real")
    if settings.amap_mock is not False:
        errors.append("amap_mock_not_false")
    if settings.demo_mode is not False:
        errors.append("demo_mode_not_false")
    if not settings.amap_api_key:
        errors.append("amap_credentials_missing")
    if not weather_fetcher._has_qweather_credentials():
        errors.append("qweather_credentials_missing")
    return errors


async def collect() -> dict[str, Any]:
    preflight_errors = _preflight()
    if preflight_errors:
        raise RuntimeError(",".join(preflight_errors))
    started_at = _now()
    samples = [await _entity_smoke(), await _route_smoke(), await _weather_smoke()]
    validation_errors = [
        f"{sample['case_id']}:{sample.get('error_category') or 'failed'}"
        for sample in samples
        if sample["status"] != "ok"
    ]
    return {
        "schema_version": "1.0",
        "evidence_class": EVIDENCE_CLASS,
        "evidence_subtype": "application_adapter_smoke",
        "claim_boundary": {
            "is_real_provider": True,
            "is_local_authorized": True,
            "is_public_internet_e2e": False,
            "is_human_evidence": False,
            "is_release_approval": False,
        },
        "runtime": {
            "runtime_profile": settings.runtime_profile,
            "amap_mock": settings.amap_mock,
            "demo_mode": settings.demo_mode,
            "qweather_auth_type": settings.qweather_auth_type,
            "qweather_custom_host_used": settings.qweather_api_host != "devapi.qweather.com",
            "authentication_material_persisted": False,
        },
        "run_started_at": started_at,
        "run_completed_at": _now(),
        "overall_status": "passed" if not validation_errors else "failed",
        "validation_errors": validation_errors,
        "samples": samples,
    }


def _bind_summary(summary_path: Path, adapter_path: Path, report: dict[str, Any]) -> None:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["adapter_smoke"] = {
        "evidence_file": adapter_path.relative_to(REPO_ROOT).as_posix(),
        "sha256": _file_hash(adapter_path),
        "overall_status": report["overall_status"],
        "run_completed_at": report["run_completed_at"],
        "claim_boundary": report["claim_boundary"],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output if args.output.is_absolute() else REPO_ROOT / args.output
    summary = args.summary if args.summary.is_absolute() else REPO_ROOT / args.summary
    report = asyncio.run(collect())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _bind_summary(summary, output, report)
    for sample in report["samples"]:
        print(
            f"{sample['case_id']}: status={sample['status']} "
            f"latency_ms={sample['latency_ms']} result_count={sample['result_count']}"
        )
    print(f"overall_status={report['overall_status']}")
    print(f"evidence_file={output}")
    if args.strict and report["overall_status"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
