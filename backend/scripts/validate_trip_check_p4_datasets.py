"""Validate P4 split, provenance, privacy, distribution and freeze hashes."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from scripts.build_trip_check_p4_datasets import (
    CITIES,
    FAULT_CLASSES,
    OUT,
    PILOT,
    build,
    canonical,
    digest,
)


EXPECTED = {"dev": 180, "regression": 72}
EXPECTED_PER_CITY = {"dev": 60, "regression": 24}
PRIVATE_PATTERN = re.compile(
    r"(?:1[3-9]\d{9})|(?:\b\d{17}[\dXx]\b)|(?:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)"
)


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def validate(root: Path = OUT) -> dict[str, Any]:
    errors: list[str] = []
    rows_by_split = {
        split: _load(root / f"{split}_v1.jsonl")
        for split in EXPECTED
    }
    manifest = json.loads((root / "dataset_v1.manifest.json").read_text(encoding="utf-8"))
    all_ids: list[str] = []
    all_hashes: list[str] = []
    family_splits: dict[str, set[str]] = defaultdict(set)
    distribution: dict[str, Any] = {}
    for split, rows in rows_by_split.items():
        if len(rows) != EXPECTED[split]:
            errors.append(f"{split}: expected {EXPECTED[split]} cases, got {len(rows)}")
        city_counts = Counter(row.get("city") for row in rows)
        expected_city = Counter({city: EXPECTED_PER_CITY[split] for city in CITIES})
        if city_counts != expected_city:
            errors.append(f"{split}: invalid city distribution {dict(city_counts)}")
        fault_counts = Counter(row.get("fault_class") for row in rows)
        if set(fault_counts) != set(FAULT_CLASSES):
            errors.append(f"{split}: fault class coverage is incomplete")
        distribution[split] = {
            "count": len(rows),
            "city_counts": dict(sorted(city_counts.items())),
            "fault_counts": dict(sorted(fault_counts.items())),
        }
        for row in rows:
            case_id = row.get("case_id", "<missing>")
            all_ids.append(case_id)
            all_hashes.append(row.get("case_hash"))
            family_splits[row.get("source_family")].add(split)
            body = {key: value for key, value in row.items() if key != "case_hash"}
            if row.get("case_hash") != digest(body):
                errors.append(f"{case_id}: case hash mismatch")
            if row.get("fixture_hash") != digest(row.get("fixture")):
                errors.append(f"{case_id}: fixture hash mismatch")
            if row.get("oracle_hash") != digest(row.get("oracle")):
                errors.append(f"{case_id}: oracle hash mismatch")
            fixture = row.get("fixture") or {}
            if not 2 <= fixture.get("days", 0) <= 5:
                errors.append(f"{case_id}: days outside 2-5")
            if not 2 <= fixture.get("traveler_count", 0) <= 5:
                errors.append(f"{case_id}: traveler count outside 2-5")
            provenance = row.get("provenance") or {}
            if (
                provenance.get("generated_by") == provenance.get("reviewed_by")
                or not provenance.get("review_receipt")
            ):
                errors.append(f"{case_id}: independent review receipt missing")
            if provenance.get("contains_human_data") is not False:
                errors.append(f"{case_id}: privacy declaration missing")
            privacy_surface = canonical({
                "case_id": case_id,
                "source_family": row.get("source_family"),
                "fixture": fixture,
                "oracle": row.get("oracle"),
            })
            if PRIVATE_PATTERN.search(privacy_surface):
                errors.append(f"{case_id}: possible personal data")
    if len(all_ids) != len(set(all_ids)):
        errors.append("case IDs must be unique across splits")
    if len(all_hashes) != len(set(all_hashes)):
        errors.append("case hashes must be unique across splits")
    if any(len(splits) != 1 for splits in family_splits.values()):
        errors.append("source family leaked across splits")
    built_datasets, built_manifest = build()
    if any(digest(rows_by_split[split]) != digest(built_datasets[split]) for split in EXPECTED):
        errors.append("checked-in data differs from deterministic factory")
    if manifest != built_manifest:
        errors.append("manifest differs from deterministic factory")
    pilot_count = len(PILOT.read_text(encoding="utf-8").splitlines())
    if pilot_count != 18 or manifest["splits"]["pilot"]["count"] != 18:
        errors.append("P1 pilot binding must remain exactly 18")
    if manifest["splits"]["frozen_blind"] != {"count": 0, "status": "NOT_RUN"}:
        errors.append("frozen_blind must remain NOT_RUN with zero cases")
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "distribution": distribution,
        "pilot_count": pilot_count,
        "frozen_blind_count": 0,
        "dataset_hashes": {
            split: digest(rows) for split, rows in rows_by_split.items()
        },
    }


def main() -> None:
    result = validate()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
