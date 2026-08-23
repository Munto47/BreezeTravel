from __future__ import annotations

from copy import deepcopy

from evals.trip_check_v1.p5.semantic_contract_v3 import validate_case_semantics_v3


def _receipt(receipt_id: str) -> dict:
    return {
        "receipt_id": receipt_id,
        "operation": "place.search",
        "status": "SUCCEEDED",
    }


def _case(*, requires_resolution: bool = False) -> dict:
    return {
        "case_id": "p5.v3.test.001",
        "city": "北京",
        "input_kind": "TEXT",
        "product_input": {
            "source_type": "MANUAL_TEXT",
            "raw_text": "北京2人。第1天 09:00 天坛公园。",
        },
        "runner_control": {"candidate_set_mode": "VALID"},
        "oracle": {"requires_user_resolution": requires_resolution},
    }


def _materialization() -> dict:
    return {
        "case_id": "p5.v3.test.001",
        "source_payload": {
            "stops": [{"place_id": "bj-temple-of-heaven"}],
            "entity_resolutions": [
                {
                    "ordinal": 0,
                    "day_index": 0,
                    "raw_name": "天坛公园",
                    "normalized_name": "天坛公园",
                    "outcome": "AUTO_RESOLVED",
                    "selected_place_id": "bj-temple-of-heaven",
                    "search_receipt_id": "search-1",
                    "candidates": [
                        {
                            "place_id": "bj-temple-of-heaven",
                            "name": "天坛公园",
                            "city": "北京",
                        }
                    ],
                }
            ],
        },
        "receipts": [_receipt("search-1")],
    }


def test_v3_semantic_contract_accepts_receipt_bound_auto_resolution() -> None:
    assert validate_case_semantics_v3(_case(), _materialization()) == []


def test_v3_semantic_contract_rejects_missing_resolution_contract() -> None:
    materialization = _materialization()
    del materialization["source_payload"]["entity_resolutions"]

    assert validate_case_semantics_v3(_case(), materialization) == [
        "p5.v3.test.001: ENTITY_RESOLUTION_CONTRACT_MISSING"
    ]


def test_v3_semantic_contract_accepts_receipt_bound_ambiguity() -> None:
    case = _case(requires_resolution=True)
    materialization = _materialization()
    resolution = materialization["source_payload"]["entity_resolutions"][0]
    resolution.update(
        {
            "outcome": "NEEDS_CONFIRMATION",
            "selected_place_id": None,
            "candidates": [
                {"place_id": "bj-national-museum", "name": "中国国家博物馆", "city": "北京"},
                {"place_id": "bj-capital-museum", "name": "首都博物馆", "city": "北京"},
            ],
        }
    )
    materialization["source_payload"]["stops"] = []

    assert validate_case_semantics_v3(case, materialization) == []


def test_v3_semantic_contract_rejects_oracle_materialization_contradiction() -> None:
    errors = validate_case_semantics_v3(_case(requires_resolution=True), _materialization())

    assert errors == [
        "p5.v3.test.001: ORACLE_RESOLUTION_CONTRADICTION oracle=True evidence=False"
    ]


def test_v3_semantic_contract_rejects_unreceipted_hard_rejection() -> None:
    case = _case(requires_resolution=True)
    materialization = deepcopy(_materialization())
    resolution = materialization["source_payload"]["entity_resolutions"][0]
    resolution.update(
        {
            "outcome": "HARD_REJECTED",
            "selected_place_id": None,
            "search_receipt_id": "missing",
            "candidates": [
                {"place_id": "sh-bund", "name": "外滩", "city": "上海"},
            ],
        }
    )
    materialization["source_payload"]["stops"] = []

    assert validate_case_semantics_v3(case, materialization) == [
        "p5.v3.test.001: RESOLUTION_SEARCH_RECEIPT_INVALID ordinal=0"
    ]
