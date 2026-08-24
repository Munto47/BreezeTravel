from __future__ import annotations

from copy import deepcopy

from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3
from evals.trip_check_v1.p5.data_contract import load_jsonl
from evals.trip_check_v1.p5.data_contract_v3 import (
    BLIND_INPUT_PATH_V3,
    BLIND_MATERIALIZATIONS_PATH_V3,
    NONBLIND_MATERIALIZATIONS_PATH_V3,
    NONBLIND_PATH_V3,
    write_dataset_v3,
)
from evals.trip_check_v1.p5.data_contract_v4 import (
    BLIND_INPUT_PATH_V4,
    BLIND_MATERIALIZATIONS_PATH_V4,
    NONBLIND_MATERIALIZATIONS_PATH_V4,
    NONBLIND_PATH_V4,
    build_dataset_v4,
    validate_materialization_v4,
)


TARGETS = {"p5.pilot.bj.004", "p5.pilot.sh.001"}


def _by_id(path):
    return {row["case_id"]: row for row in load_jsonl(path)}


def _route_fact(row):
    return next(
        item
        for item in row["evidence_snapshot"]["snapshot"]["facts"]
        if item["subject_type"] == "ROUTE_EDGE" and item["fact_type"] == "ROUTE_TIME"
    )


def _route_receipt(row):
    return next(item for item in row["receipts"] if item["operation"] == "route.audit")


def test_v4_changes_only_two_nonblind_route_durations_and_hash_cascade() -> None:
    nonblind_v3 = _by_id(NONBLIND_PATH_V3)
    nonblind_v4 = _by_id(NONBLIND_PATH_V4)
    materializations_v3 = _by_id(NONBLIND_MATERIALIZATIONS_PATH_V3)
    materializations_v4 = _by_id(NONBLIND_MATERIALIZATIONS_PATH_V4)
    assert set(nonblind_v3) == set(nonblind_v4)
    assert {
        case_id for case_id in nonblind_v3 if nonblind_v3[case_id] != nonblind_v4[case_id]
    } == TARGETS
    assert {
        case_id
        for case_id in materializations_v3
        if materializations_v3[case_id] != materializations_v4[case_id]
    } == TARGETS

    for case_id in TARGETS:
        before = materializations_v3[case_id]
        after = materializations_v4[case_id]
        before_fact = _route_fact(before)
        after_fact = _route_fact(after)
        assert before_fact["value"]["duration_minutes"] == 20
        assert after_fact["value"]["duration_minutes"] == 90
        before_fact_projection = deepcopy(before_fact)
        after_fact_projection = deepcopy(after_fact)
        for projection in (before_fact_projection, after_fact_projection):
            projection.pop("fact_id")
            projection.pop("response_hash")
            projection["value"].pop("duration_minutes")
        assert before_fact_projection == after_fact_projection

        before_receipt = deepcopy(_route_receipt(before))
        after_receipt = deepcopy(_route_receipt(after))
        old_receipt_id = before_receipt.pop("receipt_id")
        new_receipt_id = after_receipt.pop("receipt_id")
        assert old_receipt_id != new_receipt_id
        before_receipt.pop("response_hash")
        after_receipt.pop("response_hash")
        assert before_receipt == after_receipt
        assert before["provider_snapshot"]["receipt_ids"] != after["provider_snapshot"][
            "receipt_ids"
        ]
        assert before["evidence_snapshot"]["snapshot"]["provider_set"] == after[
            "evidence_snapshot"
        ]["snapshot"]["provider_set"]
        model = P5CaseV3.model_validate(nonblind_v4[case_id])
        assert validate_materialization_v4(model, after) == after


def test_v4_blind_files_are_byte_identical_and_rebuild_is_deterministic() -> None:
    assert BLIND_INPUT_PATH_V4.read_bytes() == BLIND_INPUT_PATH_V3.read_bytes()
    assert (
        BLIND_MATERIALIZATIONS_PATH_V4.read_bytes()
        == BLIND_MATERIALIZATIONS_PATH_V3.read_bytes()
    )
    assert build_dataset_v4() == (
        load_jsonl(NONBLIND_PATH_V4),
        load_jsonl(BLIND_INPUT_PATH_V4),
        load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V4),
        load_jsonl(BLIND_MATERIALIZATIONS_PATH_V4),
    )


def test_v3_seal_overwrite_guard_remains_active() -> None:
    try:
        write_dataset_v3(
            nonblind_cases=[],
            blind_cases=[],
            nonblind_materializations=[],
            blind_materializations=[],
        )
    except ValueError as exc:
        assert str(exc) == "P5_V3_SEALED_DATASET_IMMUTABLE"
    else:  # pragma: no cover - a missing guard would be a destructive regression.
        raise AssertionError("sealed v3 dataset unexpectedly accepted an overwrite")
