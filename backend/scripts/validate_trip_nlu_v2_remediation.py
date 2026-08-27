from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.trip_intake.models import TripIntakeExtraction, validate_extraction_evidence
from scripts.generate_trip_nlu_v2_remediation import (
    OUTPUT_ROOT,
    REGRESSION_SOURCE_IDS,
    SOURCE_ROOT,
    _assert_isolation,
    _read_jsonl,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_cases(cases: list[dict[str, Any]], prefix: str) -> None:
    if len({item["case_id"] for item in cases}) != len(cases):
        raise ValueError("remediation case IDs must be unique")
    for item in cases:
        if not item["case_id"].startswith(prefix):
            raise ValueError(f"unexpected remediation case namespace: {item['case_id']}")
        extraction = TripIntakeExtraction.model_validate(item["expected"])
        validate_extraction_evidence(
            extraction, {item["source_id"]: item["input_text"]}
        )


def validate() -> dict[str, Any]:
    manifest = json.loads((OUTPUT_ROOT / "manifest.json").read_text(encoding="utf-8"))
    regression = _read_jsonl(OUTPUT_ROOT / "regression.jsonl")
    validation = _read_jsonl(OUTPUT_ROOT / "validation_v2.jsonl")
    if len(regression) != len(REGRESSION_SOURCE_IDS) or len(validation) != 24:
        raise ValueError(
            "remediation pack must contain every exposed regression and 24 validation cases"
        )
    _validate_cases(regression, "TRIP_NLU_REG_")
    _validate_cases(validation, "TRIP_NLU_RV2_")
    if tuple(
        item["annotation"].get("regression_source_case_id") for item in regression
    ) != REGRESSION_SOURCE_IDS:
        raise ValueError("regression provenance does not match rejected Validation cases")

    original_families = {
        item["family_id"]
        for item in _read_jsonl(SOURCE_ROOT / "family_registry.jsonl")
    }
    new_families = {
        item["annotation"]["source_family_id"] for item in validation
    }
    if original_families & new_families:
        raise ValueError("validation_v2 reuses an original source family")
    if len(new_families) != 8:
        raise ValueError("validation_v2 must contain eight isolated three-case families")
    if any(
        sum(
            candidate["annotation"]["source_family_id"] == family
            for candidate in validation
        )
        != 3
        for family in new_families
    ):
        raise ValueError("each validation_v2 family must contain exactly three cases")

    maximum_similarity = _assert_isolation(validation)
    if round(maximum_similarity, 6) != manifest["validation_max_similarity_to_original_120"]:
        raise ValueError("validation_v2 similarity receipt mismatch")
    for name, expected in manifest["files"].items():
        if _sha256(OUTPUT_ROOT / name) != expected:
            raise ValueError(f"remediation file hash mismatch: {name}")

    original_manifest = json.loads(
        (SOURCE_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    blind_input_hash = _sha256(SOURCE_ROOT / "frozen_blind.inputs.jsonl")
    if blind_input_hash != original_manifest["files"]["frozen_blind.inputs.jsonl"]:
        raise ValueError("original frozen blind input binding changed")
    return {
        "schema_version": "trip-nlu-v2-remediation-validation-receipt-v1",
        "structurally_valid": True,
        "regression_count": len(regression),
        "validation_count": len(validation),
        "validation_family_count": len(new_families),
        "validation_max_similarity_to_original_120": round(maximum_similarity, 6),
        "original_frozen_blind_input_sha256": blind_input_hash,
        "original_frozen_blind_modified": False,
        "blind_labels_read": False,
    }


def main() -> None:
    print(json.dumps(validate(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
