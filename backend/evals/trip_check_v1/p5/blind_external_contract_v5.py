"""Label-free repository binding for the corrected external P5 v5 oracle.

The label bundle schema remains v2; v5 adds correction and independent-review
receipts.  The repository binding proves byte identity to v4 before any
external oracle artifact is accepted.
"""

from __future__ import annotations

import json
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p5.blind_external_contract_v2 import (
    P5ExternalCustodyContractError,
    _read_external_json,
    _read_schema,
    validate_external_blind_bundle_v2,
)
from evals.trip_check_v1.p5.contracts_v3 import VARIANT_IDS_V3
from evals.trip_check_v1.p5.data_contract import (
    canonical_bytes,
    digest,
    file_sha256,
    load_jsonl,
)
from evals.trip_check_v1.p5.data_contract_v5 import (
    BLIND_INPUT_PATH_V5,
    BLIND_MATERIALIZATIONS_PATH_V5,
    MANIFEST_PATH_V5,
    P5DataContractErrorV5,
    RUN_SPEC_TEMPLATE_PATH_V5,
    validate_manifest_v5,
    validate_v4_source_anchor,
)
from evals.trip_check_v1.p5.final_blind_scorer_v2 import schema_contract_sha256_v2
from jsonschema import Draft202012Validator


RUBRIC_PATH_V5 = Path(__file__).resolve().parent / "judge_rubric_v2.json"
BUNDLE_SCHEMA_ID_V5 = "trip-check-p5-blind-label-bundle-v2"
CORRECTION_RECEIPT_SCHEMA_ID_V5 = (
    "trip-check-p5-blind-oracle-correction-receipt-v5"
)
REVIEW_RECEIPT_SCHEMA_ID_V5 = "trip-check-p5-blind-review-receipt-v5"
CORRECTION_RECEIPT_SCHEMA_RELATIVE_V5 = Path(
    "backend/evals/trip_check_v1/p5/blind_oracle_correction_receipt_v5.schema.json"
)
REVIEW_RECEIPT_SCHEMA_RELATIVE_V5 = Path(
    "backend/evals/trip_check_v1/p5/blind_review_receipt_v5.schema.json"
)


