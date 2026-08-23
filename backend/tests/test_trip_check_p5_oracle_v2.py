from __future__ import annotations

import pytest

from evals.trip_check_v1.p5.contracts_v2 import P5CaseV2, P5OracleV2
from evals.trip_check_v1.p5.data_contract import digest


def _oracle() -> P5OracleV2:
    return P5OracleV2(
        task_success_required=True,
        requires_user_resolution=False,
        required_reason_codes=["P5_EVIDENCE_BOUND"],
        wrong_city_or_poi_max=0,
        max_new_blocker_high_unknown=0,
        unknown_must_be_preserved=True,
        advice_required=True,
        specific_place_allowed=True,
        candidate_receipt_mode="REQUIRED",
        expected_strategy_outcome="FEASIBLE",
        concurrency_expectation="NONE",
        ocr_required=False,
    )


def _case(split: str, *, oracle: P5OracleV2 | None, oracle_hash: str | None) -> dict:
    return {
        "case_id": f"p5.{split}.bj.001",
        "split": split,
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": "TEXT",
        "difficulty": "HARD",
        "coverage_tags": ["evidence", "unknown", "text"],
        "product_input": {"source_type": "MANUAL_TEXT", "raw_text": "北京2日行程"},
        "normalized_input_sha256": "1" * 64,
        "materialization": {
            "materialization_id": "materialization-1",
            "materialization_sha256": "2" * 64,
            "source_payload": {"artifact_id": "source", "schema_version": "source-v2", "content_sha256": "3" * 64},
            "provider_snapshot": {"artifact_id": "provider", "schema_version": "provider-v2", "content_sha256": "4" * 64},
            "evidence_snapshot": {"artifact_id": "evidence", "schema_version": "evidence-v2", "content_sha256": "5" * 64},
            "fault_script": {"artifact_id": "fault", "schema_version": "fault-v2", "content_sha256": "6" * 64},
        },
        "runner_control": {},
        "lineage": {},
        "source_ref": {},
        "provenance": {},
        "oracle": oracle.model_dump(mode="json") if oracle else None,
        "oracle_sha256": oracle_hash,
        "case_hash": "7" * 64,
    }


def test_nonblind_oracle_is_strict_and_hash_bound() -> None:
    oracle = _oracle()
    case = P5CaseV2.model_validate(
        _case("dev", oracle=oracle, oracle_hash=digest(oracle.model_dump(mode="json")))
    )
    assert case.oracle == oracle

    with pytest.raises(ValueError, match="oracle_sha256"):
        P5CaseV2.model_validate(_case("dev", oracle=oracle, oracle_hash="0" * 64))


def test_blind_case_rejects_oracle_even_when_hash_is_valid() -> None:
    oracle = _oracle()
    with pytest.raises(ValueError, match="cannot contain oracle"):
        P5CaseV2.model_validate(
            _case(
                "frozen_blind",
                oracle=oracle,
                oracle_hash=digest(oracle.model_dump(mode="json")),
            )
        )
