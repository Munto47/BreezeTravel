"""Measure 24 synthetic texts through the selected live semantic adapter.

Only normalized synthetic differences and numeric usage are retained. The POI
resolver always refuses a match; this is not a place, route or safety benchmark.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import time


DEFAULT_CASES = Path(__file__).resolve().parents[1] / "eval_data/itinerary_quality_v1/cases.json"
METRIC_KEYS = ("external_calls", "repair_call_count", "input_tokens", "output_tokens", "estimated_cost_cny", "latency_ms")


def load_cases(path: Path) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    if not 1 <= len(cases) <= 24 or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Expected 1 to 24 uniquely identified synthetic cases")
    for case in cases:
        expected = case["expected"]
        if not case["text"].strip() or not 1 <= expected["day_count"] <= 14:
            raise ValueError("Invalid synthetic input or day count")
        for role in ("planned", "optional"):
            for day, name in expected[role]:
                if day is not None and not 1 <= day <= expected["day_count"]:
                    raise ValueError("Expected day is outside the case")
                if name is not None and (not name.strip() or name not in case["text"]):
                    raise ValueError("Expected name must occur in its synthetic source")
    return cases


def _fingerprint(backend: Path) -> str:
    digest = hashlib.sha256()
    folder = backend / "app/trip_understanding"
    for path in sorted([*folder.glob("*.py"), folder / "experience_inference_prompt.md"]):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _difference(expected: list, actual: list) -> dict:
    left, right = Counter(map(tuple, expected)), Counter(map(tuple, actual))
    return {
        "missing": [list(value) for value in (left - right).elements()],
        "extra": [list(value) for value in (right - left).elements()],
        "order_matches": expected == actual,
    }


def _compare(case: dict, output) -> dict:
    mentions = [item.compiled.mention for item in output.activities]
    planned = sorted((item for item in mentions if item.role.value == "PLANNED"),
                     key=lambda item: (item.day_index or 0, item.sequence_index))
    optional = sorted((item for item in mentions if item.role.value == "OPTIONAL"),
                      key=lambda item: (item.day_index or 0, item.sequence_index))
    actual_planned = [[item.day_index, item.atomic_place_name] for item in planned]
    actual_optional = [[item.day_index, item.atomic_place_name] for item in optional]
    atom_by_token = {item.compiled.public_activity_token: item.compiled.mention.atomic_place_name
                     for item in output.activities}
    public_slots = [[day, atom_by_token.get(card.activity_token)]
                    for day, value in enumerate(output.public_result.days, 1) for card in value.activities]
    expected = case["expected"]
    timing_differences = []
    for requested in expected.get("timings", []):
        matching = [item for item in planned if item.day_index == requested["day"]
                    and item.atomic_place_name == requested["name"]]
        occurrence = requested.get("occurrence", 1)
        found = matching[occurrence - 1] if len(matching) >= occurrence else None
        for field in ("start_time", "end_time", "visit_duration_minutes", "locked", "fixed_commitment"):
            if field not in requested:
                continue
            observed = getattr(found, field, "MISSING_ACTIVITY")
            if observed != requested[field]:
                timing_differences.append({"day": requested["day"], "name": requested["name"],
                                          "field": field, "expected": requested[field], "actual": observed})
    result = {
        "planned_exact": actual_planned == expected["planned"],
        "optional_exact": actual_optional == expected["optional"],
        "day_count_exact": len(output.public_result.days) == expected["day_count"],
        "public_slots_exact": public_slots == expected["planned"],
        "timings_exact": not timing_differences,
        "actual_day_count": len(output.public_result.days),
        "planned_difference": _difference(expected["planned"], actual_planned),
        "optional_difference": _difference(expected["optional"], actual_optional),
        "public_slots_difference": _difference(expected["planned"], public_slots),
        "timing_differences": timing_differences,
        "unprocessed_count": output.proposal.unprocessed_count,
        "poi_confirmations": sum(item.place is not None for item in output.activities),
    }
    result["exact"] = all(result[key] for key in (
        "planned_exact", "optional_exact", "day_count_exact", "public_slots_exact", "timings_exact"))
    return result


def _summary(rows: list[dict]) -> dict:
    groups = {}
    for row in rows:
        group = groups.setdefault(row["group"], {"cases": 0, "exact": 0, "errors": 0})
        group["cases"] += 1
        group["exact"] += int(row.get("exact", False))
        group["errors"] += int(row["status"] == "ERROR")
    result = {"cases": len(rows), "exact": sum(row.get("exact", False) for row in rows),
              "errors": sum(row["status"] == "ERROR" for row in rows), "groups": groups,
              "poi_external_calls": 0, "poi_confirmations": sum(row.get("poi_confirmations", 0) for row in rows)}
    for key in METRIC_KEYS:
        values = [row.get("usage", {}).get(key) for row in rows]
        result[key] = round(sum(values), 8) if values and all(type(value) in (int, float) for value in values) else None
        result[f"{key}_known_cases"] = sum(type(value) in (int, float) for value in values)
    return result


async def run(args, cases: list[dict]) -> int:
    from dotenv import dotenv_values

    root = args.module_root.resolve()
    backend = root if (root / "app").is_dir() else root / "backend"
    if not (backend / "app/trip_understanding/experience_inference.py").is_file():
        raise ValueError("Module root does not contain the experience adapter")
    # No app imports are permitted until the chosen source root takes precedence.
    sys.path.insert(0, str(backend))
    from app.trip_understanding import experience_inference, pipeline
    from app.trip_understanding.models import PlaceResolutionOutcome
    for module in (experience_inference, pipeline):
        if not Path(module.__file__).resolve().is_relative_to(backend):
            raise RuntimeError("Selected source root was not loaded")
    source_fingerprint = _fingerprint(backend)
    commit = subprocess.run(["git", "-C", str(backend), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    values = dotenv_values(args.config_env, interpolate=False)
    required = ("QWEN_API_KEY", "QWEN_API_URL", "TRIP_UNDERSTANDING_QWEN_MODEL")
    if any(not values.get(key) for key in required):
        raise ValueError("Selected private configuration is missing a required model setting")
    def number(key, default, convert):
        value = values.get(key)
        return convert(value) if value else default
    provider = experience_inference.ExperienceQwenProvider(
        api_key=values["QWEN_API_KEY"], base_url=values["QWEN_API_URL"],
        model=values["TRIP_UNDERSTANDING_QWEN_MODEL"],
        deadline_seconds=number("TRIP_UNDERSTANDING_QWEN_DEADLINE_SECONDS", 30, float),
        max_output_tokens=number("TRIP_UNDERSTANDING_QWEN_MAX_OUTPUT_TOKENS", 4096, int),
        input_cny_per_million=number("TRIP_UNDERSTANDING_QWEN_INPUT_CNY_PER_MILLION", None, float),
        output_cny_per_million=number("TRIP_UNDERSTANDING_QWEN_OUTPUT_CNY_PER_MILLION", None, float),
    )

    class RecordingInference:
        binding = {}
        async def propose(self, text):
            self.binding = {}
            try:
                result = await provider.propose(text)
            except Exception as exc:
                self.binding = getattr(exc, "provider_binding", {})
                raise
            self.binding = result.binding
            return result

    class RefusingResolver:
        calls = []
        async def resolve(self, *, city, atomic_place_name, category_hint=None):
            self.calls.append((city, atomic_place_name))
            return PlaceResolutionOutcome(receipt={"provider": "MEASUREMENT_NO_POI",
                "status": "NO_UNIQUE_MATCH", "external_calls": 0})

    inference, resolver = RecordingInference(), RefusingResolver()
    runner = pipeline.TripUnderstandingPipeline(inference, resolver)
    rows = []
    report = {"source_root": str(backend), "source_commit": commit,
              "source_fingerprint": source_fingerprint,
              "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
              "model": provider.model, "started_at": datetime.now(timezone.utc).isoformat(),
              "measurement": "SYNTHETIC_LIVE_SEMANTICS_WITH_REFUSING_POI_RESOLVER",
              "raw_model_responses_retained": False, "cases": rows}
    def save():
        report["summary"] = _summary(rows)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        for index, case in enumerate(cases, 1):
            if _fingerprint(backend) != source_fingerprint:
                raise RuntimeError("Source changed during measurement; remaining calls stopped")
            resolver.calls = []
            started = time.perf_counter()
            row = {"id": case["id"], "group": case["group"], "status": "OK"}
            try:
                output = await runner.run(case["text"])
                row.update(_compare(case, output))
            except Exception as exc:
                # Never stringify model, validation, transport or credential errors.
                category = getattr(exc, "category", None)
                row.update(status="ERROR", exact=False,
                           error_category=category if isinstance(category, str) and category.isupper() else type(exc).__name__)
            row["usage"] = {key: inference.binding.get(key) for key in METRIC_KEYS}
            row["validation_issues"] = [
                {"field": issue.get("field"), "category": issue.get("category")}
                for call in inference.binding.get("calls", [])
                for issue in call.get("validation_errors", [])
            ]
            row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            row["resolver_requests"] = len(resolver.calls)
            row["resolver_cities"] = sorted({city for city, _name in resolver.calls})
            rows.append(row)
            save()
            print(json.dumps({"progress": index, "total": len(cases), "id": case["id"],
                              "group": case["group"], "exact": row["exact"], "status": row["status"],
                              "model_calls": row["usage"].get("external_calls"),
                              "elapsed_ms": row["elapsed_ms"]}, ensure_ascii=False), flush=True)
    finally:
        await provider.aclose()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["source_unchanged"] = _fingerprint(backend) == source_fingerprint
        save()
    print(json.dumps({"summary": report["summary"]}, ensure_ascii=False), flush=True)
    return int(report["summary"]["errors"] > 0 or report["summary"]["exact"] != len(cases) or not report["source_unchanged"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--config-env", type=Path)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-check", action="store_true", help="Validate only the synthetic case file; zero model calls")
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if args.self_check:
        print(json.dumps({"synthetic_cases": len(cases), "groups": dict(Counter(case["group"] for case in cases)),
                          "external_calls": 0}, ensure_ascii=False))
        return 0
    if args.config_env is None or args.output is None:
        parser.error("--config-env and --output are required for the live measurement")
    logging.disable(logging.CRITICAL)
    return asyncio.run(run(args, cases))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_category": type(error).__name__, "message": "Measurement stopped; no raw error retained"}), file=sys.stderr)
        raise SystemExit(2) from None
