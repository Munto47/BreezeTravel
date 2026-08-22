from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "evals" / "trip_check_v1" / "pilot.jsonl"
SUPPORTED_CITIES = {"北京", "上海", "杭州"}


def validate_pilot(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    errors: list[str] = []
    cases: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            case = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: invalid JSON: {exc.msg}")
            continue
        cases.append(case)
        case_id = case.get("case_id", f"line-{line_number}")
        if case.get("schema_version") != "trip-check-pilot-v1":
            errors.append(f"{case_id}: invalid schema_version")
        if case.get("split") != "pilot":
            errors.append(f"{case_id}: split must be pilot")
        if case.get("city") not in SUPPORTED_CITIES:
            errors.append(f"{case_id}: unsupported city")
        if not 2 <= case.get("traveler_count", 0) <= 5:
            errors.append(f"{case_id}: traveler_count must be 2..5")
        if not 2 <= case.get("days", 0) <= 5:
            errors.append(f"{case_id}: days must be 2..5")
        if not case.get("raw_text", "").strip():
            errors.append(f"{case_id}: raw_text is required")
        expected = case.get("expected", {})
        for zero_gate in ("wrong_poi_auto_accept_max", "repair_new_high_max", "repair_new_unknown_max"):
            if expected.get(zero_gate) != 0:
                errors.append(f"{case_id}: {zero_gate} must remain zero")
        if not case.get("fixture_profile"):
            errors.append(f"{case_id}: fixture_profile is required")

    ids = [case.get("case_id") for case in cases]
    duplicates = sorted(case_id for case_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        errors.append(f"duplicate case ids: {duplicates}")
    city_counts = Counter(case.get("city") for case in cases)
    if len(cases) != 18:
        errors.append(f"pilot must contain exactly 18 cases, got {len(cases)}")
    if city_counts != Counter({"北京": 6, "上海": 6, "杭州": 6}):
        errors.append(f"pilot city distribution must be 6/6/6, got {dict(city_counts)}")
    if not any(case.get("expected", {}).get("requires_user_resolution") for case in cases):
        errors.append("pilot must include explicit user-resolution cases")
    if not any(case.get("expected", {}).get("required_reason_codes") for case in cases):
        errors.append("pilot must include deterministic conflict cases")

    return {
        "schema_version": "trip-check-pilot-validation-v1",
        "valid": not errors,
        "execution_status": "NOT_RUN",
        "case_count": len(cases),
        "city_counts": dict(sorted(city_counts.items())),
        "errors": errors,
    }


def main() -> int:
    result = validate_pilot()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
