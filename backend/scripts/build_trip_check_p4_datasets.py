"""Generate the deterministic P4 dev/regression data contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "trip_check_v1" / "p4"
PILOT = ROOT / "evals" / "trip_check_v1" / "pilot.jsonl"
CITIES = ("北京", "上海", "杭州")
FAULT_CLASSES = (
    "advice_completeness",
    "empty_candidate_set",
    "candidate_receipt_missing",
    "route_conflict",
    "duplicate_apply",
    "concurrent_apply",
    "solver_unsat",
    "solver_timeout",
    "solver_fallback",
)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _frozen_pilot_bytes() -> bytes:
    """Reproduce the CRLF byte policy used by the frozen P4 v1 manifest.

    The manifest was sealed from a Windows checkout. Git may materialize the
    same pilot blob with LF in another checkout, so hashing ``read_bytes()``
    made the deterministic factory depend on checkout line endings.
    """

    text = PILOT.read_text(encoding="utf-8").replace("\r\n", "\n")
    return text.replace("\n", "\r\n").encode("utf-8")


def _case(split: str, city: str, city_index: int, index: int) -> dict[str, Any]:
    prefix = {"北京": "bj", "上海": "sh", "杭州": "hz"}[city]
    fault_class = FAULT_CLASSES[index % len(FAULT_CLASSES)]
    family_index = index // 3
    fixture = {
        "city": city,
        "days": 2 + index % 4,
        "traveler_count": 2 + (index // 2) % 4,
        "stop_count": 4 + index % 8,
        "finding": {
            "status": "UNKNOWN" if index % 5 == 0 else "VIOLATED",
            "severity": ("BLOCKER", "HIGH", "MEDIUM")[index % 3],
            "reason_code": f"P4_{fault_class.upper()}",
        },
        "candidate_set_state": (
            "EMPTY"
            if fault_class == "empty_candidate_set"
            else "RECEIPT_INCOMPLETE"
            if fault_class == "candidate_receipt_missing"
            else "FROZEN"
        ),
    }
    oracle = {
        "advice_required": True,
        "specific_place_allowed": fixture["candidate_set_state"] == "FROZEN",
        "expected_strategy_outcome": (
            "UNSAT"
            if fault_class == "solver_unsat"
            else "TIMEOUT"
            if fault_class == "solver_timeout"
            else "FALLBACK"
            if fault_class == "solver_fallback"
            else "FEASIBLE"
        ),
        "max_new_blocker_high_unknown": 0,
    }
    review_input = {
        "split": split,
        "city": city,
        "family": family_index,
        "fault_class": fault_class,
        "fixture_hash": digest(fixture),
        "oracle_hash": digest(oracle),
    }
    item: dict[str, Any] = {
        "schema_version": "trip-check-p4-dataset-v1",
        "case_id": f"p4.{split}.{prefix}.{index + 1:03d}",
        "split": split,
        "city": city,
        "source_family": f"p4.{split}.{prefix}.family-{family_index:02d}",
        "fault_class": fault_class,
        "fixture": fixture,
        "oracle": oracle,
        "fixture_hash": digest(fixture),
        "oracle_hash": digest(oracle),
        "provenance": {
            "generated_by": "deterministic_p4_case_factory_v1",
            "reviewed_by": "p4_contract_reviewer_v1",
            "review_receipt": digest(review_input),
            "contains_human_data": False,
        },
        "seed": 20260823 + city_index * 1000 + index,
    }
    item["case_hash"] = digest(item)
    return item


def build() -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    per_city = {"dev": 60, "regression": 24}
    datasets = {
        split: [
            _case(split, city, city_index, index)
            for city_index, city in enumerate(CITIES)
            for index in range(per_city[split])
        ]
        for split in per_city
    }
    pilot_bytes = _frozen_pilot_bytes()
    manifest: dict[str, Any] = {
        "schema_version": "trip-check-p4-dataset-manifest-v1",
        "frozen": True,
        "splits": {
            "pilot": {
                "count": len(PILOT.read_text(encoding="utf-8").splitlines()),
                "sha256": hashlib.sha256(pilot_bytes).hexdigest(),
                "source": "../pilot.jsonl",
            },
            **{
                split: {
                    "count": len(rows),
                    "city_counts": {
                        city: sum(row["city"] == city for row in rows)
                        for city in CITIES
                    },
                    "dataset_hash": digest(rows),
                }
                for split, rows in datasets.items()
            },
            "frozen_blind": {"count": 0, "status": "NOT_RUN"},
        },
        "fault_classes": list(FAULT_CLASSES),
    }
    manifest["manifest_hash"] = digest(manifest)
    return datasets, manifest


def main() -> None:
    datasets, manifest = build()
    OUT.mkdir(parents=True, exist_ok=True)
    for split, rows in datasets.items():
        (OUT / f"{split}_v1.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
            newline="\n",
        )
    (OUT / "dataset_v1.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
