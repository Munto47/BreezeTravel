"""Collect redacted, auditable local evidence from Amap and QWeather.

This script deliberately calls provider REST endpoints directly.  It does not
use application fallbacks because a fixture/mock response must never be counted
as live-provider evidence.

Run from the repository root::

    $env:PYTHONPATH = "backend"
    python backend/scripts/verify_real_providers.py --iterations 3 --strict
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import jwt as pyjwt

from app.config import Settings


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO_ROOT / "backend" / "evidence" / "real_provider_local_authorized" / "summary.json"
EVIDENCE_CLASS = "real_provider_local_authorized"
FIXED_CITIES = ("北京", "上海", "杭州")

CITY_CASES: tuple[dict[str, Any], ...] = (
    {
        "city": "北京",
        "entity": "故宫博物院",
        "origin": {"label": "故宫博物院", "lng": 116.397029, "lat": 39.918058},
        "destination": {"label": "天坛公园", "lng": 116.417312, "lat": 39.887977},
    },
    {
        "city": "上海",
        "entity": "上海博物馆",
        "origin": {"label": "上海博物馆", "lng": 121.475346, "lat": 31.228821},
        "destination": {"label": "东方明珠", "lng": 121.499718, "lat": 31.239703},
    },
    {
        "city": "杭州",
        "entity": "西湖风景名胜区",
        "origin": {"label": "西湖断桥", "lng": 120.148054, "lat": 30.263267},
        "destination": {"label": "灵隐寺", "lng": 120.102446, "lat": 30.240338},
    },
)


@dataclass(frozen=True)
class HttpResult:
    status: int | None
    payload: dict[str, Any] | None
    latency_ms: float
    observed_at: str
    error_category: str | None = None
    error_detail: str | None = None


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def percentile(values: list[float], percent: float) -> float | None:
    """Linear percentile, stable for the deliberately small provider sample."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = (len(ordered) - 1) * percent
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 2)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 2)


def classify_provider_error(provider: str, code: str | None, http_status: int | None) -> str:
    normalized = str(code or "").upper()
    if http_status == 429 or normalized in {"429", "10019", "10020", "10021"}:
        return "rate_limited"
    if http_status in {401, 403} or normalized in {"401", "403", "10001", "10002", "10007", "10009"}:
        return "authentication_or_authorization"
    if normalized == "402":
        return "quota_or_payment"
    if http_status is not None and http_status >= 500:
        return "provider_http_5xx"
    if http_status is not None and http_status >= 400:
        return "provider_http_4xx"
    if provider == "amap" and normalized and normalized != "1":
        return "provider_business_error"
    if provider == "qweather" and normalized and normalized != "200":
        return "provider_business_error"
    return "invalid_or_empty_response"


