"""Deterministic P5 v4 dataset rebinding over immutable sealed v3 bytes.

The only authorized semantic changes are the route facts for
``p5.pilot.bj.004`` and ``p5.pilot.sh.001``.  Both values are copied from the
tracked P1 pilot snapshots, where the controlled route fixture binds 90
minutes.  Blind inputs/materializations and blind truth commitments are never
read beyond their label-free repository envelopes.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p5.adapters_v3 import validate_materialization_v3
from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3, VARIANT_IDS_V3
from evals.trip_check_v1.p5.data_contract import (
    BACKEND_ROOT,
    P5_ROOT,
    digest,
    file_sha256,
    load_jsonl,
    write_json,
    write_jsonl,
)
from evals.trip_check_v1.p5.data_contract_v2 import (
    BLIND_SEAL_PATH_V2,
    JUDGE_RUBRIC_PATH_V2,
)
from evals.trip_check_v1.p5.data_contract_v3 import (
    BLIND_INPUT_PATH_V3,
    BLIND_MATERIALIZATIONS_PATH_V3,
    BLIND_SEAL_PATH_V3,
    CONTRACTS_PATH_V3,
    MANIFEST_PATH_V3,
    NONBLIND_MATERIALIZATIONS_PATH_V3,
    NONBLIND_PATH_V3,
    case_set_hash_v3,
    materialization_set_hash_v3,
)
from evals.trip_check_v1.p5.dataset_contracts_v4 import (
    P5BlindSealV4,
    P5RouteEvidenceRepairV4,
    P5SealingCommitmentV4,
)


MANIFEST_SCHEMA_VERSION_V4 = "trip-check-p5-dataset-manifest-v4"
DATASET_ID_V4 = "trip-check-p5-360-v4"
GENERATOR_VERSION_V4 = "p5-v3-route-evidence-rebinder-v4"
HASH_POLICY_VERSION_V4 = "p5-canonical-json-nfc-lf-v4"

NONBLIND_PATH_V4 = P5_ROOT / "cases_nonblind_v4.jsonl"
BLIND_INPUT_PATH_V4 = P5_ROOT / "frozen_blind.v4.inputs.jsonl"
NONBLIND_MATERIALIZATIONS_PATH_V4 = P5_ROOT / "materializations_nonblind_v4.jsonl"
BLIND_MATERIALIZATIONS_PATH_V4 = P5_ROOT / "frozen_blind.v4.materializations.jsonl"
MANIFEST_PATH_V4 = P5_ROOT / "dataset_v4.manifest.json"
BLIND_SEAL_PATH_V4 = P5_ROOT / "sealed" / "frozen_blind.v4.seal.json"
DATASET_CONTRACTS_PATH_V4 = P5_ROOT / "dataset_contracts_v4.py"
RUN_SPEC_TEMPLATE_PATH_V4 = P5_ROOT / "run_spec_template_v4.json"
SOURCE_ACTIVE_CONTRACT_V3_PATH = P5_ROOT / "source_active_contract_v3.json"

_AUTHORIZED_ROUTE_REPAIRS = {
    "p5.pilot.bj.004": {
        "source_case_id": "TC-P1-BJ-04",
        "source_path": BACKEND_ROOT
        / "evidence/trip_check_v1/p1/pilot/runs/TC-P1-BJ-04/snapshots.json",
    },
    "p5.pilot.sh.001": {
        "source_case_id": "TC-P1-SH-01",
        "source_path": BACKEND_ROOT
        / "evidence/trip_check_v1/p1/pilot/runs/TC-P1-SH-01/snapshots.json",
    },
}
_FORBIDDEN_BLIND_KEYS = {
    "answer",
    "blind_label",
    "expected",
    "ground_truth",
    "human_label",
    "label",
    "oracle",
    "oracle_sha256",
}
_EXPECTED_V3_PATHS = {
    "nonblind_cases": NONBLIND_PATH_V3,
    "blind_cases": BLIND_INPUT_PATH_V3,
    "nonblind_materializations": NONBLIND_MATERIALIZATIONS_PATH_V3,
    "blind_materializations": BLIND_MATERIALIZATIONS_PATH_V3,
}


def _canonical_text_file_sha256(path: Path) -> str:
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path.name}")
    return value


def _walk_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return set(map(str, value)) | set().union(
            *(_walk_keys(item) for item in value.values()), set()
        )
    if isinstance(value, list):
        return set().union(*(_walk_keys(item) for item in value), set())
    return set()


def validate_v3_source_anchor() -> dict[str, Any]:
    """Fail closed unless every sealed v3 source byte remains hash-bound."""

    required = (
        *_EXPECTED_V3_PATHS.values(),
        MANIFEST_PATH_V3,
        BLIND_SEAL_PATH_V3,
        BLIND_SEAL_PATH_V2,
        SOURCE_ACTIVE_CONTRACT_V3_PATH,
    )
    for path in required:
        if not path.is_file():
            raise ValueError(f"P5 v3 source artifact is missing: {path.name}")
    manifest = _load_json(MANIFEST_PATH_V3)
    seal = _load_json(BLIND_SEAL_PATH_V3)
    active = _load_json(SOURCE_ACTIVE_CONTRACT_V3_PATH)
    if manifest.get("manifest_hash") != digest(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    ):
        raise ValueError("P5 v3 source manifest hash mismatch")
    if (
        manifest.get("schema_version") != "trip-check-p5-dataset-manifest-v3"
        or manifest.get("dataset_id") != "trip-check-p5-360-v3"
        or manifest.get("seal_status") != "SEALED"
        or manifest.get("frozen") is not True
        or manifest.get("formal_validation_eligible") is not True
    ):
        raise ValueError("P5 v3 source manifest is not sealed")
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("P5 v3 source manifest has no file index")
    for name, path in _EXPECTED_V3_PATHS.items():
        rows = load_jsonl(path)
        entry = files.get(name)
        if not isinstance(entry, Mapping) or (
            entry.get("file_sha256") != file_sha256(path)
            or entry.get("content_sha256") != digest(rows)
            or entry.get("row_count") != len(rows)
        ):
            raise ValueError(f"P5 v3 source file binding mismatch: {name}")
    commitment = manifest.get("sealing_commitment")
    if not isinstance(commitment, Mapping) or commitment.get("status") != "SEALED":
        raise ValueError("P5 v3 source manifest has no sealing commitment")
    if (
        commitment.get("blind_seal_file_sha256") != file_sha256(BLIND_SEAL_PATH_V3)
        or seal.get("inputs_file_sha256") != file_sha256(BLIND_INPUT_PATH_V3)
        or seal.get("materializations_file_sha256")
        != file_sha256(BLIND_MATERIALIZATIONS_PATH_V3)
        or seal.get("case_count") != 90
    ):
        raise ValueError("P5 v3 blind seal differs from frozen bytes")
    if (
        active.get("active_contract") != "trip-check-p5-v3"
        or active.get("formal_evidence_status") != "READY"
        or active.get("dataset_manifest_hash") != manifest["manifest_hash"]
        or active.get("blind_seal_v3_sha256") != file_sha256(BLIND_SEAL_PATH_V3)
        or active.get("candidate_freeze_commit")
        != commitment.get("candidate_freeze_commit")
    ):
        raise ValueError("P5 v3 source active contract differs from manifest/seal")
    for key in (
        "external_bundle_sha256",
        "labels_canonical_sha256",
        "review_receipt_sha256",
    ):
        if seal.get(key) != commitment.get(key):
            raise ValueError(f"P5 v3 custody commitment mismatch: {key}")
    return {
        "dataset_id": manifest["dataset_id"],
        "manifest_hash": manifest["manifest_hash"],
        "manifest_file_sha256": file_sha256(MANIFEST_PATH_V3),
        "blind_seal_file_sha256": file_sha256(BLIND_SEAL_PATH_V3),
        "blind_inputs_file_sha256": file_sha256(BLIND_INPUT_PATH_V3),
        "blind_materializations_file_sha256": file_sha256(
            BLIND_MATERIALIZATIONS_PATH_V3
        ),
        "active_contract_sha256": digest(active),
        "active_contract_file_sha256": file_sha256(
            SOURCE_ACTIVE_CONTRACT_V3_PATH
        ),
        "candidate_freeze_commit": active["candidate_freeze_commit"],
        "external_bundle_sha256": seal["external_bundle_sha256"],
        "labels_canonical_sha256": seal["labels_canonical_sha256"],
        "review_receipt_sha256": seal["review_receipt_sha256"],
    }


def _route_source_binding(case: Mapping[str, Any]) -> dict[str, Any]:
    case_id = str(case.get("case_id"))
    config = _AUTHORIZED_ROUTE_REPAIRS.get(case_id)
    if config is None:
        raise ValueError(f"P5 v4 route repair is not authorized: {case_id}")
    if case.get("split") != "pilot" or case.get("source_ref", {}).get(
        "case_id"
    ) != config["source_case_id"]:
        raise ValueError(f"P5 v4 route repair source case mismatch: {case_id}")
    path = Path(config["source_path"])
    raw = json.loads(path.read_text(encoding="utf-8"))
    snapshots = raw if isinstance(raw, list) else [raw]
    first_revision = [
        item
        for item in snapshots
        if isinstance(item, Mapping) and item.get("itinerary_revision") == 1
    ]
    if len(first_revision) != 1:
        raise ValueError(f"P5 v4 P1 source must have one revision-1 snapshot: {case_id}")
    snapshot = first_revision[0]
    facts = [
        item
        for item in snapshot.get("facts", [])
        if isinstance(item, Mapping)
        and item.get("subject_type") == "ROUTE_EDGE"
        and item.get("fact_type") == "ROUTE_TIME"
    ]
    if len(facts) != 1:
        raise ValueError(f"P5 v4 P1 source must have one route fact: {case_id}")
    fact = facts[0]
    value = fact.get("value")
    if (
        not isinstance(value, Mapping)
        or value.get("duration_minutes") != 90
        or value.get("mode") != "driving"
        or fact.get("provider") != "controlled_route_fixture_v1"
    ):
        raise ValueError(f"P5 v4 P1 route fact is not the authorized 90min binding: {case_id}")
    relative = path.relative_to(BACKEND_ROOT.parent).as_posix()
    source_url = (
        f"repo://{relative}#snapshot={snapshot['snapshot_id']}&fact={fact['fact_id']}"
    )
    return P5RouteEvidenceRepairV4(
        case_id=case_id,
        source_case_id=str(config["source_case_id"]),
        source_path=relative,
        source_file_sha256=file_sha256(path),
        source_snapshot_id=str(snapshot["snapshot_id"]),
        source_fact_id=str(fact["fact_id"]),
        source_response_hash=str(fact["response_hash"]),
        duration_minutes=90,
        blind_lane_touched=False,
    ).model_dump(mode="json") | {
        "source_url": source_url,
        "source_fact": deepcopy(dict(fact)),
    }


def _artifact_rehash(artifact: dict[str, Any]) -> None:
    artifact["content_sha256"] = digest(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )


def _receipt_semantic_payload(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "provider": receipt.get("provider"),
        "operation": receipt.get("operation"),
        "execution_mode": receipt.get("execution_mode"),
        "status": receipt.get("status"),
        "request_hash": receipt.get("request_hash"),
        "response_hash": receipt.get("response_hash"),
        "observed_at": receipt.get("observed_at"),
        "source_url": receipt.get("source_url"),
        "affected_fields": receipt.get("affected_fields", []),
        "failure_category": receipt.get("failure_category"),
    }


def _evidence_materialization_hash(materialization: Mapping[str, Any]) -> str:
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
        )
    }
    projection["schema_version"] = "trip-check-p5-evidence-materialization-v3"
    return digest(projection)


def _artifact_binding(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact["artifact_id"],
        "schema_version": artifact["schema_version"],
        "content_sha256": artifact["content_sha256"],
    }


def _repair_materialization_v4(
    case: Mapping[str, Any], source_materialization: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return one exact v3 envelope with its authorized P1 route fact rebound."""

    source_case = P5CaseV3.model_validate(case)
    validate_materialization_v3(source_case, source_materialization)
    repaired = deepcopy(dict(source_materialization))
    binding = _route_source_binding(case)
    binding.pop("source_fact")
    binding.pop("source_url")
    route_facts = [
        item
        for item in repaired["evidence_snapshot"]["snapshot"]["facts"]
        if item.get("subject_type") == "ROUTE_EDGE"
        and item.get("fact_type") == "ROUTE_TIME"
    ]
    route_receipts = [
        item for item in repaired["receipts"] if item.get("operation") == "route.audit"
    ]
    if len(route_facts) != 1 or len(route_receipts) != 1:
        raise ValueError(f"P5 v4 repair requires one route fact/receipt: {case['case_id']}")
    fact = route_facts[0]
    receipt = route_receipts[0]
    old_receipt_id = str(receipt["receipt_id"])
    if fact.get("value", {}).get("duration_minutes") != 20:
        raise ValueError(f"P5 v4 source duration is not 20min: {case['case_id']}")
    fact["value"] = {**deepcopy(fact["value"]), "duration_minutes": 90}
    receipt["response_hash"] = digest(fact["value"])
    receipt["receipt_id"] = digest(_receipt_semantic_payload(receipt))
    fact["response_hash"] = receipt["response_hash"]
    fact["fact_id"] = digest(
        {
            "snapshot_id": fact["snapshot_id"],
            "subject_type": fact["subject_type"],
            "subject_id": fact["subject_id"],
            "fact_type": fact["fact_type"],
            "receipt_id": receipt["receipt_id"],
        }
    )
    provider = repaired["provider_snapshot"]
    provider["receipt_ids"] = [
        receipt["receipt_id"] if item == old_receipt_id else item
        for item in provider["receipt_ids"]
    ]
    _artifact_rehash(provider)
    _artifact_rehash(repaired["evidence_snapshot"])
    repaired["evidence_materialization_hash"] = _evidence_materialization_hash(repaired)
    repaired["materialization_hash"] = digest(
        {key: value for key, value in repaired.items() if key != "materialization_hash"}
    )
    return repaired, binding