def expected_blind_dataset_binding_v5(
    repo_root: Path,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Recompute public v5 blind commitments without reading an oracle."""

    try:
        manifest_state = json.loads(MANIFEST_PATH_V5.read_text(encoding="utf-8"))
        validate_manifest_v5(
            repo_root,
            manifest_path=MANIFEST_PATH_V5,
            require_sealed=manifest_state.get("seal_status") == "SEALED",
        )
        manifest = json.loads(MANIFEST_PATH_V5.read_text(encoding="utf-8"))
        inputs = load_jsonl(BLIND_INPUT_PATH_V5)
        materializations = load_jsonl(BLIND_MATERIALIZATIONS_PATH_V5)
    except (OSError, UnicodeError, json.JSONDecodeError, P5DataContractErrorV5) as exc:
        raise P5ExternalCustodyContractError(
            "P5 v5 dataset binding artifacts are unreadable"
        ) from exc
    case_ids = tuple(str(row.get("case_id", "")) for row in inputs)
    materialization_ids = {str(row.get("case_id", "")) for row in materializations}
    if (
        len(inputs) != 90
        or len(materializations) != 90
        or len(set(case_ids)) != 90
        or materialization_ids != set(case_ids)
    ):
        raise P5ExternalCustodyContractError(
            "P5 v5 blind dataset does not have exact 90-case coverage"
        )
    if not isinstance(manifest, Mapping) or (
        manifest.get("schema_version") != "trip-check-p5-dataset-manifest-v5"
        or manifest.get("dataset_id") != "trip-check-p5-360-v5"
        or manifest.get("manifest_hash")
        != digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
        or manifest.get("seal_status") not in {"PENDING_V5_SEAL", "SEALED"}
    ):
        raise P5ExternalCustodyContractError("P5 v5 dataset manifest is invalid")
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise P5ExternalCustodyContractError("P5 v5 dataset file index is missing")
    for name, path, rows in (
        ("blind_cases", BLIND_INPUT_PATH_V5, inputs),
        ("blind_materializations", BLIND_MATERIALIZATIONS_PATH_V5, materializations),
    ):
        entry = files.get(name)
        if not isinstance(entry, Mapping) or (
            entry.get("row_count") != 90
            or entry.get("file_sha256") != file_sha256(path)
            or entry.get("content_sha256") != digest(rows)
        ):
            raise P5ExternalCustodyContractError(
                f"P5 v5 dataset file binding is stale: {name}"
            )
    binding = {
        "case_count": 90,
        "case_ids_sha256": digest(sorted(case_ids)),
        "inputs_file_sha256": file_sha256(BLIND_INPUT_PATH_V5),
        "inputs_content_sha256": digest(inputs),
        "materializations_file_sha256": file_sha256(
            BLIND_MATERIALIZATIONS_PATH_V5
        ),
        "materializations_content_sha256": digest(materializations),
        "schema_contract_sha256": schema_contract_sha256_v2(
            Path(__file__).resolve().parents[4]
        ),
        "run_spec_template_sha256": file_sha256(RUN_SPEC_TEMPLATE_PATH_V5),
        "rubric_sha256": file_sha256(RUBRIC_PATH_V5),
        "variant_ids_sha256": digest(list(VARIANT_IDS_V3)),
    }
    return binding, tuple(sorted(case_ids))


def _validate_receipt(
    *,
    repo_root: Path,
    path: Path,
    expected_sha256: str,
    schema_relative: Path,
) -> tuple[dict[str, Any], str]:
    receipt, payload = _read_external_json(repo_root, path, expected_sha256)
    errors = sorted(
        Draft202012Validator(_read_schema(repo_root, schema_relative)).iter_errors(
            receipt
        ),
        key=lambda item: list(item.path),
    )
    if errors:
        raise P5ExternalCustodyContractError("P5 v5 custody receipt schema mismatch")
    if receipt.get("receipt_sha256") != digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    ):
        raise P5ExternalCustodyContractError("P5 v5 custody receipt self-hash mismatch")
    if payload != canonical_bytes(receipt) + b"\n":
        raise P5ExternalCustodyContractError("P5 v5 custody receipt is not canonical JSON")
    return receipt, hashlib.sha256(payload).hexdigest()


def validate_external_custody_v5(
    *,
    repo_root: Path,
    external_bundle_path: Path,
    external_correction_receipt_path: Path,
    external_review_receipt_path: Path,
    expected_bundle_sha256: str,
    expected_labels_canonical_sha256: str,
    expected_correction_receipt_sha256: str,
    expected_review_receipt_sha256: str,
    candidate_subject_commit: str,
    source_bundle_sha256: str,
    source_labels_canonical_sha256: str,
) -> dict[str, str]:
    """Validate the corrected bundle and two independent aggregate receipts."""

    binding, case_ids = expected_blind_dataset_binding_v5(repo_root)
    source_anchor = validate_v4_source_anchor(repo_root)
    if (
        source_bundle_sha256 != source_anchor["external_bundle_sha256"]
        or source_labels_canonical_sha256
        != source_anchor["labels_canonical_sha256"]
    ):
        raise P5ExternalCustodyContractError(
            "P5 v5 source oracle hashes do not match the sealed v4 anchor"
        )
    bundle = validate_external_blind_bundle_v2(
        repo_root=repo_root,
        bundle_path=external_bundle_path,
        expected_bundle_sha256=expected_bundle_sha256,
        expected_labels_canonical_sha256=expected_labels_canonical_sha256,
        expected_dataset_binding=binding,
        expected_case_ids=case_ids,
    )
    correction, correction_file_hash = _validate_receipt(
        repo_root=repo_root,
        path=external_correction_receipt_path,
        expected_sha256=expected_correction_receipt_sha256,
        schema_relative=CORRECTION_RECEIPT_SCHEMA_RELATIVE_V5,
    )
    review, review_file_hash = _validate_receipt(
        repo_root=repo_root,
        path=external_review_receipt_path,
        expected_sha256=expected_review_receipt_sha256,
        schema_relative=REVIEW_RECEIPT_SCHEMA_RELATIVE_V5,
    )
    shared = {
        "candidate_subject_commit": candidate_subject_commit,
        "source_bundle_sha256": source_bundle_sha256,
        "source_labels_canonical_sha256": source_labels_canonical_sha256,
        "corrected_bundle_byte_sha256": bundle["bundle_byte_sha256"],
        "corrected_bundle_canonical_sha256": bundle["bundle_canonical_sha256"],
        "corrected_labels_canonical_sha256": bundle["labels_canonical_sha256"],
        "dataset_binding": binding,
        "correction_scope": "specific_place_allowed_payload_policy_only",
        "non_target_oracle_diff_count": 0,
        "blind_payload_changed": False,
    }
    for key, value in shared.items():
        if correction.get(key) != value or review.get(key) != value:
            raise P5ExternalCustodyContractError(
                f"P5 v5 custody receipt binding mismatch: {key}"
            )
    if (
        correction.get("changed_label_count") != 60
        or correction.get("changed_field")
        != "oracle.specific_place_allowed"
        or review.get("reviewed_changed_label_count") != 60
        or review.get("reviewed_changed_field")
        != "oracle.specific_place_allowed"
        or review.get("correction_receipt_sha256") != correction_file_hash
        or review.get("policy_mapping_sha256")
        != correction.get("policy_mapping_sha256")
        or review.get("correction_tool_sha256")
        != correction.get("correction_tool_sha256")
        or review.get("reviewer_tool_sha256")
        == correction.get("correction_tool_sha256")
        or bundle["labels_canonical_sha256"] == source_labels_canonical_sha256
    ):
        raise P5ExternalCustodyContractError("P5 v5 correction/review proof mismatch")
    return {
        "labels_canonical_sha256": bundle["labels_canonical_sha256"],
        "external_bundle_sha256": bundle["bundle_byte_sha256"],
        "correction_receipt_sha256": correction_file_hash,
        "review_receipt_sha256": review_file_hash,
        "policy_mapping_sha256": str(correction["policy_mapping_sha256"]),
    }


__all__ = [
    "BUNDLE_SCHEMA_ID_V5",
    "CORRECTION_RECEIPT_SCHEMA_ID_V5",
    "REVIEW_RECEIPT_SCHEMA_ID_V5",
    "expected_blind_dataset_binding_v5",
    "validate_external_custody_v5",
]
