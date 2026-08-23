"""Build the immutable P4 solver bake-off contract.

This generator is intentionally deterministic.  The checked-in JSONL is the
frozen comparison input; changing a case or oracle requires a new schema/version
instead of silently regenerating P4 v1 in place.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "trip_check_v1" / "p4"
CITIES = ("北京", "上海", "杭州")
SCENARIOS = (
    ("repairable_time_overlap", "TIME_CHAIN_BROKEN", "FEASIBLE"),
    ("repairable_route_gap", "ROUTE_GAP_INSUFFICIENT", "FEASIBLE"),
    ("repairable_intensity", "DAILY_INTENSITY_EXCEEDED", "FEASIBLE"),
    ("repairable_meal_gap", "MEAL_WINDOW_MISSING", "FEASIBLE"),
    ("locked_time_overlap", "TIME_CHAIN_BROKEN", "UNSAT"),
    ("missing_route_evidence", "ROUTE_EVIDENCE_UNAVAILABLE", "UNSAT"),
    ("empty_candidate_set", "PLACE_REPLACEMENT_REQUIRED", "UNSAT"),
    ("conflicting_place_receipt", "PLACE_EVIDENCE_CONFLICTING", "UNSAT"),
    ("dense_25_stop", "ROUTE_GAP_INSUFFICIENT", "PERFORMANCE"),
    ("solver_deadline", "DAILY_INTENSITY_EXCEEDED", "TIMEOUT"),
    ("strategy_exception", "TIME_CHAIN_BROKEN", "FALLBACK"),
    ("deterministic_tie", "TIME_CHAIN_BROKEN", "FEASIBLE"),
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _receipt(city_index: int, scenario_index: int, kind: str) -> dict[str, str]:
    identity = f"p4-v1:{city_index}:{scenario_index}:{kind}"
    return {
        "receipt_id": identity,
        "provider": "controlled_p4_fixture",
        "execution_mode": "fixture",
        "request_hash": _sha256({"identity": identity, "side": "request"}),
        "response_hash": _sha256({"identity": identity, "side": "response"}),
    }


def _case(city: str, city_index: int, scenario_index: int) -> dict[str, Any]:
    scenario, reason_code, expected_outcome = SCENARIOS[scenario_index]
    case_id = f"p4.{('bj', 'sh', 'hz')[city_index]}.{scenario_index + 1:02d}"
    stop_count = 25 if scenario == "dense_25_stop" else 4 + scenario_index % 4
    place_receipt = _receipt(city_index, scenario_index, "place")
    route_receipt = _receipt(city_index, scenario_index, "route")
    candidate_set = {
        "candidate_set_id": f"candidate-set-{case_id}",
        "candidates": [] if scenario == "empty_candidate_set" else [{
            "candidate_id": f"candidate-{case_id}",
            "canonical_place_id": f"fixture-place-{city_index}-{scenario_index}",
            "place_receipt": place_receipt,
            "route_receipts": [route_receipt],
        }],
    }
    candidate_set["content_hash"] = _sha256(candidate_set)
    case: dict[str, Any] = {
        "schema_version": "trip-check-p4-bakeoff-v1",
        "case_id": case_id,
        "split": "bakeoff",
        "city": city,
        "source_family": f"p4-{city_index}-{scenario_index:02d}",
        "scenario": scenario,
        "input": {
            "days": 5 if stop_count == 25 else 2 + scenario_index % 4,
            "traveler_count": 2 + scenario_index % 4,
            "stop_count": stop_count,
            "finding": {
                "status": "UNKNOWN" if "EVIDENCE" in reason_code else "VIOLATED",
                "reason_code": reason_code,
            },
        },
        "candidate_set": candidate_set,
        "oracle": {
            "expected_outcome": expected_outcome,
            "must_not_add_severity": ["BLOCKER", "HIGH", "UNKNOWN"],
            "candidate_boundary": "FROZEN_SET_ONLY",
        },
        "run_spec": {
            "strategy_ids": ["bounded_repair_v1", "routing_tsptw_v1", "cp_sat_v1"],
            "timeout_ms": 2000,
            "seed": 20260823,
            "objective_version": "p4_lexicographic_v1",
            "objective_order": [
                "no_new_serious_finding",
                "postcheck_success",
                "edit_cost",
                "route_cost",
                "stable_id",
            ],
        },
        "provenance": {
            "generated_by": "deterministic_p4_factory_v1",
            "reviewed_by": "independent_contract_review_v1",
            "contains_human_data": False,
        },
    }
    case["case_hash"] = _sha256(case)
    return case


def build() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cases = [
        _case(city, city_index, scenario_index)
        for city_index, city in enumerate(CITIES)
        for scenario_index in range(len(SCENARIOS))
    ]
    manifest: dict[str, Any] = {
        "schema_version": "trip-check-p4-bakeoff-manifest-v1",
        "frozen": True,
        "case_count": len(cases),
        "city_counts": {city: sum(case["city"] == city for case in cases) for city in CITIES},
        "dataset_hash": _sha256(cases),
        "case_hashes": {case["case_id"]: case["case_hash"] for case in cases},
        "frozen_blind_count": 0,
    }
    manifest["manifest_hash"] = _sha256(manifest)
    return cases, manifest


def main() -> None:
    cases, manifest = build()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "solver_bakeoff_v1.jsonl").write_text(
        "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
        encoding="utf-8",
        newline="\n",
    )
    (OUT / "solver_bakeoff_v1.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
