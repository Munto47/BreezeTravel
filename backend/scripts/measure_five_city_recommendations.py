"""Opt-in real five-city examples: model/POI recognition, lodging and dining.

No fixture providers, browser history, booking or production writes. Five examples
are operational evidence, not population accuracy or dining coverage acceptance.
Private credentials are read in-process and never copied into the report.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
CASES = {
    "北京": [["故宫博物院", "景山公园"], ["天坛公园", "前门大街"], ["颐和园", "圆明园"]],
    "上海": [["外滩", "豫园"], ["上海博物馆人民广场馆", "南京路步行街"], ["上海科技馆", "世纪公园"]],
    "广州": [["广东省博物馆", "广州塔"], ["越秀公园", "陈家祠"], ["沙面", "北京路步行街"]],
    "深圳": [["莲花山公园", "深圳博物馆"], ["深圳湾公园", "欢乐海岸"], ["深圳人才公园", "海上世界"]],
    "杭州": [["西湖风景名胜区", "浙江省博物馆孤山馆"], ["灵隐寺", "岳王庙"], ["杭州博物馆", "河坊街"]],
}


class HttpMeasurements:
    """Instrument real HTTP dispatches, including sends that time out or fail."""
    def __init__(self):
        self.phase = "initialization"
        self.records: list[dict] = []
        self.active = Counter()
        self.peak = Counter()
        self.original = httpx.AsyncClient.send

    def install(self):
        tracker = self
        original = self.original

        async def send(client, request, *args, **kwargs):
            phase = tracker.phase
            kind = "model"
            if request.url.host == "restapi.amap.com":
                path = request.url.path
                kind = "district" if path.endswith("/district") else "route" if "/direction/" in path else "poi"
            record = {"phase": phase, "kind": kind, "status_code": None, "error_category": None}
            tracker.records.append(record)
            tracker.active[phase] += 1
            tracker.peak[phase] = max(tracker.peak[phase], tracker.active[phase])
            started = time.perf_counter()
            try:
                response = await original(client, request, *args, **kwargs)
                record["status_code"] = response.status_code
                if kind == "poi" and phase.endswith(":dining"):
                    try:
                        pois = response.json().get("pois", [])
                        record["poi_types"] = [{key: poi.get(key) for key in ("id", "name", "typecode", "type")}
                            for poi in pois if isinstance(poi, dict)]
                    except (ValueError, AttributeError):
                        pass
                return response
            except BaseException as error:
                record["error_category"] = type(error).__name__
                raise
            finally:
                tracker.active[phase] -= 1
                record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)

        httpx.AsyncClient.send = send

    def restore(self):
        httpx.AsyncClient.send = self.original

    def summary(self, phase: str):
        rows = [r for r in self.records if r["phase"] == phase]
        return {"http_attempts": dict(Counter(r["kind"] for r in rows)),
            "peak_http_concurrency": self.peak[phase], "calls": rows}


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def runtime_hashes():
    folder = ROOT / "backend/app/trip_understanding"
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.glob("*"))
        if p.suffix in {".py", ".json", ".md"}}


def safe_failure(error):
    category = getattr(error, "category", None)
    return category if isinstance(category, str) and category.isupper() else type(error).__name__


def route_fact(fact):
    return {"status": fact.status, "minutes": fact.duration_minutes,
        "transfer_count": fact.transfer_count, "external_http_calls": fact.external_call_count,
        "observed_at": fact.observed_at.isoformat(), "expires_at": fact.expires_at.isoformat(),
        "failure_category": fact.provider_binding.get("category")}


async def inspect_report(args):
    """Separate live follow-up of POI types for already recorded dining rows."""
    values = dotenv_values(args.config_env, interpolate=False)
    report = json.loads(args.output.read_text(encoding="utf-8"))
    attempts, inspections = 0, []
    async with httpx.AsyncClient(timeout=6) as client:
        for city in report["cities"]:
            for day in city.get("dining", {}).get("days", []):
                candidates = day.get("candidates", [])
                anchors = [s for s in city["confirmed_stops"] if s["day"] == day["day_index"] and s["status"] == "AUTO_MATCHED"]
                if not candidates or not anchors:
                    continue
                anchor = anchors[max(0, (len(anchors) - 1) // 2)]
                attempts += 1
                row = {"city": city["city"], "day": day["day_index"], "anchor": anchor["name"], "candidates": []}
                try:
                    response = await client.get("https://restapi.amap.com/v5/place/around", params={
                        "key": values["AMAP_API_KEY"], "location": f"{anchor['longitude']:.6f},{anchor['latitude']:.6f}",
                        "radius": 1200, "types": "050000", "sortrule": "distance", "page_size": 25, "page_num": 1,
                        "region": city["city"], "city_limit": "true", "show_fields": "business"})
                    response.raise_for_status()
                    payload = response.json()
                    by_id = {p["id"]: p for p in payload.get("pois", []) if isinstance(p, dict) and p.get("id")}
                    row["provider_status"] = payload.get("status")
                    for candidate in candidates:
                        place = candidate["place"]
                        raw = by_id.get(place["canonical_place_id"].removeprefix("amap:"), {})
                        row["candidates"].append({"name": place["name"], "poi_id": place["canonical_place_id"],
                            "same_id_found": bool(raw), "typecode": raw.get("typecode"), "type": raw.get("type"),
                            "address": raw.get("address"), "business_area": place.get("business_area")})
                except Exception as error:
                    row["error_category"] = safe_failure(error)
                inspections.append(row)
    previous = report.get("supplemental_dining_type_inspection")
    if previous:
        report.setdefault("previous_dining_type_inspections", []).append(previous)
    report["supplemental_dining_type_inspection"] = {"observed_at": datetime.now(timezone.utc).isoformat(),
        "additional_poi_http_attempts": attempts, "route_http_attempts": 0, "days": inspections}
    save_json(args.output, report)
    print(json.dumps({"inspection_days": len(inspections), "additional_poi_http_attempts": attempts}, ensure_ascii=False))
    return 0


async def measure(args):
    values = dotenv_values(args.config_env, interpolate=False)
    required = ("QWEN_API_KEY", "QWEN_API_URL", "TRIP_UNDERSTANDING_QWEN_MODEL", "AMAP_API_KEY")
    if not all(values.get(key) for key in required):
        raise ValueError("Required live provider configuration is unavailable")
    os.environ.update({key: str(value) for key, value in values.items() if value is not None})
    os.environ.update(TRIP_UNDERSTANDING_PROVIDER_MODE="live", AMAP_MOCK="false",
        LANGCHAIN_TRACING_V2="false", LANGSMITH_TRACING="false")
    from app.config import get_settings
    from app.trip_understanding.amap_place import AmapPlaceResolver
    from app.trip_understanding.amap_route import AmapRouteProvider
    from app.trip_understanding.daily_dining import build_daily_meals
    from app.trip_understanding.experience_inference import ExperienceQwenProvider
    from app.trip_understanding.map_repository import _plan_for_result
    from app.trip_understanding.pipeline import TripUnderstandingPipeline
    from app.trip_understanding.stay import AmapStayCandidateProvider, StayRecommendationEngine, stay_plan_from_map

    get_settings.cache_clear()
    model = ExperienceQwenProvider(api_key=values["QWEN_API_KEY"], base_url=values["QWEN_API_URL"],
        model=values["TRIP_UNDERSTANDING_QWEN_MODEL"],
        enable_source_visits=True,
        deadline_seconds=float(values.get("TRIP_UNDERSTANDING_QWEN_DEADLINE_SECONDS") or 60),
        max_output_tokens=int(values.get("TRIP_UNDERSTANDING_QWEN_MAX_OUTPUT_TOKENS") or 4096))
    pipeline = TripUnderstandingPipeline(model, AmapPlaceResolver(api_key=values["AMAP_API_KEY"]), max_place_concurrency=4)
    tracker = HttpMeasurements()
    report = {"schema_version": "five-city-recommendation-examples-v1", "provenance": "LIVE_QWEN_AND_AMAP",
        "input_provenance": "AGENT_AUTHORED_PUBLIC_POI_NAMES", "acceptance_claim": "NONE_FIVE_EXAMPLES_ONLY",
        "started_at": datetime.now(timezone.utc).isoformat(), "model": model.model,
        "runtime_files_before": runtime_hashes(), "budgets": {"stay_recall_per_segment": 12,
            "stay_measured_per_segment": 6, "stay_display_per_segment": 3, "stay_route_http_per_city": 96,
            "stay_concurrency": 4, "dining_route_dispatches_per_city": 48}, "cities": []}
    tracker.install()
    try:
        for city in args.cities:
            started = time.perf_counter()
            source = city + "三日两晚行程，按以下顺序游览：\n" + "\n".join(
                f"Day {i} {city}\n" + "\n".join(names) for i, names in enumerate(CASES[city], 1))
            row = {"city": city, "input_text": source, "expected_days": CASES[city], "status": "FAILED",
                "estimated_cost_cny": None, "cost_status": "UNKNOWN_PROVIDER_PRICES_NOT_CONFIGURED"}
            report["cities"].append(row)
            try:
                tracker.phase = f"{city}:recognition"
                stage = time.perf_counter()
                output = await pipeline.run(source)
                row["recognition"] = {"status": output.public_result.status,
                    "coverage": output.public_result.coverage.model_dump() if output.public_result.coverage else None,
                    "counts": output.resolution_receipt,
                    "usage": {key: output.proposal.binding.get(key) for key in
                        ("external_calls", "input_tokens", "output_tokens", "repair_call_count", "estimated_cost_cny")},
                    "elapsed_ms": round((time.perf_counter() - stage) * 1000, 2)}
                bindings = {a.compiled.public_activity_token: (a.place.canonical_place_id if a.place else None,
                    a.resolution_status.value, a.resolver_receipt) for a in output.activities}
                plan = _plan_for_result("real-evaluation-" + city, 1, output.public_result, bindings, city=city)
                row["confirmed_stops"] = [{"day": s.day_index, "order": s.sequence_index, "name": s.name,
                    "city": s.city, "poi_id": s.canonical_place_id, "status": s.resolution_status,
                    "longitude": s.longitude, "latitude": s.latitude} for s in plan.stops]
                stay_plan = stay_plan_from_map(plan)
                if stay_plan is None:
                    row["stay"] = {"status": "NO_OVERNIGHT_CONTEXT"}
                else:
                    tracker.phase = f"{city}:stay"
                    stage = time.perf_counter()
                    result = await StayRecommendationEngine(AmapStayCandidateProvider(api_key=values["AMAP_API_KEY"]),
                        AmapRouteProvider(api_key=values["AMAP_API_KEY"], max_concurrency=4), task_route_budget=96).recommend(stay_plan)
                    row["stay"] = {"status": result.status, "counts": result.provider_binding,
                        "elapsed_ms": round((time.perf_counter() - stage) * 1000, 2),
                        "segments": [{"city": segment.city, "overnight_days": segment.overnight_days,
                            "uncertain": segment.uncertain, "preserved_hotels": segment.preserved_hotels,
                            "anchors": [{"day": a.day_index, "direction": a.direction, "name": a.stop.name,
                                "poi_id": a.stop.canonical_place_id} for a in segment.anchors]} for segment in stay_plan.segments],
                        "candidates": [{"name": c.candidate.name, "poi_id": c.candidate.canonical_place_id,
                            "city": c.candidate.city, "address": c.candidate.area_or_address, "brand": c.candidate.brand,
                            "identity": {key: c.candidate.provider_binding.get(key) for key in ("property_identity", "brand_group",
                                "brand_priority", "official_url", "checked_at", "property_status", "segment_rank", "area_search_seed")},
                            "missing_leg_count": c.missing_leg_count, "score": c.total_score,
                            "legs": [{"day": leg.day_index, "direction": leg.direction, "endpoint": leg.endpoint_name,
                                "selected_mode": leg.selected_mode, "walking": route_fact(leg.walking),
                                "transit": route_fact(leg.transit)} for leg in c.legs]} for c in result.candidates]}
                tracker.phase = f"{city}:dining"
                stage = time.perf_counter()
                dining_stats = {}
                meals = await build_daily_meals(output.public_result, plan,
                    routes=AmapRouteProvider(api_key=values["AMAP_API_KEY"], max_concurrency=4), stats=dining_stats)
                row["dining"] = {"days": [{key: value for key, value in meal.items()
                    if key not in {"after_activity_token", "existing_activity_token"}} for meal in meals],
                    "counts": dining_stats, "elapsed_ms": round((time.perf_counter() - stage) * 1000, 2)}
                row["status"] = "COMPLETED"
            except Exception as error:
                row["error_category"] = safe_failure(error)
            finally:
                row["http"] = {stage: tracker.summary(f"{city}:{stage}") for stage in ("recognition", "stay", "dining")}
                row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
                save_json(args.output, report)
                print(json.dumps({"city": city, "status": row["status"], "elapsed_ms": row["elapsed_ms"],
                    "stay_status": row.get("stay", {}).get("status"),
                    "dining_available_days": row.get("dining", {}).get("counts", {}).get("available_days")}, ensure_ascii=False), flush=True)
    finally:
        tracker.restore()
        await pipeline.aclose()
        report["runtime_files_after"] = runtime_hashes()
        report["runtime_unchanged"] = report["runtime_files_before"] == report["runtime_files_after"]
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["summary"] = {"examples": len(report["cities"]), "completed_runs": sum(c["status"] == "COMPLETED" for c in report["cities"]),
            "official_property_matches": sum(sum(v["identity"]["property_identity"] == "OFFICIAL_NAME_ADDRESS"
                for v in c.get("stay", {}).get("candidates", [])) for c in report["cities"]),
            "route_http_attempts": sum(r["kind"] == "route" for r in tracker.records),
            "estimated_total_cost_cny": None, "statistical_acceptance": "NOT_MEASURED"}
        save_json(args.output, report)
    return int(any(c["status"] != "COMPLETED" for c in report["cities"]))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-env", type=Path, default=ROOT / ".local-artifacts/experience/experience.env")
    parser.add_argument("--output", type=Path, default=ROOT / ".local-artifacts/evaluation/five-city-recommendations.json")
    parser.add_argument("--cities", nargs="+", choices=list(CASES), default=list(CASES))
    parser.add_argument("--inspect-report", action="store_true", help="Only look up recorded dining POI types; no new model/routes")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    return asyncio.run(inspect_report(args) if args.inspect_report else measure(args))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error_category": type(error).__name__,
            "message": "Measurement stopped; private details omitted"}))
        sys.exit(2)
