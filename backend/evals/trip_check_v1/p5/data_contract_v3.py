"""Build P5 v3 cases by rebinding the immutable, sealed v2 source dataset.

This module never runs OCR and never reads blind labels.  Screenshot cases reuse
the exact render/OCR/cleanup receipts already admitted by the v2 seal, while the
label-free evidence layer is rebuilt under the stricter v3 semantic contract.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p5.data_contract import (
    BACKEND_ROOT,
    P5_ROOT,
    digest,
    file_sha256,
    load_jsonl,
    write_json,
    write_jsonl,
)
from evals.trip_check_v1.p5.contracts_v3 import (
    P5BlindSealV3,
    P5CaseV3,
    P5SealingCommitmentV3,
    VARIANT_IDS_V3,
)
from evals.trip_check_v1.p5.data_contract_v2 import (
    BLIND_INPUT_PATH_V2,
    BLIND_MATERIALIZATIONS_PATH_V2,
    BLIND_SEAL_PATH_V2,
    JUDGE_RUBRIC_PATH_V2,
    MANIFEST_PATH_V2,
    NONBLIND_MATERIALIZATIONS_PATH_V2,
    NONBLIND_PATH_V2,
)
from evals.trip_check_v1.p5.evidence_materialization_v3 import (
    PROVIDER_SNAPSHOT_ID_V3,
    build_evidence_materialization_v3,
    validate_evidence_materialization_v3,
)
from evals.trip_check_v1.p5.semantic_contract_v3 import (
    validate_dataset_semantics_v3,
    validate_nonblind_oracle_compatibility_v3,
)


CASE_SCHEMA_VERSION_V3 = "trip-check-p5-eval-case-v3"
MATERIALIZATION_SCHEMA_VERSION_V3 = "trip-check-p5-materialization-v3"
MANIFEST_SCHEMA_VERSION_V3 = "trip-check-p5-dataset-manifest-v3"
DATASET_ID_V3 = "trip-check-p5-360-v3"
GENERATOR_VERSION_V3 = "p5-dataset-rebinder-v3"
HASH_POLICY_VERSION_V3 = "p5-canonical-json-nfc-lf-v3"

NONBLIND_PATH_V3 = P5_ROOT / "cases_nonblind_v3.jsonl"
BLIND_INPUT_PATH_V3 = P5_ROOT / "frozen_blind.v3.inputs.jsonl"
NONBLIND_MATERIALIZATIONS_PATH_V3 = P5_ROOT / "materializations_nonblind_v3.jsonl"
BLIND_MATERIALIZATIONS_PATH_V3 = P5_ROOT / "frozen_blind.v3.materializations.jsonl"
MANIFEST_PATH_V3 = P5_ROOT / "dataset_v3.manifest.json"
BLIND_SEAL_PATH_V3 = P5_ROOT / "sealed" / "frozen_blind.v3.seal.json"
RUN_SPEC_TEMPLATE_PATH_V3 = P5_ROOT / "run_spec_template_v3.json"
CONTRACTS_PATH_V3 = P5_ROOT / "contracts_v3.py"
ACTIVE_CONTRACT_PATH = P5_ROOT / "active_contract.json"

_EXPECTED_V2_PATHS = {
    "nonblind_cases": NONBLIND_PATH_V2,
    "blind_cases": BLIND_INPUT_PATH_V2,
    "nonblind_materializations": NONBLIND_MATERIALIZATIONS_PATH_V2,
    "blind_materializations": BLIND_MATERIALIZATIONS_PATH_V2,
}
_LABEL_KEYS = {
    "answer",
    "blind_label",
    "expected",
    "ground_truth",
    "human_label",
    "label",
    "oracle",
    "oracle_sha256",
}


def _canonical_text_file_sha256(path: Path) -> str:
    """Hash the LF-normalized Git text blob rather than checkout-specific CRLF."""

    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def _walk_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return set(map(str, value)) | set().union(
            *(_walk_keys(item) for item in value.values()), set()
        )
    if isinstance(value, list):
        return set().union(*(_walk_keys(item) for item in value), set())
    return set()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path.name}")
    return value


def validate_v2_source_anchor() -> dict[str, Any]:
    """Fail closed unless the checked-in v2 dataset, seal, and active contract agree."""

    for path in (*_EXPECTED_V2_PATHS.values(), MANIFEST_PATH_V2, BLIND_SEAL_PATH_V2, ACTIVE_CONTRACT_PATH):
        if not path.is_file():
            raise ValueError(f"P5 v2 source artifact is missing: {path.name}")
    manifest = _load_json(MANIFEST_PATH_V2)
    seal = _load_json(BLIND_SEAL_PATH_V2)
    active = _load_json(ACTIVE_CONTRACT_PATH)
    if manifest.get("manifest_hash") != digest(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    ):
        raise ValueError("P5 v2 source manifest hash mismatch")
    if (
        manifest.get("schema_version") != "trip-check-p5-dataset-manifest-v2"
        or manifest.get("dataset_id") != "trip-check-p5-360-v2"
        or manifest.get("frozen") is not True
        or manifest.get("generation", {}).get("formal_validation_eligible") is not True
        or manifest.get("generation", {}).get("ocr_mode") != "actual"
    ):
        raise ValueError("P5 v2 source manifest is not the frozen actual-OCR dataset")
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("P5 v2 source manifest has no file index")
    for name, path in _EXPECTED_V2_PATHS.items():
        rows = load_jsonl(path)
        entry = files.get(name)
        if not isinstance(entry, Mapping) or (
            entry.get("file_sha256") != file_sha256(path)
            or entry.get("content_sha256") != digest(rows)
            or entry.get("row_count") != len(rows)
        ):
            raise ValueError(f"P5 v2 source file binding mismatch: {name}")
    commitment = manifest.get("sealing_commitment")
    if not isinstance(commitment, Mapping) or commitment.get("status") != "SEALED":
        raise ValueError("P5 v2 source manifest has no sealed commitment")
    if (
        commitment.get("blind_seal_v2_sha256") != file_sha256(BLIND_SEAL_PATH_V2)
        or seal.get("inputs_file_sha256") != file_sha256(BLIND_INPUT_PATH_V2)
        or seal.get("materializations_file_sha256")
        != file_sha256(BLIND_MATERIALIZATIONS_PATH_V2)
        or seal.get("case_count") != 90
    ):
        raise ValueError("P5 v2 blind seal differs from the source dataset")
    if (
        active.get("active_contract") != "trip-check-p5-v2"
        or active.get("formal_evidence_status") != "READY"
        or active.get("dataset_manifest_hash") != manifest["manifest_hash"]
        or active.get("blind_seal_v2_sha256") != file_sha256(BLIND_SEAL_PATH_V2)
        or active.get("candidate_freeze_commit") != commitment.get("candidate_freeze_commit")
    ):
        raise ValueError("P5 v2 active contract differs from manifest/seal")
    return {
        "dataset_id": manifest["dataset_id"],
        "manifest_hash": manifest["manifest_hash"],
        "manifest_file_sha256": file_sha256(MANIFEST_PATH_V2),
        "blind_seal_file_sha256": file_sha256(BLIND_SEAL_PATH_V2),
        "active_contract_sha256": digest(active),
        "active_contract_file_sha256": file_sha256(ACTIVE_CONTRACT_PATH),
        "candidate_freeze_commit": active["candidate_freeze_commit"],
    }


def _artifact_binding(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact["artifact_id"],
        "schema_version": artifact["schema_version"],
        "content_sha256": artifact["content_sha256"],
    }


def _receipt_binding(receipt: Mapping[str, Any], *, artifact_id: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "schema_version": receipt["schema_version"],
        "content_sha256": digest(receipt),
    }


def _cleanup_receipt(v2_materialization: Mapping[str, Any]) -> dict[str, Any] | None:
    rows = [
        item
        for item in v2_materialization.get("receipts", [])
        if isinstance(item, Mapping)
        and item.get("schema_version") == "trip-check-p5-cleanup-receipt-v2"
    ]
    if v2_materialization.get("ocr_baseline_receipt") is None:
        if rows:
            raise ValueError("text v2 source unexpectedly contains cleanup receipt")
        return None
    if len(rows) != 1:
        raise ValueError("screenshot v2 source must contain exactly one cleanup receipt")
    return dict(rows[0])


def _runner_control_v3(case: Mapping[str, Any]) -> dict[str, Any]:
    runner = deepcopy(case["runner_control"])
    runner["provider_snapshot_id"] = PROVIDER_SNAPSHOT_ID_V3
    return runner


def materialization_input_projection_v3(
    case: Mapping[str, Any], v2_materialization: Mapping[str, Any]
) -> dict[str, Any]:
    """Project only label-free inputs and immutable historical screenshot receipts."""

    projected = {
        "case_id": case.get("case_id"),
        "city": case.get("city"),
        "trip_days": case.get("trip_days"),
        "group_size": case.get("group_size"),
        "input_kind": case.get("input_kind"),
        "product_input": deepcopy(case.get("product_input")),
        "normalized_input_sha256": case.get("normalized_input_sha256"),
        "runner_control": _runner_control_v3(case),
    }
    if case.get("input_kind") == "SYNTHETIC_SCREENSHOT":
        cleanup = _cleanup_receipt(v2_materialization)
        projected.update(
            {
                "render_receipt": deepcopy(v2_materialization.get("render_receipt")),
                "ocr_baseline_receipt": deepcopy(v2_materialization.get("ocr_baseline_receipt")),
                "cleanup_receipt": cleanup,
            }
        )
    return projected


def ocr_source_binding_projection_v3(inner: Mapping[str, Any]) -> dict[str, Any] | None:
    source = inner["source_payload"].get("ocr_source_binding")
    if source is None:
        return None
    return {
        "schema_version": source["schema_version"],
        "source_dataset_id": source["source_dataset_id"],
        "source_manifest_hash": source["source_manifest_hash"],
        "source_manifest_file_sha256": source["source_manifest_file_sha256"],
        "source_blind_seal_file_sha256": source["source_blind_seal_file_sha256"],
        "source_active_contract_sha256": source["source_active_contract_sha256"],
        "source_active_contract_file_sha256": source["source_active_contract_file_sha256"],
        "source_candidate_freeze_commit": source["source_candidate_freeze_commit"],
        "source_path": source["source_path"],
        "source_file_sha256": source["source_file_sha256"],
        "source_materialization_hash": source["source_materialization_hash"],
        "historical_render_receipt_sha256": source["render_receipt_sha256"],
        "historical_ocr_receipt_sha256": source["ocr_receipt_sha256"],
        "historical_cleanup_receipt_sha256": source["cleanup_receipt_sha256"],
    }


def build_materialization_v3(
    case: Mapping[str, Any], v2_materialization: Mapping[str, Any]
) -> dict[str, Any]:
    if case.get("case_id") != v2_materialization.get("case_id"):
        raise ValueError("P5 v2 case/materialization ID mismatch")
    inner = build_evidence_materialization_v3(
        materialization_input_projection_v3(case, v2_materialization)
    )
    fault_script = deepcopy(v2_materialization["fault_script"])
    if fault_script.get("content_sha256") != digest(
        {key: value for key, value in fault_script.items() if key != "content_sha256"}
    ):
        raise ValueError("P5 v2 fault script hash mismatch")
    materialization = {
        "schema_version": MATERIALIZATION_SCHEMA_VERSION_V3,
        "materialization_id": f"materialization-v3-{case['case_id']}",
        "case_id": case["case_id"],
        "source_payload": inner["source_payload"],
        "render_receipt": inner["render_receipt"],
        "ocr_baseline_receipt": inner["ocr_baseline_receipt"],
        "cleanup_receipt": inner["cleanup_receipt"],
        "ocr_source_binding": ocr_source_binding_projection_v3(inner),
        "provider_snapshot": inner["provider_snapshot"],
        "evidence_snapshot": inner["evidence_snapshot"],
        "candidate_sets": inner["candidate_sets"],
        "fault_script": fault_script,
        "receipts": inner["receipts"],
        "evidence_materialization_hash": inner["evidence_materialization_hash"],
    }
    materialization["materialization_hash"] = digest(materialization)
    return materialization


def evidence_projection_v3(materialization: Mapping[str, Any]) -> dict[str, Any]:
    projection = {
        key: deepcopy(materialization[key])
        for key in (
            "case_id",
            "source_payload",
            "provider_snapshot",
            "evidence_snapshot",
            "render_receipt",
            "ocr_baseline_receipt",
            "cleanup_receipt",
            "candidate_sets",
            "receipts",
            "evidence_materialization_hash",
        )
    }
    projection["schema_version"] = "trip-check-p5-evidence-materialization-v3"
    return validate_evidence_materialization_v3(projection)


def _binding_for_materialization_v3(materialization: Mapping[str, Any]) -> dict[str, Any]:
    case_id = str(materialization["case_id"])
    render = materialization["render_receipt"]
    ocr = materialization["ocr_baseline_receipt"]
    cleanup = materialization["cleanup_receipt"]
    binding = {
        "schema_version": "trip-check-p5-materialization-binding-v3",
        "materialization_id": materialization["materialization_id"],
        "materialization_sha256": materialization["materialization_hash"],
        "source_payload": _artifact_binding(materialization["source_payload"]),
        "render_receipt": (
            _receipt_binding(render, artifact_id=f"render-{case_id}") if render else None
        ),
        "ocr_baseline_receipt": (
            _receipt_binding(ocr, artifact_id=f"ocr-{case_id}") if ocr else None
        ),
        "cleanup_receipt": (
            _receipt_binding(cleanup, artifact_id=f"cleanup-{case_id}") if cleanup else None
        ),
        "ocr_source_binding": deepcopy(materialization["ocr_source_binding"]),
        "provider_snapshot": _artifact_binding(materialization["provider_snapshot"]),
        "evidence_snapshot": _artifact_binding(materialization["evidence_snapshot"]),
        "candidate_sets": [
            _artifact_binding(item) for item in materialization["candidate_sets"]
        ],
        "fault_script": _artifact_binding(materialization["fault_script"]),
    }
    return {key: value for key, value in binding.items() if value is not None}


def build_case_v3(
    v2_case: Mapping[str, Any], materialization: Mapping[str, Any]
) -> dict[str, Any]:
    case = {
        key: deepcopy(value)
        for key, value in v2_case.items()
        if key not in {"schema_version", "case_hash", "materialization", "runner_control", "lineage", "provenance"}
    }
    lineage = deepcopy(v2_case["lineage"])
    lineage["generator_family_id"] = GENERATOR_VERSION_V3
    provenance = {
        **deepcopy(v2_case["provenance"]),
        "generated_by": GENERATOR_VERSION_V3,
        "reviewed_by": "NOT_RUN",
        "source_dataset_id": "trip-check-p5-360-v2",
        "source_case_hash": v2_case["case_hash"],
        "source_materialization_hash": materialization["ocr_source_binding"]["source_materialization_hash"]
        if materialization["ocr_source_binding"]
        else None,
        "actual_ocr_materialization": (
            "PASS_HISTORICAL_V2_RECEIPT"
            if v2_case["input_kind"] == "SYNTHETIC_SCREENSHOT"
            else "NOT_APPLICABLE"
        ),
        "v3_receipt_rebinding": (
            "PASS" if v2_case["input_kind"] == "SYNTHETIC_SCREENSHOT" else "NOT_APPLICABLE"
        ),
        "fresh_actual_ocr_execution": "NOT_RUN",
    }
    case.update(
        {
            "schema_version": CASE_SCHEMA_VERSION_V3,
            "materialization": _binding_for_materialization_v3(materialization),
            "runner_control": _runner_control_v3(v2_case),
            "lineage": lineage,
            "provenance": provenance,
        }
    )
    case["case_hash"] = digest(case)
    return P5CaseV3.model_validate(case).model_dump(mode="json", exclude_none=True)


def _label_free_case(case: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in case.items()
        if key not in {"oracle", "oracle_sha256"}
    }


def build_dataset_v3() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Deterministically rebuild all 360 v3 rows without OCR or blind labels."""

    validate_v2_source_anchor()
    nonblind_v2 = load_jsonl(NONBLIND_PATH_V2)
    blind_v2 = load_jsonl(BLIND_INPUT_PATH_V2)
    nonblind_m2 = {row["case_id"]: row for row in load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V2)}
    blind_m2 = {row["case_id"]: row for row in load_jsonl(BLIND_MATERIALIZATIONS_PATH_V2)}
    if any(_walk_keys(row) & _LABEL_KEYS for row in blind_v2):
        raise ValueError("P5 v2 blind input contains forbidden label fields")

    def build_lane(
        source_cases: list[dict[str, Any]], source_materializations: Mapping[str, dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        cases: list[dict[str, Any]] = []
        materializations: list[dict[str, Any]] = []
        if set(source_materializations) != {row["case_id"] for row in source_cases}:
            raise ValueError("P5 v2 source case/materialization sets differ")
        for source_case in source_cases:
            materialization = build_materialization_v3(
                source_case, source_materializations[source_case["case_id"]]
            )
            case = build_case_v3(source_case, materialization)
            cases.append(case)
            materializations.append(materialization)
        return cases, materializations

    nonblind_cases, nonblind_materializations = build_lane(nonblind_v2, nonblind_m2)
    blind_cases, blind_materializations = build_lane(blind_v2, blind_m2)
    semantic = validate_dataset_semantics_v3(
        [_label_free_case(row) for row in [*nonblind_cases, *blind_cases]],
        [evidence_projection_v3(row) for row in [*nonblind_materializations, *blind_materializations]],
    )
    if semantic["status"] != "PASS":
        raise ValueError(f"P5 v3 semantic validation failed: {semantic['errors'][:3]}")
    oracle_errors = [
        error
        for case, materialization in zip(nonblind_cases, nonblind_materializations, strict=True)
        for error in validate_nonblind_oracle_compatibility_v3(
            case, evidence_projection_v3(materialization)
        )
    ]
    if oracle_errors:
        raise ValueError(f"P5 v3 nonblind oracle compatibility failed: {oracle_errors[:3]}")
    return nonblind_cases, blind_cases, nonblind_materializations, blind_materializations


def case_set_hash_v3(rows: list[dict[str, Any]]) -> str:
    return digest(
        sorted(
            ({"case_id": row["case_id"], "case_hash": row["case_hash"]} for row in rows),
            key=lambda row: row["case_id"],
        )
    )


def materialization_set_hash_v3(rows: list[dict[str, Any]]) -> str:
    return digest(
        sorted(
            (
                {
                    "case_id": row["case_id"],
                    "materialization_id": row["materialization_id"],
                    "materialization_hash": row["materialization_hash"],
                }
                for row in rows
            ),
            key=lambda row: row["case_id"],
        )
    )


def _file_entry(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "path": path.relative_to(BACKEND_ROOT).as_posix(),
        "row_count": len(rows),
        "file_sha256": file_sha256(path),
        "content_sha256": digest(rows),
    }


def _validated_sealing_commitment_v3(
    commitment_payload: Mapping[str, Any],
    *,
    candidate_manifest_hash: str,
    blind_cases: list[dict[str, Any]],
    blind_materializations: list[dict[str, Any]],
) -> dict[str, Any]:
    commitment = P5SealingCommitmentV3.model_validate(commitment_payload)
    if commitment.candidate_dataset_manifest_hash != candidate_manifest_hash:
        raise ValueError("P5 v3 sealing commitment candidate manifest hash mismatch")
    if not BLIND_SEAL_PATH_V3.is_file():
        raise ValueError("P5 v3 sealing commitment requires the blind seal file")
    if commitment.blind_seal_file_sha256 != file_sha256(BLIND_SEAL_PATH_V3):
        raise ValueError("P5 v3 sealing commitment blind seal file hash mismatch")
    seal = P5BlindSealV3.model_validate(_load_json(BLIND_SEAL_PATH_V3))
    expected = {
        "case_count": len(blind_cases),
        "case_ids_sha256": digest(sorted(row["case_id"] for row in blind_cases)),
        "inputs_file_sha256": file_sha256(BLIND_INPUT_PATH_V3),
        "inputs_content_sha256": digest(blind_cases),
        "materializations_file_sha256": file_sha256(BLIND_MATERIALIZATIONS_PATH_V3),
        "materializations_content_sha256": digest(blind_materializations),
        "case_set_hash": case_set_hash_v3(blind_cases),
        "materialization_set_hash": materialization_set_hash_v3(blind_materializations),
        "contracts_v3_sha256": _canonical_text_file_sha256(CONTRACTS_PATH_V3),
        "run_spec_template_sha256": file_sha256(RUN_SPEC_TEMPLATE_PATH_V3),
        "rubric_sha256": file_sha256(JUDGE_RUBRIC_PATH_V2),
        "variant_ids_sha256": digest(list(VARIANT_IDS_V3)),
    }
    seal_payload = seal.model_dump(mode="json")
    if any(seal_payload[key] != value for key, value in expected.items()):
        raise ValueError("P5 v3 blind seal differs from frozen candidate bytes/contracts")
    if (
        commitment.candidate_freeze_commit != seal.candidate_freeze_commit
        or commitment.candidate_dataset_manifest_hash
        != seal.candidate_dataset_manifest_hash
        or commitment.labels_canonical_sha256 != seal.labels_canonical_sha256
        or commitment.external_bundle_sha256 != seal.external_bundle_sha256
        or commitment.review_receipt_sha256 != seal.review_receipt_sha256
    ):
        raise ValueError("P5 v3 sealing commitment differs from blind custodian receipts")
    return commitment.model_dump(mode="json")


def build_manifest_v3(
    *,
    nonblind_cases: list[dict[str, Any]],
    blind_cases: list[dict[str, Any]],
    nonblind_materializations: list[dict[str, Any]],
    blind_materializations: list[dict[str, Any]],
    sealing_commitment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source_anchor = validate_v2_source_anchor()
    all_cases = [*nonblind_cases, *blind_cases]
    screenshot_cases = [
        row for row in all_cases if row["input_kind"] == "SYNTHETIC_SCREENSHOT"
    ]
    screenshot_materializations = [
        row
        for row in [*nonblind_materializations, *blind_materializations]
        if row["ocr_baseline_receipt"] is not None
    ]
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION_V3,
        "dataset_id": DATASET_ID_V3,
        "frozen": False,
        "formal_validation_eligible": False,
        "seal_status": "PENDING_V3_SEAL",
        "generation": {
            "builder_version": GENERATOR_VERSION_V3,
            "mode": "SEALED_V2_SOURCE_REBIND",
            "blind_labels_read": False,
            "ocr_executed": False,
        },
        "hash_policy_version": HASH_POLICY_VERSION_V3,
        "counts": {
            "total": len(all_cases),
            "by_split": dict(sorted(Counter(row["split"] for row in all_cases).items())),
            "by_city": dict(sorted(Counter(row["city"] for row in all_cases).items())),
            "screenshots_by_split": dict(
                sorted(Counter(row["split"] for row in screenshot_cases).items())
            ),
            "screenshot_receipts_total": len(screenshot_materializations),
            "unique_screenshot_image_hashes": len(
                {row["render_receipt"]["image_sha256"] for row in screenshot_materializations}
            ),
        },
        "files": {
            "nonblind_cases": _file_entry(NONBLIND_PATH_V3, nonblind_cases),
            "blind_cases": _file_entry(BLIND_INPUT_PATH_V3, blind_cases),
            "nonblind_materializations": _file_entry(
                NONBLIND_MATERIALIZATIONS_PATH_V3, nonblind_materializations
            ),
            "blind_materializations": _file_entry(
                BLIND_MATERIALIZATIONS_PATH_V3, blind_materializations
            ),
        },
        "lanes": {
            "nonblind": {
                "case_count": len(nonblind_cases),
                "materialization_count": len(nonblind_materializations),
                "case_set_hash": case_set_hash_v3(nonblind_cases),
                "materialization_set_hash": materialization_set_hash_v3(
                    nonblind_materializations
                ),
                "oracle_schema_version": "trip-check-p5-oracle-v2",
                "oracle_content_changed": False,
            },
            "frozen_blind": {
                "case_count": len(blind_cases),
                "materialization_count": len(blind_materializations),
                "case_set_hash": case_set_hash_v3(blind_cases),
                "materialization_set_hash": materialization_set_hash_v3(
                    blind_materializations
                ),
                "label_storage": "external_bundle_only",
                "label_access": "isolated_custodian_only",
                "label_payload_present": False,
            },
        },
        "source_v2_anchor": source_anchor,
        "contract_hashes": {
            "contracts_v3_path": CONTRACTS_PATH_V3.relative_to(BACKEND_ROOT).as_posix(),
            "contracts_v3_sha256": _canonical_text_file_sha256(CONTRACTS_PATH_V3),
            "judge_rubric_path": JUDGE_RUBRIC_PATH_V2.relative_to(BACKEND_ROOT).as_posix(),
            "judge_rubric_sha256": file_sha256(JUDGE_RUBRIC_PATH_V2),
            "judge_rubric_semantics_changed": False,
            "run_spec_template_path": RUN_SPEC_TEMPLATE_PATH_V3.relative_to(BACKEND_ROOT).as_posix(),
            "run_spec_template_sha256": file_sha256(RUN_SPEC_TEMPLATE_PATH_V3),
        },
        "evidence_boundary": {
            "controlled_fixture": "MATERIALIZED_V3",
            "actual_ocr_materialization": "PASS_HISTORICAL_V2_RECEIPT",
            "v3_receipt_rebinding": "PASS",
            "fresh_actual_ocr_execution": "NOT_RUN",
            "live_provider": "NOT_RUN",
            "public_e2e": "NOT_RUN",
            "human_evidence": "NOT_RUN",
        },
    }
    manifest["manifest_hash"] = digest(manifest)
    if sealing_commitment is not None:
        manifest["sealing_commitment"] = _validated_sealing_commitment_v3(
            sealing_commitment,
            candidate_manifest_hash=manifest["manifest_hash"],
            blind_cases=blind_cases,
            blind_materializations=blind_materializations,
        )
        manifest["frozen"] = True
        manifest["formal_validation_eligible"] = True
        manifest["seal_status"] = "SEALED"
        manifest["manifest_hash"] = digest(
            {key: value for key, value in manifest.items() if key != "manifest_hash"}
        )
    return manifest


def write_dataset_v3(
    *,
    nonblind_cases: list[dict[str, Any]],
    blind_cases: list[dict[str, Any]],
    nonblind_materializations: list[dict[str, Any]],
    blind_materializations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write only new v3 paths; a present v3 seal makes the dataset immutable."""

    if BLIND_SEAL_PATH_V3.exists():
        raise ValueError("P5_V3_SEALED_DATASET_IMMUTABLE")
    before = {path: file_sha256(path) for path in (*_EXPECTED_V2_PATHS.values(), MANIFEST_PATH_V2, BLIND_SEAL_PATH_V2)}
    write_jsonl(NONBLIND_PATH_V3, nonblind_cases)
    write_jsonl(BLIND_INPUT_PATH_V3, blind_cases)
    write_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V3, nonblind_materializations)
    write_jsonl(BLIND_MATERIALIZATIONS_PATH_V3, blind_materializations)
    after = {path: file_sha256(path) for path in before}
    if before != after:
        raise ValueError("P5 v2 source bytes changed while writing v3")
    manifest = build_manifest_v3(
        nonblind_cases=nonblind_cases,
        blind_cases=blind_cases,
        nonblind_materializations=nonblind_materializations,
        blind_materializations=blind_materializations,
    )
    write_json(MANIFEST_PATH_V3, manifest)
    return manifest


__all__ = [
    "BLIND_INPUT_PATH_V3",
    "BLIND_MATERIALIZATIONS_PATH_V3",
    "BLIND_SEAL_PATH_V3",
    "DATASET_ID_V3",
    "MANIFEST_PATH_V3",
    "NONBLIND_MATERIALIZATIONS_PATH_V3",
    "NONBLIND_PATH_V3",
    "RUN_SPEC_TEMPLATE_PATH_V3",
    "build_case_v3",
    "build_dataset_v3",
    "build_manifest_v3",
    "build_materialization_v3",
    "case_set_hash_v3",
    "evidence_projection_v3",
    "materialization_input_projection_v3",
    "materialization_set_hash_v3",
    "ocr_source_binding_projection_v3",
    "validate_v2_source_anchor",
    "write_dataset_v3",
]