async def _request_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, Any],
    headers: dict[str, str] | None,
    timeout_seconds: float,
) -> HttpResult:
    started = time.perf_counter()
    try:
        async with session.get(
            url,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            payload = await response.json(content_type=None)
            elapsed = (time.perf_counter() - started) * 1000
            if not isinstance(payload, dict):
                return HttpResult(
                    response.status, None, elapsed, _now_iso(), "invalid_json_shape", type(payload).__name__,
                )
            return HttpResult(response.status, payload, elapsed, _now_iso())
    except asyncio.TimeoutError:
        return HttpResult(None, None, (time.perf_counter() - started) * 1000, _now_iso(), "timeout", None)
    except aiohttp.ClientConnectorCertificateError as exc:
        return HttpResult(
            None, None, (time.perf_counter() - started) * 1000, _now_iso(), "tls_error", type(exc).__name__,
        )
    except aiohttp.ClientConnectorError as exc:
        return HttpResult(
            None, None, (time.perf_counter() - started) * 1000, _now_iso(), "connection_error", type(exc).__name__,
        )
    except (aiohttp.ClientError, json.JSONDecodeError) as exc:
        return HttpResult(
            None, None, (time.perf_counter() - started) * 1000, _now_iso(), "http_or_json_error", type(exc).__name__,
        )


def _base_sample(
    *,
    case_id: str,
    city: str,
    kind: str,
    iteration: int,
    request_descriptor: Any,
    result: HttpResult,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "city": city,
        "kind": kind,
        "iteration": iteration,
        "status": "error",
        "http_status": result.status,
        "latency_ms": round(result.latency_ms, 2),
        "observed_at": result.observed_at,
        "request_hash": _canonical_hash(request_descriptor),
        "response_hash": _canonical_hash(result.payload) if result.payload is not None else None,
        "result_count": 0,
        "error_category": result.error_category,
        "error_detail": result.error_detail,
    }


async def _collect_amap_entity(
    session: aiohttp.ClientSession,
    settings: Settings,
    case: dict[str, Any],
    iteration: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    endpoint = "https://restapi.amap.com/v5/place/text"
    public_params = {
        "keywords": case["entity"],
        "region": case["city"],
        "city_limit": "true",
        "page_size": 10,
        "show_fields": "business",
        "output": "json",
    }
    result = await _request_json(
        session,
        endpoint,
        params={"key": settings.amap_api_key, **public_params},
        headers=None,
        timeout_seconds=timeout_seconds,
    )
    sample = _base_sample(
        case_id=f"amap_entity_{case['city']}_{iteration}", city=case["city"], kind="entity",
        iteration=iteration, request_descriptor={"provider": "amap", "path": "/v5/place/text", **public_params},
        result=result,
    )
    data = result.payload or {}
    pois = data.get("pois") if isinstance(data.get("pois"), list) else []
    names = [str(item.get("name") or "") for item in pois if isinstance(item, dict)]
    sample["result_count"] = len(pois)
    sample["expected_entity"] = case["entity"]
    sample["expected_entity_found"] = any(case["entity"] in name or name in case["entity"] for name in names if name)
    sample["sample_result"] = {
        "name": names[0] if names else None,
        "provider_place_id": str(pois[0].get("id")) if pois and isinstance(pois[0], dict) else None,
        "location": str(pois[0].get("location")) if pois and isinstance(pois[0], dict) else None,
    }
    provider_code = str(data.get("status") or "")
    if result.error_category is None and result.status == 200 and provider_code == "1" and pois:
        sample["status"] = "ok"
        sample["error_category"] = None
    elif sample["error_category"] is None:
        sample["error_category"] = classify_provider_error("amap", data.get("infocode") or provider_code, result.status)
        sample["error_detail"] = str(data.get("info") or "") or None
    return sample


async def _collect_amap_route(
    session: aiohttp.ClientSession,
    settings: Settings,
    case: dict[str, Any],
    iteration: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    endpoint = "https://restapi.amap.com/v3/direction/driving"
    origin = case["origin"]
    destination = case["destination"]
    public_params = {
        "origin": f"{origin['lng']},{origin['lat']}",
        "destination": f"{destination['lng']},{destination['lat']}",
        "output": "json",
    }
    result = await _request_json(
        session,
        endpoint,
        params={"key": settings.amap_api_key, **public_params},
        headers=None,
        timeout_seconds=timeout_seconds,
    )
    sample = _base_sample(
        case_id=f"amap_route_{case['city']}_{iteration}", city=case["city"], kind="route",
        iteration=iteration,
        request_descriptor={
            "provider": "amap", "path": "/v3/direction/driving", **public_params,
            "origin_label": origin["label"], "destination_label": destination["label"],
        },
        result=result,
    )
    data = result.payload or {}
    paths = ((data.get("route") or {}).get("paths") or []) if isinstance(data.get("route"), dict) else []
    sample["result_count"] = len(paths)
    sample["sample_result"] = {
        "origin_label": origin["label"],
        "destination_label": destination["label"],
        "duration_seconds": int(float(paths[0].get("duration") or 0)) if paths else None,
        "distance_meters": int(float(paths[0].get("distance") or 0)) if paths else None,
    }
    provider_code = str(data.get("status") or "")
    if result.error_category is None and result.status == 200 and provider_code == "1" and paths:
        sample["status"] = "ok"
        sample["error_category"] = None
    elif sample["error_category"] is None:
        sample["error_category"] = classify_provider_error("amap", data.get("infocode") or provider_code, result.status)
        sample["error_detail"] = str(data.get("info") or "") or None
    return sample


def _qweather_headers(settings: Settings) -> dict[str, str]:
    if settings.qweather_auth_type == "jwt":
        issued_at = int(time.time()) - 30
        private_key = (
            "-----BEGIN PRIVATE KEY-----\n"
            + settings.qweather_private_key.strip()
            + "\n-----END PRIVATE KEY-----\n"
        )
        token = pyjwt.encode(
            {"sub": settings.qweather_project_id, "iat": issued_at, "exp": issued_at + 900},
            private_key,
            algorithm="EdDSA",
            headers={"kid": settings.qweather_key_id},
        )
        return {"Authorization": f"Bearer {token}"}
    return {"X-QW-Api-Key": settings.qweather_api_key}


async def _collect_qweather(
    session: aiohttp.ClientSession,
    settings: Settings,
    case: dict[str, Any],
    iteration: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    headers = _qweather_headers(settings)
    host = settings.qweather_api_host.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    geo_public_params = {"location": case["city"], "range": "cn", "number": "1", "lang": "zh"}
    geo_descriptor = {"provider": "qweather", "host": host, "path": "/geo/v2/city/lookup", **geo_public_params}
    geo = await _request_json(
        session,
        f"https://{host}/geo/v2/city/lookup",
        params=geo_public_params,
        headers=headers,
        timeout_seconds=timeout_seconds,
    )
    locations = (geo.payload or {}).get("location") or []
    location_id = str(locations[0].get("id")) if locations and isinstance(locations[0], dict) else ""
    forecast: HttpResult | None = None
    forecast_descriptor: dict[str, Any] | None = None
    if geo.error_category is None and geo.status == 200 and str((geo.payload or {}).get("code")) == "200" and location_id:
        forecast_public_params = {"location": location_id, "lang": "zh"}
        forecast_descriptor = {
            "provider": "qweather", "host": host, "path": "/v7/weather/3d", **forecast_public_params,
        }
        forecast = await _request_json(
            session,
            f"https://{host}/v7/weather/3d",
            params=forecast_public_params,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )

    total_latency = geo.latency_ms + (forecast.latency_ms if forecast else 0.0)
    observed_at = forecast.observed_at if forecast else geo.observed_at
    request_descriptors = [geo_descriptor] + ([forecast_descriptor] if forecast_descriptor else [])
    response_payloads = [geo.payload] + ([forecast.payload] if forecast else [])
    sample = {
        "case_id": f"qweather_forecast_{case['city']}_{iteration}",
        "city": case["city"],
        "kind": "weather",
        "iteration": iteration,
        "status": "error",
        "http_status": forecast.status if forecast else geo.status,
        "latency_ms": round(total_latency, 2),
        "observed_at": observed_at,
        "request_hash": _canonical_hash(request_descriptors),
        "response_hash": _canonical_hash(response_payloads),
        "result_count": 0,
        "error_category": geo.error_category,
        "error_detail": geo.error_detail,
        "provider_calls": [
            {
                "path": descriptor["path"],
                "request_hash": _canonical_hash(descriptor),
                "response_hash": _canonical_hash(http_result.payload) if http_result.payload is not None else None,
                "http_status": http_result.status,
                "latency_ms": round(http_result.latency_ms, 2),
                "observed_at": http_result.observed_at,
            }
            for descriptor, http_result in (
                [(geo_descriptor, geo)] + ([(forecast_descriptor, forecast)] if forecast_descriptor and forecast else [])
            )
        ],
    }
    geo_code = str((geo.payload or {}).get("code") or "")
    if not location_id and sample["error_category"] is None:
        sample["error_category"] = classify_provider_error("qweather", geo_code, geo.status)
        sample["error_detail"] = f"geo_code={geo_code or 'missing'}"
        return sample
    if forecast is None:
        return sample
    daily = (forecast.payload or {}).get("daily") or []
    forecast_code = str((forecast.payload or {}).get("code") or "")
    sample["result_count"] = len(daily) if isinstance(daily, list) else 0
    sample["sample_result"] = {
        "provider_location_id": location_id,
        "provider_location_name": str(locations[0].get("name") or "") if locations else None,
        "first_forecast_date": str(daily[0].get("fxDate") or "") if daily and isinstance(daily[0], dict) else None,
        "first_forecast_condition": str(daily[0].get("textDay") or "") if daily and isinstance(daily[0], dict) else None,
    }
    if (
        forecast.error_category is None
        and forecast.status == 200
        and forecast_code == "200"
        and isinstance(daily, list)
        and daily
    ):
        sample["status"] = "ok"
        sample["error_category"] = None
        sample["error_detail"] = None
    else:
        sample["error_category"] = forecast.error_category or classify_provider_error(
            "qweather", forecast_code, forecast.status,
        )
        sample["error_detail"] = forecast.error_detail or f"forecast_code={forecast_code or 'missing'}"
    return sample


def _provider_credentials_configured(settings: Settings) -> dict[str, Any]:
    qweather_configured = bool(settings.qweather_api_key)
    if settings.qweather_auth_type == "jwt":
        qweather_configured = bool(
            settings.qweather_private_key and settings.qweather_key_id and settings.qweather_project_id
        )
    return {
        "amap_api_key_configured": bool(settings.amap_api_key),
        "qweather_auth_type": settings.qweather_auth_type,
        "qweather_credentials_configured": qweather_configured,
        "qweather_custom_host_configured": bool(settings.qweather_api_host),
    }


def _performance(samples: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kind in ("entity", "route", "weather", "all"):
        selected = samples if kind == "all" else [sample for sample in samples if sample["kind"] == kind]
        latencies = [float(sample["latency_ms"]) for sample in selected]
        ok_count = sum(sample["status"] == "ok" for sample in selected)
        result[kind] = {
            "sample_count": len(selected),
            "ok_count": ok_count,
            "error_count": len(selected) - ok_count,
            "success_rate": round(ok_count / len(selected), 4) if selected else 0.0,
            "latency_ms": {
                "min": round(min(latencies), 2) if latencies else None,
                "p50": percentile(latencies, 0.50),
                "p95": percentile(latencies, 0.95),
                "max": round(max(latencies), 2) if latencies else None,
            },
        }
    return result


def validate_report(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("evidence_class") != EVIDENCE_CLASS:
        errors.append("wrong_evidence_class")
    runtime = report.get("runtime") or {}
    if runtime.get("amap_mock") is not False:
        errors.append("amap_mock_must_be_false")
    if runtime.get("demo_mode") is not False:
        errors.append("demo_mode_must_be_false")
    if sorted(report.get("fixed_cities") or []) != sorted(FIXED_CITIES):
        errors.append("fixed_city_set_mismatch")
    samples = report.get("samples") or []
    expected_count = len(FIXED_CITIES) * 3 * int(report.get("iterations") or 0)
    if len(samples) != expected_count:
        errors.append("sample_count_mismatch")
    for sample in samples:
        for field in (
            "request_hash", "response_hash", "observed_at", "status", "latency_ms", "result_count",
            "error_category",
        ):
            if field not in sample:
                errors.append(f"missing_{field}:{sample.get('case_id', 'unknown')}")
        if not str(sample.get("request_hash") or "").startswith("sha256:"):
            errors.append(f"invalid_request_hash:{sample.get('case_id', 'unknown')}")
        if not str(sample.get("response_hash") or "").startswith("sha256:"):
            errors.append(f"invalid_response_hash:{sample.get('case_id', 'unknown')}")
        if int(sample.get("result_count") or 0) < 1:
            errors.append(f"empty_result:{sample.get('case_id', 'unknown')}")
        if sample.get("kind") == "entity" and sample.get("expected_entity_found") is not True:
            errors.append(f"expected_entity_not_found:{sample.get('case_id', 'unknown')}")
        if sample.get("status") != "ok":
            errors.append(f"provider_failure:{sample.get('case_id', 'unknown')}:{sample.get('error_category')}")
    return errors


async def collect(settings: Settings, *, iterations: int, timeout_seconds: float) -> dict[str, Any]:
    credentials = _provider_credentials_configured(settings)
    preflight_errors = []
    if settings.amap_mock:
        preflight_errors.append("AMAP_MOCK must be false")
    if settings.demo_mode:
        preflight_errors.append("DEMO_MODE must be false")
    if settings.runtime_profile != "local_real":
        preflight_errors.append("RUNTIME_PROFILE must resolve to local_real for this evidence class")
    if not credentials["amap_api_key_configured"]:
        preflight_errors.append("AMAP_API_KEY is not configured")
    if not credentials["qweather_credentials_configured"]:
        preflight_errors.append("QWeather credentials are not configured")
    if preflight_errors:
        raise RuntimeError("; ".join(preflight_errors))

    started_at = _now_iso()
    samples: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        for iteration in range(1, iterations + 1):
            # Keep execution sequential so measured latency is not distorted by
            # local contention and provider quota is easy to audit.
            for case in CITY_CASES:
                samples.append(await _collect_amap_entity(session, settings, case, iteration, timeout_seconds))
                samples.append(await _collect_amap_route(session, settings, case, iteration, timeout_seconds))
                samples.append(await _collect_qweather(session, settings, case, iteration, timeout_seconds))

    report = {
        "schema_version": "1.0",
        "evidence_class": EVIDENCE_CLASS,
        "claim_boundary": {
            "is_real_provider": True,
            "is_local_authorized": True,
            "is_public_internet_e2e": False,
            "is_human_evidence": False,
            "is_release_approval": False,
            "note": "Local authorized REST observations only; no fixture fallback and no human/public claim.",
        },
        "run_started_at": started_at,
        "run_completed_at": _now_iso(),
        "iterations": iterations,
        "fixed_cities": list(FIXED_CITIES),
        "runtime": {
            "runtime_profile": settings.runtime_profile,
            "amap_mock": settings.amap_mock,
            "demo_mode": settings.demo_mode,
            **credentials,
            "sequential_requests": True,
            "timeout_seconds": timeout_seconds,
        },
        "sample_definition": {
            "entity": "Amap v5 exact landmark text search",
            "route": "Amap v3 driving route between fixed labeled coordinates",
            "weather": "QWeather city lookup followed by 3-day forecast",
            "raw_provider_payload_persisted": False,
            "authentication_material_persisted": False,
            "hash_algorithm": "SHA-256 over canonical UTF-8 JSON",
        },
        "performance": _performance(samples),
        "errors_by_category": {
            category: sum(sample.get("error_category") == category for sample in samples)
            for category in sorted({sample.get("error_category") for sample in samples if sample.get("error_category")})
        },
        "samples": samples,
    }
    report["validation_errors"] = validate_report(report)
    report["overall_status"] = "passed" if not report["validation_errors"] else "failed"
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=3, help="Sequential repetitions per city and operation")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero unless every fixed sample succeeds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.iterations < 1:
        raise SystemExit("--iterations must be >= 1")
    settings = Settings(_env_file=REPO_ROOT / ".env")
    report = asyncio.run(collect(settings, iterations=args.iterations, timeout_seconds=args.timeout_seconds))
    output = args.output if args.output.is_absolute() else REPO_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    perf = report["performance"]
    print(f"evidence_class={report['evidence_class']}")
    print(f"overall_status={report['overall_status']}")
    for kind in ("entity", "route", "weather", "all"):
        metrics = perf[kind]
        print(
            f"{kind}: ok={metrics['ok_count']}/{metrics['sample_count']} "
            f"p50_ms={metrics['latency_ms']['p50']} p95_ms={metrics['latency_ms']['p95']}"
        )
    print(f"evidence_file={output}")
    if args.strict and report["overall_status"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