def _repair_case_binding_v4(
    source_case: Mapping[str, Any], repaired_materialization: Mapping[str, Any]
) -> dict[str, Any]:
    repaired = deepcopy(dict(source_case))
    materialization = repaired["materialization"]
    materialization["materialization_sha256"] = repaired_materialization[
        "materialization_hash"
    ]
    materialization["provider_snapshot"] = _artifact_binding(
        repaired_materialization["provider_snapshot"]
    )
    materialization["evidence_snapshot"] = _artifact_binding(
        repaired_materialization["evidence_snapshot"]
    )
    repaired["case_hash"] = digest(
        {key: value for key, value in repaired.items() if key != "case_hash"}
    )
    return P5CaseV3.model_validate(repaired).model_dump(mode="json", exclude_none=True)


def validate_materialization_v4(
    case: P5CaseV3, value: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate v3 payloads plus the two exact, source-bound v4 deviations."""

    if case.case_id not in _AUTHORIZED_ROUTE_REPAIRS:
        return validate_materialization_v3(case, value)
    source_cases = {row["case_id"]: row for row in load_jsonl(NONBLIND_PATH_V3)}
    source_materializations = {
        row["case_id"]: row for row in load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V3)
    }
    source_case = source_cases[case.case_id]
    expected_materialization, _binding = _repair_materialization_v4(
        source_case, source_materializations[case.case_id]
    )
    expected_case = _repair_case_binding_v4(source_case, expected_materialization)
    if case.model_dump(mode="json", exclude_none=True) != expected_case:
        raise ValueError("P5 v4 case differs from the authorized route repair")
    if dict(value) != expected_materialization:
        raise ValueError("P5 v4 materialization differs from the authorized route repair")
    return deepcopy(expected_materialization)


def build_dataset_v4() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Rebuild v4 without OCR, external bundles, or blind labels."""

    validate_v3_source_anchor()
    nonblind_v3 = load_jsonl(NONBLIND_PATH_V3)
    blind_v3 = load_jsonl(BLIND_INPUT_PATH_V3)
    nonblind_m3 = {
        row["case_id"]: row for row in load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V3)
    }
    blind_materializations = load_jsonl(BLIND_MATERIALIZATIONS_PATH_V3)
    if any(_walk_keys(row) & _FORBIDDEN_BLIND_KEYS for row in blind_v3):
        raise ValueError("P5 v3 blind input contains forbidden label fields")
    if any(_walk_keys(row) & _FORBIDDEN_BLIND_KEYS for row in blind_materializations):
        raise ValueError("P5 v3 blind materialization contains forbidden label fields")
    if set(nonblind_m3) != {row["case_id"] for row in nonblind_v3}:
        raise ValueError("P5 v3 nonblind case/materialization sets differ")

    nonblind_cases: list[dict[str, Any]] = []
    nonblind_materializations: list[dict[str, Any]] = []
    changed: set[str] = set()
    for source_case in nonblind_v3:
        case_id = str(source_case["case_id"])
        source_materialization = nonblind_m3[case_id]
        source_model = P5CaseV3.model_validate(source_case)
        validate_materialization_v3(source_model, source_materialization)
        if case_id in _AUTHORIZED_ROUTE_REPAIRS:
            materialization, _binding = _repair_materialization_v4(
                source_case, source_materialization
            )
            case = _repair_case_binding_v4(source_case, materialization)
            changed.add(case_id)
        else:
            case = deepcopy(source_case)
            materialization = deepcopy(source_materialization)
        nonblind_cases.append(case)
        nonblind_materializations.append(materialization)
    if changed != set(_AUTHORIZED_ROUTE_REPAIRS):
        raise ValueError(f"P5 v4 route repair set mismatch: {sorted(changed)}")
    return (
        nonblind_cases,
        deepcopy(blind_v3),
        nonblind_materializations,
        deepcopy(blind_materializations),
    )


