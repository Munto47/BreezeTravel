from __future__ import annotations

from copy import deepcopy

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.evidence_materialization_v3 import build_evidence_materialization_v3
from evals.trip_check_v1.p5.semantic_contract_v3 import (
    validate_case_semantics_v3,
    validate_dataset_semantics_v3,
    validate_nonblind_oracle_compatibility_v3,
)


def _pair(
    raw_text: str = "北京2人。第1天 09:00 天坛公园；第2天 09:00 故宫博物院。",
    *,
    requires_resolution: bool = False,
) -> tuple[dict, dict]:
    product_input = {"source_type": "MANUAL_TEXT", "raw_text": raw_text}
    case = {
        "case_id": "p5.v3.test.001",
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": "TEXT",
        "product_input": product_input,
        "normalized_input_sha256": digest(product_input),
        "runner_control": {
            "provider_snapshot_id": "trip-check-p5-controlled-snapshot-v3",
            "fault_profile_id": "advice_completeness",
            "candidate_set_mode": "VALID",
            "evidence_freshness": "FRESH",
            "unknown_required": False,
            "fault_registry_version": "trip-check-p5-fault-registry-v2",
            "budget_profile": "p5-zero-api-v2",
            "seed": 20260823,
        },
        "oracle": {"requires_user_resolution": requires_resolution},
    }
    materialization = build_evidence_materialization_v3(
        {key: value for key, value in case.items() if key != "oracle"}
    )
    return case, materialization


def _label_free(case: dict) -> dict:
    return {key: value for key, value in case.items() if key not in {"oracle", "oracle_sha256"}}


def test_v3_semantic_contract_accepts_receipt_bound_auto_resolution() -> None:
    case, materialization = _pair()

    assert validate_case_semantics_v3(_label_free(case), materialization) == []


def test_v3_semantic_contract_rejects_missing_resolution_contract() -> None:
    case, materialization = _pair()
    del materialization["source_payload"]["entity_resolutions"]

    assert validate_case_semantics_v3(_label_free(case), materialization) == [
        "p5.v3.test.001: ENTITY_RESOLUTION_CONTRACT_MISSING"
    ]


def test_v3_semantic_contract_accepts_receipt_bound_ambiguity() -> None:
    case, materialization = _pair(
        "北京2人。第1天 09:00 博物馆；第2天 09:00 颐和园。",
        requires_resolution=True,
    )

    assert validate_case_semantics_v3(_label_free(case), materialization) == []


def test_v3_semantic_contract_rejects_oracle_materialization_contradiction() -> None:
    case, materialization = _pair(requires_resolution=True)

    errors = validate_nonblind_oracle_compatibility_v3(case, materialization)

    assert errors == [
        "p5.v3.test.001: ORACLE_RESOLUTION_CONTRADICTION oracle=True evidence=False"
    ]


def test_v3_semantic_contract_rejects_unreceipted_hard_rejection() -> None:
    case, materialization = _pair(
        "北京2人。第1天 09:00 东方明珠；第2天 09:00 故宫博物院。",
        requires_resolution=True,
    )
    tampered = deepcopy(materialization)
    tampered["receipts"] = [
        item for item in tampered["receipts"] if item["operation"] != "place.search"
    ]

    errors = validate_case_semantics_v3(_label_free(case), tampered)

    assert any("RESOLUTION_SEARCH_RECEIPT_INVALID" in item for item in errors)


def test_v3_dataset_semantics_requires_exact_unique_case_set() -> None:
    case, materialization = _pair()

    duplicate = validate_dataset_semantics_v3(
        [_label_free(case)], [materialization, materialization]
    )
    extra = {**materialization, "case_id": "p5.v3.extra"}
    mismatch = validate_dataset_semantics_v3([_label_free(case)], [materialization, extra])

    assert duplicate["status"] == "REJECT"
    assert any("DUPLICATE_MATERIALIZATION_CASE_IDS" in item for item in duplicate["errors"])
    assert mismatch["status"] == "REJECT"
    assert any("CASE_MATERIALIZATION_SET_MISMATCH" in item for item in mismatch["errors"])


def test_v3_dataset_semantics_rejects_invalid_materialization_hash() -> None:
    case, materialization = _pair()
    materialization["evidence_materialization_hash"] = "0" * 64

    result = validate_dataset_semantics_v3([_label_free(case)], [materialization])

    assert result["status"] == "REJECT"
    assert any("MATERIALIZATION_INVALID" in item for item in result["errors"])


def test_v3_label_free_semantic_contract_rejects_oracle_input() -> None:
    case, materialization = _pair()

    result = validate_dataset_semantics_v3([case], [materialization])

    assert result["status"] == "REJECT"
    assert result["blind_labels_read"] is False
    assert any("LABEL_FIELDS_FORBIDDEN" in item for item in result["errors"])


def test_v3_semantic_contract_rejects_case_and_runner_drift() -> None:
    case, materialization = _pair()
    label_free = _label_free(case)
    label_free["group_size"] = 5
    label_free["runner_control"] = {**label_free["runner_control"], "seed": 999999}

    errors = validate_case_semantics_v3(label_free, materialization)

    assert any("CASE_SOURCE_BINDING_MISMATCH field=group_size" in item for item in errors)
    assert any("CASE_RUNNER_CONTROL_BINDING_MISMATCH" in item for item in errors)
