"""Fail-closed validation for P5 v2 cases and materializations."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.contracts_v2 import P5CaseV2, VARIANT_IDS_V2  # noqa: E402
from evals.trip_check_v1.p5.data_contract import digest, file_sha256, load_jsonl  # noqa: E402
from evals.trip_check_v1.p5.data_contract_v2 import (  # noqa: E402
    BLIND_INPUT_PATH_V2,
    BLIND_MATERIALIZATIONS_PATH_V2,
    BLIND_SEAL_PATH_V2,
    CITIES,
    FAULT_PROFILES,
    JUDGE_RUBRIC_PATH_V2,
    MANIFEST_PATH_V2,
    NONBLIND_MATERIALIZATIONS_PATH_V2,
    NONBLIND_PATH_V2,
    RUN_SPEC_TEMPLATE_PATH_V2,
    SCREENSHOT_COUNTS,
    SPLIT_COUNTS,
    build_manifest_v2,
    case_set_hash,
    legacy_overlap_debt_v2,
    materialization_set_hash,
)
from evals.trip_check_v1.p5.final_blind_scorer_v2 import schema_contract_sha256_v2  # noqa: E402


_PRIVATE = re.compile(
    r"(?:1[3-9]\d{9})|(?:\b\d{17}[\dXx]\b)|(?:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)|"
    r"(?:BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY)|(?:sk-[A-Za-z0-9]{20,})"
)
_BLIND_FORBIDDEN_KEYS = {"oracle", "oracle_sha256", "expected", "answer", "ground_truth", "human_label"}
_MATERIALIZER_FORBIDDEN_KEYS = {"oracle", "oracle_sha256", "expected"}


def _walk_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_walk_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_walk_keys(item) for item in value), set())
    return set()


def _artifact_binding(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact["artifact_id"],
        "schema_version": artifact["schema_version"],
        "content_sha256": artifact["content_sha256"],
    }


def _validate_artifact(artifact: Any, *, case_id: str, field: str, errors: list[str]) -> None:
    if not isinstance(artifact, dict):
        errors.append(f"{case_id}: {field} must be an artifact object")
        return
    if not {"artifact_id", "schema_version", "content_sha256"}.issubset(artifact):
        errors.append(f"{case_id}: {field} lacks artifact binding fields")
        return
    content = {key: value for key, value in artifact.items() if key != "content_sha256"}
    if artifact["content_sha256"] != digest(content):
        errors.append(f"{case_id}: {field} content hash mismatch")


def _validate_case_materialization(
    case: dict[str, Any],
    materialization: dict[str, Any],
    *,
    formal: bool,
    errors: list[str],
) -> None:
    case_id = case["case_id"]
    if materialization.get("case_id") != case_id:
        errors.append(f"{case_id}: materialization case binding mismatch")
        return
    expected_hash = digest({key: value for key, value in materialization.items() if key != "materialization_hash"})
    if materialization.get("materialization_hash") != expected_hash:
        errors.append(f"{case_id}: materialization hash mismatch")
    binding = case["materialization"]
    if binding.get("materialization_id") != materialization.get("materialization_id"):
        errors.append(f"{case_id}: materialization id binding mismatch")
    if binding.get("materialization_sha256") != materialization.get("materialization_hash"):
        errors.append(f"{case_id}: materialization sha binding mismatch")
    if _walk_keys(materialization) & _MATERIALIZER_FORBIDDEN_KEYS:
        errors.append(f"{case_id}: materialization leaks oracle/expected fields")

    for field in ("source_payload", "provider_snapshot", "evidence_snapshot", "fault_script"):
        artifact = materialization.get(field)
        _validate_artifact(artifact, case_id=case_id, field=field, errors=errors)
        if isinstance(artifact, dict) and binding.get(field) != _artifact_binding(artifact):
            errors.append(f"{case_id}: {field} case binding mismatch")
    candidates = materialization.get("candidate_sets")
    if not isinstance(candidates, list):
        errors.append(f"{case_id}: candidate_sets must be an array")
        candidates = []
    for index, candidate in enumerate(candidates):
        _validate_artifact(candidate, case_id=case_id, field=f"candidate_sets[{index}]", errors=errors)
    if binding.get("candidate_sets") != [_artifact_binding(item) for item in candidates if isinstance(item, dict)]:
        errors.append(f"{case_id}: CandidateSet bindings mismatch")

    receipts = materialization.get("receipts")
    if not isinstance(receipts, list):
        errors.append(f"{case_id}: receipts must be an array")
        receipts = []
    provider_receipts = [item for item in receipts if isinstance(item, dict) and "provider" in item]
    if not provider_receipts:
        errors.append(f"{case_id}: every case requires Provider receipts")
    evidence = materialization.get("evidence_snapshot", {}).get("snapshot", {})
    if not evidence.get("facts"):
        errors.append(f"{case_id}: every case requires Evidence facts")
    provider_snapshot_ids = materialization.get("provider_snapshot", {}).get("receipt_ids")
    if provider_snapshot_ids != [item["receipt_id"] for item in provider_receipts]:
        errors.append(f"{case_id}: Provider snapshot receipt ids mismatch")

    candidate_mode = case["runner_control"]["candidate_set_mode"]
    if candidate_mode == "VALID" and not candidates:
        errors.append(f"{case_id}: VALID CandidateSet path has no frozen CandidateSet")
    if candidate_mode != "VALID" and candidates:
        errors.append(f"{case_id}: non-VALID CandidateSet path exposed candidates")
    operations = Counter(item.get("operation") for item in provider_receipts)
    if candidate_mode in {"EMPTY", "MISSING_RECEIPT", "NOT_APPLICABLE"} and operations["route.candidate"]:
        errors.append(f"{case_id}: non-VALID CandidateSet path retained a candidate route receipt")
    if candidate_mode == "VALID" and operations["route.candidate"] < 1:
        errors.append(f"{case_id}: VALID CandidateSet path lacks route receipt")

    render = materialization.get("render_receipt")
    ocr = materialization.get("ocr_baseline_receipt")
    if case["input_kind"] == "TEXT":
        if render is not None or ocr is not None:
            errors.append(f"{case_id}: text case contains OCR artifacts")
        if binding.get("render_receipt") is not None or binding.get("ocr_baseline_receipt") is not None:
            errors.append(f"{case_id}: text case contains OCR bindings")
    else:
        if not isinstance(render, dict) or not isinstance(ocr, dict):
            errors.append(f"{case_id}: screenshot case lacks successful render/OCR receipts")
            return
        expected_render_binding = {
            "artifact_id": f"render-{case_id}",
            "schema_version": render.get("schema_version"),
            "content_sha256": digest(render),
        }
        expected_ocr_binding = {
            "artifact_id": f"ocr-{case_id}",
            "schema_version": ocr.get("schema_version"),
            "content_sha256": digest(ocr),
        }
        if binding.get("render_receipt") != expected_render_binding:
            errors.append(f"{case_id}: render receipt binding mismatch")
        if binding.get("ocr_baseline_receipt") != expected_ocr_binding:
            errors.append(f"{case_id}: OCR receipt binding mismatch")
        cleanup = [
            item
            for item in receipts
            if isinstance(item, dict) and item.get("schema_version") == "trip-check-p5-cleanup-receipt-v2"
        ]
        if len(cleanup) != 1 or cleanup[0].get("cleanup_status") != "DELETED" or cleanup[0].get("original_removed") is not True:
            errors.append(f"{case_id}: screenshot cleanup must be DELETED with original removed")
        if formal and (ocr.get("engine"), ocr.get("engine_version")) != ("paddleocr", "3.7.0"):
            errors.append(f"{case_id}: formal validation requires actual paddleocr 3.7.0 receipt")


def _cross_split_overlap(rows: list[dict[str, Any]], extractor: Any) -> dict[str, list[str]]:
    values: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        values[row["split"]].add(str(extractor(row)))
    overlaps: dict[str, list[str]] = {}
    splits = list(SPLIT_COUNTS)
    for left_index, left in enumerate(splits):
        for right in splits[left_index + 1 :]:
            intersection = sorted(values[left] & values[right])
            if intersection:
                overlaps[f"{left}:{right}"] = intersection
    return overlaps


def validate(*, formal: bool = True, root: Path | None = None) -> dict[str, Any]:
    del root  # v2 has one fixed, repository-relative generation path
    errors: list[str] = []
    required = (
        NONBLIND_PATH_V2,
        BLIND_INPUT_PATH_V2,
        NONBLIND_MATERIALIZATIONS_PATH_V2,
        BLIND_MATERIALIZATIONS_PATH_V2,
        MANIFEST_PATH_V2,
        RUN_SPEC_TEMPLATE_PATH_V2,
        JUDGE_RUBRIC_PATH_V2,
        NONBLIND_PATH_V2.parent / "case_v2.schema.json",
        NONBLIND_PATH_V2.parent / "materialization_v2.schema.json",
        NONBLIND_PATH_V2.parent / "oracle_v2.schema.json",
    )
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        return {"schema_version": "trip-check-p5-dataset-validation-v2", "status": "FAIL", "errors": [f"missing required files: {missing}"]}

    nonblind_cases = load_jsonl(NONBLIND_PATH_V2)
    blind_cases = load_jsonl(BLIND_INPUT_PATH_V2)
    nonblind_materializations = load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V2)
    blind_materializations = load_jsonl(BLIND_MATERIALIZATIONS_PATH_V2)
    all_cases = [*nonblind_cases, *blind_cases]
    all_materializations = [*nonblind_materializations, *blind_materializations]
    materialization_schema = json.loads(
        (NONBLIND_PATH_V2.parent / "materialization_v2.schema.json").read_text(encoding="utf-8")
    )
    materialization_validator = Draft202012Validator(materialization_schema)

    for row in all_cases:
        case_id = row.get("case_id", "<missing>")
        try:
            P5CaseV2.model_validate(row)
        except Exception as exc:
            errors.append(f"{case_id}: case contract {exc}")
        if row.get("case_hash") != digest({key: value for key, value in row.items() if key != "case_hash"}):
            errors.append(f"{case_id}: case hash mismatch")
        if row.get("normalized_input_sha256") != digest(row.get("product_input")):
            errors.append(f"{case_id}: normalized input hash mismatch")
        product_input = row.get("product_input") or {}
        privacy_text = product_input.get("raw_text") or product_input.get("source_text") or ""
        if _PRIVATE.search(str(privacy_text)):
            errors.append(f"{case_id}: possible private or secret content")
        if row.get("split") == "frozen_blind" and _walk_keys(row) & _BLIND_FORBIDDEN_KEYS:
            errors.append(f"{case_id}: blind input exposes label fields")

    materialization_by_case: dict[str, dict[str, Any]] = {}
    for row in all_materializations:
        case_id = row.get("case_id", "<missing>")
        if case_id in materialization_by_case:
            errors.append(f"{case_id}: duplicate materialization")
        materialization_by_case[case_id] = row
        for schema_error in materialization_validator.iter_errors(row):
            errors.append(f"{case_id}: materialization schema {schema_error.message}")
    if set(materialization_by_case) != {row["case_id"] for row in all_cases}:
        errors.append("case/materialization case-id sets differ")
    for case in all_cases:
        materialization = materialization_by_case.get(case["case_id"])
        if materialization is not None:
            _validate_case_materialization(case, materialization, formal=formal, errors=errors)

    by_split = Counter(row["split"] for row in all_cases)
    by_city = Counter(row["city"] for row in all_cases)
    screenshots_by_split = Counter(
        row["split"] for row in all_cases if row["input_kind"] == "SYNTHETIC_SCREENSHOT"
    )
    if dict(by_split) != SPLIT_COUNTS:
        errors.append(f"split counts must be {SPLIT_COUNTS}, got {dict(by_split)}")
    if by_city != Counter({city: 120 for city in CITIES}):
        errors.append(f"city counts must be 120 each, got {dict(by_city)}")
    normalized_screenshots = {split: screenshots_by_split.get(split, 0) for split in SPLIT_COUNTS}
    if normalized_screenshots != SCREENSHOT_COUNTS:
        errors.append(f"screenshot counts must be {SCREENSHOT_COUNTS}, got {normalized_screenshots}")
    ids = [row["case_id"] for row in all_cases]
    if len(ids) != len(set(ids)):
        errors.append("case IDs must be unique")

    blind = blind_cases
    for city in CITIES:
        city_rows = [row for row in blind if row["city"] == city]
        if Counter(row["input_kind"] for row in city_rows) != Counter({"TEXT": 15, "SYNTHETIC_SCREENSHOT": 15}):
            errors.append(f"{city}: blind text/screenshot distribution must be 15/15")
        if Counter(row["difficulty"] for row in city_rows) != Counter({"CLEAN": 10, "MEDIUM": 10, "HARD": 10}):
            errors.append(f"{city}: blind difficulty distribution must be 10 each")
    if Counter(row["runner_control"]["fault_profile_id"] for row in blind) != Counter({item: 10 for item in FAULT_PROFILES}):
        errors.append("blind fault profiles must contain 10 cases each")
    if sum(bool(row["runner_control"]["unknown_required"]) for row in blind) != 18:
        errors.append("blind unknown_required count must be 18")
    candidate_counts = Counter(row["runner_control"]["candidate_set_mode"] for row in blind)
    if {key: candidate_counts[key] for key in ("VALID", "EMPTY", "MISSING_RECEIPT")} != {
        "VALID": 10,
        "EMPTY": 10,
        "MISSING_RECEIPT": 10,
    }:
        errors.append("blind CandidateSet paths must be valid/empty/missing-receipt 10 each")
    if sum(row["runner_control"]["fault_profile_id"] in {"duplicate_apply", "concurrent_apply"} for row in blind) != 20:
        errors.append("blind concurrency count must be 20")

    for name, extractor in (
        ("normalized_input", lambda row: row["normalized_input_sha256"]),
        ("content_family", lambda row: row["lineage"]["content_family_id"]),
        ("mutation_ancestry", lambda row: row["lineage"]["mutation_ancestry_id"]),
    ):
        overlap = _cross_split_overlap(all_cases, extractor)
        if overlap:
            errors.append(f"{name} overlaps across splits: {sorted(overlap)}")

    manifest = json.loads(MANIFEST_PATH_V2.read_text(encoding="utf-8"))
    manifest_mode = manifest.get("generation", {}).get("ocr_mode")
    if manifest_mode not in {"development", "actual"}:
        errors.append("manifest OCR mode is invalid")
        manifest_mode = "development"
    if formal and manifest_mode != "actual":
        errors.append("formal validation rejects development OCR artifacts")
    expected_manifest = build_manifest_v2(
        nonblind_cases=nonblind_cases,
        blind_cases=blind_cases,
        nonblind_materializations=nonblind_materializations,
        blind_materializations=blind_materializations,
        ocr_mode=manifest_mode,
        sealing_commitment=manifest.get("sealing_commitment"),
    )
    if manifest != expected_manifest:
        errors.append("dataset manifest differs from bound files and contracts")
    if manifest.get("manifest_hash") != digest({key: value for key, value in manifest.items() if key != "manifest_hash"}):
        errors.append("manifest hash mismatch")
    sealing_commitment = manifest.get("sealing_commitment")
    if sealing_commitment is not None:
        required_commitment_fields = {
            "schema_version",
            "status",
            "candidate_freeze_commit",
            "candidate_dataset_manifest_hash",
            "blind_seal_path",
            "blind_seal_v2_sha256",
            "labels_canonical_sha256",
            "external_bundle_sha256",
            "review_receipt_sha256",
        }
        if not isinstance(sealing_commitment, dict) or set(sealing_commitment) != required_commitment_fields:
            errors.append("sealing commitment has invalid or extra fields")
        else:
            candidate_manifest = build_manifest_v2(
                nonblind_cases=nonblind_cases,
                blind_cases=blind_cases,
                nonblind_materializations=nonblind_materializations,
                blind_materializations=blind_materializations,
                ocr_mode=manifest_mode,
            )
            expected_seal_path = BLIND_SEAL_PATH_V2.relative_to(BACKEND_ROOT.parent).as_posix()
            if (
                sealing_commitment.get("schema_version") != "trip-check-p5-sealing-commitment-v2"
                or sealing_commitment.get("status") != "SEALED"
                or not re.fullmatch(r"[0-9a-f]{40}", str(sealing_commitment.get("candidate_freeze_commit", "")))
                or sealing_commitment.get("candidate_dataset_manifest_hash") != candidate_manifest["manifest_hash"]
                or sealing_commitment.get("blind_seal_path") != expected_seal_path
            ):
                errors.append("sealing commitment candidate or path binding mismatch")
            if not BLIND_SEAL_PATH_V2.is_file():
                errors.append("sealing commitment blind seal is missing")
            else:
                try:
                    seal = json.loads(BLIND_SEAL_PATH_V2.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    errors.append(f"sealing commitment blind seal is unreadable: {exc}")
                else:
                    seal_schema = json.loads(
                        (BLIND_INPUT_PATH_V2.parent / "blind_seal_v2.schema.json").read_text(encoding="utf-8")
                    )
                    for schema_error in Draft202012Validator(seal_schema).iter_errors(seal):
                        errors.append(f"blind seal schema: {schema_error.message}")
                    expected_seal_bindings = {
                        "schema_version": "trip-check-p5-blind-seal-v2",
                        "split": "frozen_blind",
                        "case_count": 90,
                        "case_ids_sha256": digest(sorted(row["case_id"] for row in blind_cases)),
                        "inputs_file_sha256": file_sha256(BLIND_INPUT_PATH_V2),
                        "inputs_content_sha256": digest(blind_cases),
                        "materializations_file_sha256": file_sha256(BLIND_MATERIALIZATIONS_PATH_V2),
                        "materializations_content_sha256": digest(blind_materializations),
                        "schema_contract_sha256": schema_contract_sha256_v2(BACKEND_ROOT.parent),
                        "run_spec_template_sha256": file_sha256(RUN_SPEC_TEMPLATE_PATH_V2),
                        "rubric_sha256": file_sha256(JUDGE_RUBRIC_PATH_V2),
                        "variant_ids_sha256": digest(list(VARIANT_IDS_V2)),
                    }
                    if any(seal.get(key) != value for key, value in expected_seal_bindings.items()):
                        errors.append("blind seal differs from frozen dataset and contract bytes")
                    seal_bindings = {
                        "blind_seal_v2_sha256": file_sha256(BLIND_SEAL_PATH_V2),
                        "labels_canonical_sha256": seal.get("labels_canonical_sha256"),
                        "external_bundle_sha256": seal.get("external_bundle_sha256"),
                        "review_receipt_sha256": seal.get("review_receipt_sha256"),
                    }
                    if any(sealing_commitment.get(key) != value for key, value in seal_bindings.items()):
                        errors.append("sealing commitment differs from blind seal readback")
    for key, path, rows in (
        ("nonblind_cases", NONBLIND_PATH_V2, nonblind_cases),
        ("blind_cases", BLIND_INPUT_PATH_V2, blind_cases),
        ("nonblind_materializations", NONBLIND_MATERIALIZATIONS_PATH_V2, nonblind_materializations),
        ("blind_materializations", BLIND_MATERIALIZATIONS_PATH_V2, blind_materializations),
    ):
        entry = manifest.get("files", {}).get(key, {})
        if entry.get("file_sha256") != file_sha256(path) or entry.get("content_sha256") != digest(rows):
            errors.append(f"manifest file binding mismatch: {key}")
    if manifest.get("lanes", {}).get("nonblind", {}).get("case_set_hash") != case_set_hash(nonblind_cases):
        errors.append("nonblind case_set_hash mismatch")
    if manifest.get("lanes", {}).get("frozen_blind", {}).get("case_set_hash") != case_set_hash(blind_cases):
        errors.append("blind case_set_hash mismatch")
    if manifest.get("lanes", {}).get("nonblind", {}).get("materialization_set_hash") != materialization_set_hash(nonblind_materializations):
        errors.append("nonblind materialization_set_hash mismatch")
    if manifest.get("lanes", {}).get("frozen_blind", {}).get("materialization_set_hash") != materialization_set_hash(blind_materializations):
        errors.append("blind materialization_set_hash mismatch")
    debt = legacy_overlap_debt_v2()
    if debt["regression_fixture_hashes_overlapping_dev"] != 72 or debt["regression_oracle_hashes_overlapping_dev"] != 72:
        errors.append("P4 72/72 overlap debt changed")

    run_spec = json.loads(RUN_SPEC_TEMPLATE_PATH_V2.read_text(encoding="utf-8"))
    rubric = json.loads(JUDGE_RUBRIC_PATH_V2.read_text(encoding="utf-8"))
    if run_spec.get("schema_version") != "trip-check-p5-run-spec-v2":
        errors.append("run spec template is not v2")
    if run_spec.get("ocr_engine") != {"name": "paddleocr", "version": "3.7.0"}:
        errors.append("run spec must freeze paddleocr 3.7.0")
    if rubric.get("schema_version") != "trip-check-p5-judge-rubric-v2":
        errors.append("Judge rubric is not v2")

    return {
        "schema_version": "trip-check-p5-dataset-validation-v2",
        "status": "PASS" if not errors else "FAIL",
        "formal": formal,
        "errors": errors,
        "counts": {
            "total": len(all_cases),
            "by_split": dict(sorted(by_split.items())),
            "by_city": dict(sorted(by_city.items())),
            "screenshots_by_split": normalized_screenshots,
        },
        "blind": {
            "label_payload_in_repository": False,
            "unknown_required": sum(bool(row["runner_control"]["unknown_required"]) for row in blind),
            "fault_profiles": dict(sorted(Counter(row["runner_control"]["fault_profile_id"] for row in blind).items())),
            "candidate_set_paths": dict(sorted(candidate_counts.items())),
            "concurrency": sum(
                row["runner_control"]["fault_profile_id"] in {"duplicate_apply", "concurrent_apply"} for row in blind
            ),
        },
        "legacy_overlap_debt": debt,
        "manifest_hash": manifest.get("manifest_hash"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-development-ocr",
        action="store_true",
        help="development-only structural validation; formal validation rejects these receipts",
    )
    args = parser.parse_args()
    result = validate(formal=not args.allow_development_ocr)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