def case_set_hash_v4(rows: list[dict[str, Any]]) -> str:
    return case_set_hash_v3(rows)


def materialization_set_hash_v4(rows: list[dict[str, Any]]) -> str:
    return materialization_set_hash_v3(rows)


def _file_entry(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "path": path.relative_to(BACKEND_ROOT).as_posix(),
        "row_count": len(rows),
        "content_sha256": digest(rows),
        "file_sha256": file_sha256(path),
    }


def _manifest_route_repairs(nonblind_cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {row["case_id"]: row for row in nonblind_cases}
    return [
        {
            key: value
            for key, value in _route_source_binding(by_id[case_id]).items()
            if key not in {"source_fact", "source_url"}
        }
        for case_id in sorted(_AUTHORIZED_ROUTE_REPAIRS)
    ]


def _validated_sealing_commitment_v4(
    commitment_payload: Mapping[str, Any],
    *,
    pending_manifest_hash: str,
    blind_cases: list[dict[str, Any]],
    blind_materializations: list[dict[str, Any]],
) -> dict[str, Any]:
    commitment = P5SealingCommitmentV4.model_validate(commitment_payload)
    if commitment.candidate_dataset_manifest_hash != pending_manifest_hash:
        raise ValueError("P5 v4 sealing commitment candidate manifest hash mismatch")
    if not BLIND_SEAL_PATH_V4.is_file():
        raise ValueError("P5 v4 sealing commitment requires the blind seal")
    if commitment.blind_seal_file_sha256 != file_sha256(BLIND_SEAL_PATH_V4):
        raise ValueError("P5 v4 sealing commitment blind seal hash mismatch")
    seal = P5BlindSealV4.model_validate(_load_json(BLIND_SEAL_PATH_V4))
    source = validate_v3_source_anchor()
    expected = {
        "nonblind_cases_file_sha256": file_sha256(NONBLIND_PATH_V4),
        "nonblind_materializations_file_sha256": file_sha256(
            NONBLIND_MATERIALIZATIONS_PATH_V4
        ),
        "inputs_file_sha256": file_sha256(BLIND_INPUT_PATH_V4),
        "materializations_file_sha256": file_sha256(BLIND_MATERIALIZATIONS_PATH_V4),
        "case_set_hash": case_set_hash_v4(blind_cases),
        "materialization_set_hash": materialization_set_hash_v4(
            blind_materializations
        ),
        "contracts_v3_sha256": _canonical_text_file_sha256(CONTRACTS_PATH_V3),
        "dataset_contracts_v4_sha256": _canonical_text_file_sha256(
            DATASET_CONTRACTS_PATH_V4
        ),
        "run_spec_template_sha256": file_sha256(RUN_SPEC_TEMPLATE_PATH_V4),
        "rubric_sha256": file_sha256(JUDGE_RUBRIC_PATH_V2),
        "variant_ids_sha256": digest(list(VARIANT_IDS_V3)),
        "source_v3_blind_seal_file_sha256": source["blind_seal_file_sha256"],
        "source_v3_inputs_file_sha256": source["blind_inputs_file_sha256"],
        "source_v3_materializations_file_sha256": source[
            "blind_materializations_file_sha256"
        ],
        "source_v3_dataset_manifest_hash": source["manifest_hash"],
    }
    payload = seal.model_dump(mode="json")
    if any(payload[key] != value for key, value in expected.items()):
        raise ValueError("P5 v4 blind seal differs from candidate/source bytes")
    for key in (
        "labels_canonical_sha256",
        "external_bundle_sha256",
        "review_receipt_sha256",
    ):
        if payload[key] != source[key] or getattr(commitment, key) != source[key]:
            raise ValueError(f"P5 v4 custody commitment changed: {key}")
    return commitment.model_dump(mode="json")


def build_manifest_v4(
    *,
    nonblind_cases: list[dict[str, Any]],
    blind_cases: list[dict[str, Any]],
    nonblind_materializations: list[dict[str, Any]],
    blind_materializations: list[dict[str, Any]],
    sealing_commitment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    all_cases = [*nonblind_cases, *blind_cases]
    source = validate_v3_source_anchor()
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION_V4,
        "dataset_id": DATASET_ID_V4,
        "frozen": False,
        "formal_validation_eligible": False,
        "seal_status": "PENDING_V4_SEAL",
        "generation": {
            "builder_version": GENERATOR_VERSION_V4,
            "mode": "SEALED_V3_ROUTE_EVIDENCE_REBIND",
            "blind_labels_read": False,
            "blind_bytes_copied_from_v3": True,
            "ocr_executed": False,
        },
        "hash_policy_version": HASH_POLICY_VERSION_V4,
        "counts": {
            "total": len(all_cases),
            "by_split": dict(
                sorted(Counter(row["split"] for row in all_cases).items())
            ),
            "by_city": dict(sorted(Counter(row["city"] for row in all_cases).items())),
            "screenshots_by_split": dict(
                sorted(
                    Counter(
                        row["split"]
                        for row in all_cases
                        if row["input_kind"] == "SYNTHETIC_SCREENSHOT"
                    ).items()
                )
            ),
        },
        "files": {
            "nonblind_cases": _file_entry(NONBLIND_PATH_V4, nonblind_cases),
            "blind_cases": _file_entry(BLIND_INPUT_PATH_V4, blind_cases),
            "nonblind_materializations": _file_entry(
                NONBLIND_MATERIALIZATIONS_PATH_V4, nonblind_materializations
            ),
            "blind_materializations": _file_entry(
                BLIND_MATERIALIZATIONS_PATH_V4, blind_materializations
            ),
        },
        "lanes": {
            "nonblind": {
                "case_count": len(nonblind_cases),
                "materialization_count": len(nonblind_materializations),
                "case_set_hash": case_set_hash_v4(nonblind_cases),
                "materialization_set_hash": materialization_set_hash_v4(
                    nonblind_materializations
                ),
                "oracle_content_changed": False,
                "oracle_schema_version": "trip-check-p5-oracle-v2",
            },
            "frozen_blind": {
                "case_count": len(blind_cases),
                "materialization_count": len(blind_materializations),
                "case_set_hash": case_set_hash_v4(blind_cases),
                "materialization_set_hash": materialization_set_hash_v4(
                    blind_materializations
                ),
                "label_payload_present": False,
                "label_storage": "external_bundle_only",
                "label_access": "isolated_custodian_only",
                "bytes_identical_to_v3": True,
            },
        },
        "contract_hashes": {
            "case_materialization_contract": "v3-unchanged",
            "contracts_v3_path": CONTRACTS_PATH_V3.relative_to(BACKEND_ROOT).as_posix(),
            "contracts_v3_sha256": _canonical_text_file_sha256(CONTRACTS_PATH_V3),
            "dataset_contracts_v4_path": DATASET_CONTRACTS_PATH_V4.relative_to(
                BACKEND_ROOT
            ).as_posix(),
            "dataset_contracts_v4_sha256": _canonical_text_file_sha256(
                DATASET_CONTRACTS_PATH_V4
            ),
            "run_spec_template_path": RUN_SPEC_TEMPLATE_PATH_V4.relative_to(
                BACKEND_ROOT
            ).as_posix(),
            "run_spec_template_sha256": file_sha256(RUN_SPEC_TEMPLATE_PATH_V4),
            "judge_rubric_path": JUDGE_RUBRIC_PATH_V2.relative_to(BACKEND_ROOT).as_posix(),
            "judge_rubric_sha256": file_sha256(JUDGE_RUBRIC_PATH_V2),
            "judge_rubric_semantics_changed": False,
        },
        "route_evidence_repairs": _manifest_route_repairs(nonblind_cases),
        "source_v3_anchor": source,
        "evidence_boundary": {
            "controlled_fixture": "MATERIALIZED_V3_WITH_TWO_P1_ROUTE_BINDINGS",
            "fresh_actual_ocr_execution": "NOT_RUN",
            "live_provider": "NOT_RUN",
            "public_e2e": "NOT_RUN",
            "human_evidence": "NOT_RUN",
        },
    }
    pending = deepcopy(manifest)
    pending["manifest_hash"] = digest(pending)
    if sealing_commitment is not None:
        manifest["sealing_commitment"] = _validated_sealing_commitment_v4(
            sealing_commitment,
            pending_manifest_hash=pending["manifest_hash"],
            blind_cases=blind_cases,
            blind_materializations=blind_materializations,
        )
        manifest["frozen"] = True
        manifest["formal_validation_eligible"] = True
        manifest["seal_status"] = "SEALED"
    manifest["manifest_hash"] = digest(manifest)
    return manifest


def write_dataset_v4(
    *,
    nonblind_cases: list[dict[str, Any]],
    blind_cases: list[dict[str, Any]],
    nonblind_materializations: list[dict[str, Any]],
    blind_materializations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write only v4 paths while guarding every v2/v3/P1 source byte."""

    if BLIND_SEAL_PATH_V4.exists():
        raise ValueError("P5_V4_SEALED_DATASET_IMMUTABLE")
    guarded = [
        *_EXPECTED_V3_PATHS.values(),
        MANIFEST_PATH_V3,
        BLIND_SEAL_PATH_V3,
        BLIND_SEAL_PATH_V2,
        SOURCE_ACTIVE_CONTRACT_V3_PATH,
        *(Path(item["source_path"]) for item in _AUTHORIZED_ROUTE_REPAIRS.values()),
    ]
    before = {path: file_sha256(path) for path in guarded}
    write_jsonl(NONBLIND_PATH_V4, nonblind_cases)
    write_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V4, nonblind_materializations)
    BLIND_INPUT_PATH_V4.write_bytes(BLIND_INPUT_PATH_V3.read_bytes())
    BLIND_MATERIALIZATIONS_PATH_V4.write_bytes(
        BLIND_MATERIALIZATIONS_PATH_V3.read_bytes()
    )
    if load_jsonl(BLIND_INPUT_PATH_V4) != blind_cases or load_jsonl(
        BLIND_MATERIALIZATIONS_PATH_V4
    ) != blind_materializations:
        raise ValueError("P5 v4 blind byte copy differs from in-memory source")
    after = {path: file_sha256(path) for path in guarded}
    if before != after:
        raise ValueError("P5 v2/v3/P1 source bytes changed while writing v4")
    manifest = build_manifest_v4(
        nonblind_cases=nonblind_cases,
        blind_cases=blind_cases,
        nonblind_materializations=nonblind_materializations,
        blind_materializations=blind_materializations,
    )
    write_json(MANIFEST_PATH_V4, manifest)
    return manifest


__all__ = [
    "BLIND_INPUT_PATH_V4",
    "BLIND_MATERIALIZATIONS_PATH_V4",
    "BLIND_SEAL_PATH_V4",
    "DATASET_CONTRACTS_PATH_V4",
    "DATASET_ID_V4",
    "MANIFEST_PATH_V4",
    "NONBLIND_MATERIALIZATIONS_PATH_V4",
    "NONBLIND_PATH_V4",
    "RUN_SPEC_TEMPLATE_PATH_V4",
    "SOURCE_ACTIVE_CONTRACT_V3_PATH",
    "build_dataset_v4",
    "build_manifest_v4",
    "case_set_hash_v4",
    "materialization_set_hash_v4",
    "validate_materialization_v4",
    "validate_v3_source_anchor",
    "write_dataset_v4",
]
