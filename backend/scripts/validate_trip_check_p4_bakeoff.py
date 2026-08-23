"""Validate the checked-in immutable P4 solver bake-off."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from scripts.build_trip_check_p4_bakeoff import CITIES, OUT, _sha256


DATASET = OUT / "solver_bakeoff_v1.jsonl"
MANIFEST = OUT / "solver_bakeoff_v1.manifest.json"


def validate(dataset: Path = DATASET, manifest_path: Path = MANIFEST) -> dict[str, Any]:
    cases = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    ids = [case.get("case_id") for case in cases]
    source_families = [case.get("source_family") for case in cases]
    if len(cases) != 36:
        errors.append(f"expected 36 cases, got {len(cases)}")
    if len(ids) != len(set(ids)):
        errors.append("case IDs must be unique")
    if len(source_families) != len(set(source_families)):
        errors.append("source families must be isolated")
    counts = Counter(case.get("city") for case in cases)
    if counts != Counter({city: 12 for city in CITIES}):
        errors.append(f"city distribution must be 12/12/12, got {dict(counts)}")
    for case in cases:
        case_id = case.get("case_id", "<missing>")
        claimed = case.get("case_hash")
        body = {key: value for key, value in case.items() if key != "case_hash"}
        if claimed != _sha256(body):
            errors.append(f"{case_id}: case_hash mismatch")
        candidate_set = case.get("candidate_set") or {}
        set_hash = candidate_set.get("content_hash")
        set_body = {key: value for key, value in candidate_set.items() if key != "content_hash"}
        if set_hash != _sha256(set_body):
            errors.append(f"{case_id}: CandidateSet hash mismatch")
        if case.get("split") != "bakeoff" or case.get("city") not in CITIES:
            errors.append(f"{case_id}: invalid scope")
        run_spec = case.get("run_spec") or {}
        if run_spec.get("timeout_ms") != 2000 or run_spec.get("seed") != 20260823:
            errors.append(f"{case_id}: RunSpec is not frozen")
        if (case.get("provenance") or {}).get("contains_human_data") is not False:
            errors.append(f"{case_id}: privacy declaration missing")
    expected_manifest_hash = manifest.get("manifest_hash")
    manifest_body = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if expected_manifest_hash != _sha256(manifest_body):
        errors.append("manifest_hash mismatch")
    if manifest.get("dataset_hash") != _sha256(cases):
        errors.append("dataset_hash mismatch")
    if manifest.get("case_hashes") != {case["case_id"]: case["case_hash"] for case in cases}:
        errors.append("case hash index mismatch")
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "case_count": len(cases),
        "city_counts": dict(sorted(counts.items())),
        "dataset_hash": _sha256(cases),
        "frozen_blind_count": manifest.get("frozen_blind_count"),
    }


def main() -> None:
    result = validate()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
