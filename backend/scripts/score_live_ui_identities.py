"""Compare live, persisted card identities with separately reviewed POI facts.

Explicitly ambiguous targets remain in the coverage denominator. The report is
descriptive; this command never changes source facts, predictions or thresholds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def poi_id(value):
    return str(value or "").removeprefix("amap:").removeprefix("AMAP:")


def score(report, facts):
    cities = []
    for case in report["cases"]:
        gold = [f for f in facts["facts"] if f["city"] == case["city"]]
        metrics = case.get("worker_metrics", {})
        predictions = metrics.get("initial_activities", [])
        visible = [p for p in predictions if p.get("canonical_place_id") and p["role"] == "PLANNED"]
        used = set()
        rows = []
        for fact in gold:
            allowed = set(fact["expected_poi_ids"])
            match = next((index for index, p in enumerate(visible) if index not in used
                          and p["day_index"] == fact["day_index"]
                          and poi_id(p["canonical_place_id"]) in allowed), None)
            if match is not None:
                used.add(match)
            rows.append({"day_index": fact["day_index"], "source_name": fact["source_name"],
                         "requires_choice": not fact["expected_automatic_confirmation"],
                         "correct_confirmed_identity": match is not None,
                         "matched_poi_id": poi_id(visible[match]["canonical_place_id"]) if match is not None else None,
                         "expected_poi_ids": sorted(allowed)})
        unexpected = [p for index, p in enumerate(visible) if index not in used]
        correct = len(used)
        cities.append({"city": case["city"], "measurement_status": metrics.get("status", "NOT_RUN"),
                       "expected_main_stops": len(gold), "confirmed_main_stops": len(visible),
                       "correct_visible_identities": correct,
                       "coverage": correct / len(gold) if gold else None,
                       "identity_precision": correct / len(visible) if visible else None,
                       "rows": rows, "unexpected_confirmed": unexpected})
    expected = sum(c["expected_main_stops"] for c in cities)
    confirmed = sum(c["confirmed_main_stops"] for c in cities)
    correct = sum(c["correct_visible_identities"] for c in cities)
    return {"schema_version": "live-ui-independent-poi-score-v1", "cities": cities,
            "summary": {"expected_main_stops": expected, "confirmed_main_stops": confirmed,
                        "correct_visible_identities": correct,
                        "coverage": correct / expected if expected else None,
                        "identity_precision": correct / confirmed if confirmed else None,
                        "unmatched_predictions": sum(len(c["unexpected_confirmed"]) for c in cities)},
            "limits": ["Only these developer-constructed short inputs; not long-guide or holdout acceptance.",
                       "Agent-reviewed map identities, not human travel experience or operating facts.",
                       "Ambiguous source locations remain in the denominator and must not be guessed.",
                       "Browser visibility is checked separately against each persisted confirmed card."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-report", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Use a new output file to preserve all earlier measurements")
    result = score(json.loads(args.ui_report.read_text(encoding="utf-8")),
                   json.loads(args.facts.read_text(encoding="utf-8")))
    result["inputs"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in (args.ui_report, args.facts)}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"]))


if __name__ == "__main__":
    main()
