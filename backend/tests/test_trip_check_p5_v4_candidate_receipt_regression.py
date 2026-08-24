"""Non-blind regression-next coverage for P5 v4 candidate receipts.

The cases in this module are synthetic test fixtures only.  They are not part
of the frozen 360-case dataset and deliberately exercise the v3 deterministic
semantics inherited by the P5 v4 scorer.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

import pytest

from evals.trip_check_v1.p5.contracts_v2 import P5OracleV2
from evals.trip_check_v1.p5.contracts_v3 import (
    P5CaseV3,
    TerminalStatusV3,
)
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.nonblind_scorer_v4 import _project_terminal_to_v3
from evals.trip_check_v1.p5.runner_v4 import (
    P5TerminalOutputV4,
    semantic_output_hash_v4,
)
from evals.trip_check_v1.p5.scorer_v3 import P5CaseScoreV3, score_case_v3


_CANDIDATE_SET_HASH = digest({"fixture": "candidate-set"})
_MATERIALIZATION_HASH = digest({"fixture": "materialization"})
_INPUT_HASH = digest({"fixture": "product-input"})
_PROVIDER_HASH = digest({"fixture": "provider-snapshot"})
_EVIDENCE_HASH = digest({"fixture": "evidence-snapshot"})
_FAULT_HASH = digest({"fixture": "fault-script"})
_RUN_SPEC_HASH = digest({"fixture": "run-spec"})

_CANDIDATES = [
    {
        "canonical_place_id": "synthetic-place-a",
        "place_receipt_id": "place-a",
        "route_receipt_ids": ["route-a-in", "route-a-out"],
    },
    {
        "canonical_place_id": "synthetic-place-b",
        "place_receipt_id": "place-b",
        "route_receipt_ids": ["route-b-in"],
    },
]


def _artifact(artifact_id: str, schema_version: str, content_sha256: str) -> dict:
    return {
        "artifact_id": artifact_id,
        "schema_version": schema_version,
        "content_sha256": content_sha256,
    }


def _synthetic_case(*, requires_user_resolution: bool) -> P5CaseV3:
    oracle = P5OracleV2(
        task_success_required=True,
        requires_user_resolution=requires_user_resolution,
        required_reason_codes=[],
        wrong_city_or_poi_max=0,
        max_new_blocker_high_unknown=0,
        unknown_must_be_preserved=False,
        advice_required=False,
        specific_place_allowed=True,
        candidate_receipt_mode="REQUIRED",
        expected_strategy_outcome="FEASIBLE",
        concurrency_expectation="NONE",
        ocr_required=False,
    )
    payload = {
        "schema_version": "trip-check-p5-eval-case-v3",
        "case_id": (
            "p5.regression-next.synthetic.candidate-user-resolution"
            if requires_user_resolution
            else "p5.regression-next.synthetic.candidate-selected"
        ),
        "split": "regression",
        "city": "杭州",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": "TEXT",
        "difficulty": "HARD",
        "coverage_tags": [
            "regression-next",
            "candidate-receipt",
            "terminal-propagation",
        ],
        "product_input": {"text": "全新合成的杭州两日行程 receipt 回归输入"},
        "normalized_input_sha256": _INPUT_HASH,
        "materialization": {
            "schema_version": "trip-check-p5-materialization-binding-v3",
            "materialization_id": "synthetic-candidate-receipt-materialization",
            "materialization_sha256": _MATERIALIZATION_HASH,
            "source_payload": _artifact(
                "synthetic-source", "trip-check-source-payload-test-v1", _INPUT_HASH
            ),
            "provider_snapshot": _artifact(
                "synthetic-provider",
                "trip-check-provider-snapshot-test-v1",
                _PROVIDER_HASH,
            ),
            "evidence_snapshot": _artifact(
                "synthetic-evidence",
                "trip-check-evidence-snapshot-test-v1",
                _EVIDENCE_HASH,
            ),
            "candidate_sets": [
                _artifact(
                    "synthetic-candidates",
                    "trip-check-candidate-set-test-v1",
                    _CANDIDATE_SET_HASH,
                )
            ],
            "fault_script": _artifact(
                "synthetic-fault", "trip-check-fault-script-test-v1", _FAULT_HASH
            ),
        },
        "runner_control": {"fault_profile_id": "NONE"},
        "lineage": {"source_family": "synthetic-regression-next-receipt"},
        "source_ref": {"source_id": "synthetic-receipt-regression"},
        "provenance": {
            "contains_human_data": False,
            "generated_by": "deterministic-test-fixture",
        },
        "oracle": oracle.model_dump(mode="json"),
        "oracle_sha256": digest(oracle.model_dump(mode="json")),
    }
    return P5CaseV3.model_validate({**payload, "case_hash": digest(payload)})


def _synthetic_materialization() -> dict:
    return {
        "candidate_sets": [
            {
                "content_sha256": _CANDIDATE_SET_HASH,
                "candidate_set": {"candidates": deepcopy(_CANDIDATES)},
            }
        ]
    }


def _receipt(receipt_id: str) -> dict:
    return {"receipt_id": receipt_id, "status": "PASS"}


def _terminal(
    case: P5CaseV3,
    *,
    selected_place_ids: list[str],
    receipt_ids: list[str],
) -> P5TerminalOutputV4:
    terminal_status = (
        TerminalStatusV3.NEEDS_USER_RESOLUTION
        if case.oracle and case.oracle.requires_user_resolution
        else TerminalStatusV3.SUCCEEDED
    )
    payload = {
        "schema_version": "trip-check-p5-terminal-output-v4",
        "case_id": case.case_id,
        "split": case.split,
        "city": case.city,
        "input_kind": case.input_kind,
        "input_hash": case.normalized_input_sha256,
        "materialization_hash": case.materialization.materialization_sha256,
        "provider_snapshot_hash": _PROVIDER_HASH,
        "evidence_snapshot_hash": _EVIDENCE_HASH,
        "candidate_set_hashes": [_CANDIDATE_SET_HASH],
        "fault_script_hash": _FAULT_HASH,
        "run_spec_hash": _RUN_SPEC_HASH,
        "variant_id": "core_b",
        "adapter_version": "core-b-v4",
        "repair_strategy": "CORE_HEURISTIC",
        "terminal_status": terminal_status,
        "capability_outcomes": {},
        "native_output": {},
        "evaluation_projection": {
            "requires_user_resolution": bool(
                case.oracle and case.oracle.requires_user_resolution
            ),
            "selected_place_ids": selected_place_ids,
            "candidate_receipt_coverage": 1.0,
            "wrong_city_or_poi_count": 0,
            "repair_adoption_attempted": False,
            "replay_side_effect_counts_equal": True,
        },
        "findings": [],
        "advice": [],
        "postcheck": None,
        "receipts": [_receipt(receipt_id) for receipt_id in receipt_ids],
        "latency_ms": 1.0,
        "token_count": 0,
        "cost_usd": 0.0,
        "error_category": None,
        "raw_artifact_hash": digest({"fixture": "raw-terminal"}),
        "semantic_output_hash": "0" * 64,
        "replay_hash": "0" * 64,
    }
    provisional = P5TerminalOutputV4.model_validate(payload)
    semantic_hash = semantic_output_hash_v4(provisional)
    return P5TerminalOutputV4.model_validate(
        {
            **payload,
            "semantic_output_hash": semantic_hash,
            "replay_hash": semantic_hash,
        }
    )


def _mutate_terminal(
    terminal: P5TerminalOutputV4,
    mutation: Literal["place_receipt", "route_receipt", "coverage"],
    *,
    target_candidate: Literal["a", "b"],
) -> P5TerminalOutputV4:
    payload = terminal.model_dump(mode="json")
    if mutation == "coverage":
        payload["evaluation_projection"].pop("candidate_receipt_coverage")
    else:
        removed_receipt_id = (
            f"place-{target_candidate}"
            if mutation == "place_receipt"
            else f"route-{target_candidate}-in"
        )
        payload["receipts"] = [
            receipt
            for receipt in payload["receipts"]
            if receipt.get("receipt_id") != removed_receipt_id
        ]
    payload["semantic_output_hash"] = "0" * 64
    payload["replay_hash"] = "0" * 64
    provisional = P5TerminalOutputV4.model_validate(payload)
    semantic_hash = semantic_output_hash_v4(provisional)
    return P5TerminalOutputV4.model_validate(
        {
            **payload,
            "semantic_output_hash": semantic_hash,
            "replay_hash": semantic_hash,
        }
    )


def _score(
    case: P5CaseV3,
    terminal: P5TerminalOutputV4,
    materialization: dict,
) -> P5CaseScoreV3:
    return score_case_v3(
        case,
        _project_terminal_to_v3(terminal),
        materialization=materialization,
    )


@pytest.mark.parametrize("mutation", ["place_receipt", "route_receipt", "coverage"])
def test_selected_candidate_requires_complete_terminal_receipts(
    mutation: Literal["place_receipt", "route_receipt", "coverage"],
) -> None:
    case = _synthetic_case(requires_user_resolution=False)
    materialization = _synthetic_materialization()
    terminal = _terminal(
        case,
        selected_place_ids=["synthetic-place-a"],
        receipt_ids=["place-a", "route-a-in", "route-a-out"],
    )

    baseline = _score(case, terminal, materialization)
    assert baseline.candidate_receipt_coverage == "PASS"
    assert baseline.deterministic_pass is True

    mutated = _mutate_terminal(terminal, mutation, target_candidate="a")
    score = _score(case, mutated, materialization)
    assert score.candidate_receipt_coverage == "FAIL"
    assert "CANDIDATE_RECEIPT_VIOLATION" in score.deterministic_failure_codes


@pytest.mark.parametrize("mutation", ["place_receipt", "route_receipt", "coverage"])
def test_user_resolution_requires_all_displayed_candidate_receipts(
    mutation: Literal["place_receipt", "route_receipt", "coverage"],
) -> None:
    case = _synthetic_case(requires_user_resolution=True)
    materialization = _synthetic_materialization()
    terminal = _terminal(
        case,
        selected_place_ids=[],
        receipt_ids=[
            "place-a",
            "route-a-in",
            "route-a-out",
            "place-b",
            "route-b-in",
        ],
    )

    baseline = _score(case, terminal, materialization)
    assert baseline.candidate_receipt_coverage == "PASS"
    assert baseline.deterministic_pass is True

    mutated = _mutate_terminal(terminal, mutation, target_candidate="b")
    score = _score(case, mutated, materialization)
    assert score.candidate_receipt_coverage == "FAIL"
    assert "CANDIDATE_RECEIPT_VIOLATION" in score.deterministic_failure_codes
