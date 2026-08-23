"""Validate P5 executable inputs, blind isolation, lineage and commitments."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from evals.trip_check_v1.p5.data_contract import (
    BLIND_INPUT_PATH,
    BLIND_SEAL_PATH,
    CITIES,
    CITY_TOTAL,
    MANIFEST_PATH,
    NONBLIND_PATH,
    P5_ROOT,
    SPLIT_COUNTS,
    build_blind_inputs,
    build_manifest,
    build_nonblind_cases,
    digest,
    load_jsonl,
)


_SHA = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE = re.compile(
    r"(?:1[3-9]\d{9})|(?:\b\d{17}[\dXx]\b)|(?:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)|"
    r"(?:BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY)|(?:sk-[A-Za-z0-9]{20,})"
)
_BLIND_FORBIDDEN_KEYS = {"oracle", "expected", "answer", "ground_truth", "human_label"}


def _walk_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_walk_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_walk_keys(item) for item in value), set())
    return set()


def _validate_case(row: dict[str, Any], *, blind: bool, errors: list[str]) -> None:
    case_id = row.get("case_id", "<missing>")
    body = {key: value for key, value in row.items() if key != "case_hash"}
    if row.get("case_hash") != digest(body):
        errors.append(f"{case_id}: case hash mismatch")
    if row.get("normalized_input_sha256") != digest(row.get("product_input")):
        errors.append(f"{case_id}: normalized input hash mismatch")
    if not 2 <= row.get("trip_days", 0) <= 5:
        errors.append(f"{case_id}: trip days outside 2-5")
    if not 2 <= row.get("group_size", 0) <= 5:
        errors.append(f"{case_id}: group size outside 2-5")
    if blind:
        exposed = _walk_keys(row) & _BLIND_FORBIDDEN_KEYS
        if exposed:
            errors.append(f"{case_id}: blind input exposes {sorted(exposed)}")
    else:
        if "oracle" not in row or row.get("oracle_sha256") != digest(row.get("oracle")):
            errors.append(f"{case_id}: non-blind oracle binding missing")
    if row.get("provenance", {}).get("contains_human_data") is not False:
        errors.append(f"{case_id}: privacy declaration missing")
    product_input = row.get("product_input") or {}
    privacy_surface = {
        "case_id": case_id,
        "text": product_input.get("raw_text") or product_input.get("ocr_text") or "",
    }
    if _PRIVATE.search(json.dumps(privacy_surface, ensure_ascii=False, sort_keys=True)):
        errors.append(f"{case_id}: possible private or secret content")


def validate(root: Path = P5_ROOT) -> dict[str, Any]:
    errors: list[str] = []
    required = [
        root / "case.schema.json",
        root / "blind_seal.schema.json",
        root / "blind_bundle.schema.json",
        root / "judge_rubric_v1.json",
        root / "run_spec_template_v1.json",
        NONBLIND_PATH,
        BLIND_INPUT_PATH,
        BLIND_SEAL_PATH,
        MANIFEST_PATH,
    ]
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        return {"status": "FAIL", "errors": [f"missing required files: {missing}"]}
    nonblind = load_jsonl(NONBLIND_PATH)
    blind = load_jsonl(BLIND_INPUT_PATH)
    case_schema = json.loads((root / "case.schema.json").read_text(encoding="utf-8"))
    case_validator = Draft202012Validator(case_schema)
    if nonblind != build_nonblind_cases():
        errors.append("checked-in non-blind cases differ from deterministic materializer")
    if blind != build_blind_inputs():
        errors.append("checked-in blind inputs differ from deterministic materializer")
    for row in nonblind:
        for error in case_validator.iter_errors(row):
            errors.append(f"{row.get('case_id', '<missing>')}: schema {error.message}")
        _validate_case(row, blind=False, errors=errors)
    for row in blind:
        for error in case_validator.iter_errors(row):
            errors.append(f"{row.get('case_id', '<missing>')}: schema {error.message}")
        _validate_case(row, blind=True, errors=errors)
    by_split = Counter(row["split"] for row in [*nonblind, *blind])
    if dict(by_split) != SPLIT_COUNTS:
        errors.append(f"invalid split counts: {dict(by_split)}")
    by_city = Counter(row["city"] for row in [*nonblind, *blind])
    if by_city != Counter({city: CITY_TOTAL for city in CITIES}):
        errors.append(f"invalid city counts: {dict(by_city)}")
    ids = [row["case_id"] for row in [*nonblind, *blind]]
    if len(ids) != len(set(ids)):
        errors.append("case IDs must be unique")
    input_hashes = [row["normalized_input_sha256"] for row in [*nonblind, *blind]]
    if len(input_hashes) != len(set(input_hashes)):
        errors.append("normalized product inputs overlap across splits")
    families = [row["lineage"]["source_family_id"] for row in [*nonblind, *blind]]
    if len(families) != len(set(families)):
        errors.append("source families overlap across splits")
    if Counter(row["input_kind"] for row in blind) != Counter({"TEXT": 45, "SYNTHETIC_SCREENSHOT": 45}):
        errors.append("blind input kinds must be 45/45")
    if Counter(row["difficulty"] for row in blind) != Counter({"CLEAN": 30, "MEDIUM": 30, "HARD": 30}):
        errors.append("blind difficulty distribution must be 30/30/30")
    seal = json.loads(BLIND_SEAL_PATH.read_text(encoding="utf-8"))
    seal_schema = json.loads((root / "blind_seal.schema.json").read_text(encoding="utf-8"))
    for error in Draft202012Validator(seal_schema).iter_errors(seal):
        errors.append(f"blind seal schema: {error.message}")
    for key in (
        "case_ids_sha256",
        "inputs_file_sha256",
        "inputs_content_sha256",
        "labels_canonical_sha256",
        "external_bundle_sha256",
        "rubric_sha256",
        "run_spec_template_sha256",
        "variant_ids_sha256",
        "review_receipt_sha256",
    ):
        if not _SHA.fullmatch(str(seal.get(key, ""))):
            errors.append(f"blind seal {key} is not a concrete SHA-256")
    if seal.get("case_count") != 90 or seal.get("case_ids_sha256") != digest(sorted(row["case_id"] for row in blind)):
        errors.append("blind seal case binding mismatch")
    if seal.get("scoring_payload_present") is not False or seal.get("human_evidence") is not False:
        errors.append("blind seal evidence boundary is invalid")
    if seal.get("label_storage") != "external_bundle_only" or seal.get("label_access") != "isolated_scorer_only":
        errors.append("blind labels must remain external and isolated")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    expected_manifest = build_manifest(nonblind, blind, seal)
    if manifest != expected_manifest:
        errors.append("dataset manifest differs from current files and commitments")
    debt = manifest.get("legacy_overlap_debt", {})
    if debt.get("regression_fixture_hashes_overlapping_dev") != 72:
        errors.append("P4 fixture overlap debt must remain explicitly recorded as 72")
    if debt.get("regression_oracle_hashes_overlapping_dev") != 72:
        errors.append("P4 oracle overlap debt must remain explicitly recorded as 72")
    return {
        "schema_version": "trip-check-p5-dataset-validation-v1",
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "counts": {"total": len(nonblind) + len(blind), "by_split": dict(sorted(by_split.items())), "by_city": dict(sorted(by_city.items()))},
        "blind": {
            "label_payload_in_repository": False,
            "case_count": len(blind),
            "case_ids_sha256": digest(sorted(row["case_id"] for row in blind)),
        },
        "legacy_overlap_debt": debt,
        "manifest_hash": manifest.get("manifest_hash"),
    }


def main() -> None:
    result = validate()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
