from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.dual_entry_scorer import (
    aggregate_metric_scores,
    evaluate_metric_thresholds,
    finding_signature,
    score_metric_oracles,
)


def _na() -> dict:
    return {"applicability": "N_A", "reason_code": "NO_TRUTH"}


def _label(metric: str, oracle: dict, *, case_id: str = "dev.test") -> dict:
    oracles = {name: _na() for name in (
        "parse_f1",
        "entity_precision_recall",
        "finding_precision_recall",
        "repair_postcheck",
        "builder_ndcg_at_5",
        "builder_recall_at_5",
    )}
    oracles[metric] = {"applicability": "APPLICABLE", **oracle}
    return {"case_id": case_id, "metric_oracles": oracles}


def test_parse_set_f1_normalizes_unicode_and_spaces_and_collapses_duplicates():
    label = _label(
        "parse_f1",
        {
            "metric_version": "set-f1-v1",
            "normalization": "unicode-nfc-trim-collapse-space",
            "ground_truth_items": [{"stop_name": "咖啡厅"}, {"stop_name": "故宫 博物院"}],
        },
    )
    score = score_metric_oracles(
        label,
        {
            "parse_items": [
                {"stop_name": "  咖啡\u0301厅  ".replace("啡\u0301", "啡")},
                {"stop_name": "故宫\t博物院"},
                {"stop_name": "故宫  博物院"},
                {"stop_name": "天坛"},
            ]
        },
    )["metrics"]["parse_f1"]

    assert score["status"] == "SCORED"
    assert score["duplicate_actual_count"] == 1
    assert score["components"]["true_positive"] == 2
    assert score["components"]["false_positive"] == 1
    assert score["components"]["false_negative"] == 0
    assert score["value"] == pytest.approx(0.8)


def test_parse_present_but_empty_is_zero_not_invalid_and_missing_is_invalid():
    label = _label(
        "parse_f1",
        {"metric_version": "set-f1-v1", "ground_truth_items": [{"stop_name": "故宫"}]},
    )
    empty = score_metric_oracles(label, {"parse_items": []})["metrics"]["parse_f1"]
    missing = score_metric_oracles(label, {})["metrics"]["parse_f1"]

    assert empty["status"] == "SCORED"
    assert empty["value"] == 0
    assert missing["status"] == "UNSCORED"
    assert missing["reason_code"] == "ACTUAL_FIELD_MISSING"


def test_entity_exact_set_scores_false_positive_and_provider_independent_id_selector():
    label = _label(
        "entity_precision_recall",
        {
            "metric_version": "exact-set-precision-recall-v1",
            "ground_truth_items": [
                {"raw_name": "景山公园", "status": "AUTO_MATCHED", "canonical_place_id": None},
                {"raw_name": "西湖", "status": "NOT_FOUND", "canonical_place_id": None},
            ],
        },
    )
    score = score_metric_oracles(
        label,
        {
            "entity_items": [
                {"raw_name": "景山公园", "status": "AUTO_MATCHED", "canonical_place_id": "amap-1"},
                {"raw_name": "西湖", "status": "NOT_FOUND", "canonical_place_id": None},
                {"raw_name": "西湖", "status": "AUTO_MATCHED", "canonical_place_id": "wrong-city"},
            ]
        },
    )["metrics"]["entity_precision_recall"]

    assert score["components"]["true_positive"] == 2
    assert score["components"]["false_positive"] == 1
    assert score["components"]["false_negative"] == 0


def test_finding_unknown_and_semantic_star_selector_match_but_satisfied_does_not():
    label = _label(
        "finding_precision_recall",
        {
            "metric_version": "exact-set-precision-recall-v1",
            "ground_truth_items": [
                {
                    "reason_code": "TIME_DATA_INVALID",
                    "status": "UNKNOWN",
                    "subject": "day:*",
                    "affected_member": None,
                }
            ],
        },
    )
    matched = score_metric_oracles(
        label,
        {
            "finding_items": [
                {
                    "reason_code": "TIME_DATA_INVALID",
                    "status": "UNKNOWN",
                    "subject": "day:2",
                    "affected_member": None,
                }
            ]
        },
    )["metrics"]["finding_precision_recall"]
    wrong_status = score_metric_oracles(
        label,
        {
            "finding_items": [
                {
                    "reason_code": "TIME_DATA_INVALID",
                    "status": "SATISFIED",
                    "subject": "day:2",
                    "affected_member": None,
                }
            ]
        },
    )["metrics"]["finding_precision_recall"]

    assert matched["value"] == 1
    assert wrong_status["value"] == 0


def test_finding_duplicates_are_independent_predictions_and_cannot_double_match():
    label = _label(
        "finding_precision_recall",
        {
            "metric_version": "exact-set-precision-recall-v1",
            "ground_truth_items": [
                {"reason_code": "DUPLICATE_PLACE", "status": "VIOLATED", "subject": "west-lake", "affected_member": None}
            ],
        },
    )
    item = {
        "reason_code": "DUPLICATE_PLACE",
        "status": "VIOLATED",
        "subject": "west-lake",
        "affected_member": None,
    }
    score = score_metric_oracles(label, {"finding_items": [item, item]})["metrics"]["finding_precision_recall"]

    assert score["components"]["true_positive"] == 1
    assert score["components"]["false_positive"] == 1
    assert score["components"]["precision"]["value"] == 0.5


def test_blocker_high_finding_scope_keeps_high_unknown_and_supports_clean_truth():
    label = _label(
        "finding_precision_recall",
        {
            "metric_version": "exact-set-blocker-high-v1",
            "unit_key_fields": ["reason_code", "status", "subject", "affected_member"],
            "scope_severities": ["BLOCKER", "HIGH"],
            "ground_truth_items": [
                {
                    "reason_code": "OPENING_HOURS_MISSING",
                    "status": "UNKNOWN",
                    "subject": "poi-1",
                    "affected_member": None,
                }
            ],
        },
    )
    score = score_metric_oracles(
        label,
        {
            "finding_items": [
                {
                    "reason_code": "OPENING_HOURS_MISSING",
                    "status": "UNKNOWN",
                    "severity": "HIGH",
                    "subject": "poi-1",
                    "affected_member": None,
                },
                {
                    "reason_code": "MEAL_WINDOW_EMPTY",
                    "status": "VIOLATED",
                    "severity": "MEDIUM",
                    "subject": "day:1",
                    "affected_member": None,
                },
            ]
        },
    )["metrics"]["finding_precision_recall"]
    assert score["value"] == 1

    clean_label = _label(
        "finding_precision_recall",
        {
            "metric_version": "exact-set-blocker-high-v1",
            "unit_key_fields": ["reason_code", "status", "subject", "affected_member"],
            "scope_severities": ["BLOCKER", "HIGH"],
            "ground_truth_items": [],
        },
    )
    clean = score_metric_oracles(clean_label, {"finding_items": []})["metrics"]["finding_precision_recall"]
    assert clean["status"] == "SCORED"
    assert clean["value"] == 1


def test_finding_signature_uses_canonical_duplicate_and_overlap_semantic_selectors():
    duplicate = finding_signature(
        {
            "reason_code": "DUPLICATE_PLACE",
            "status": "VIOLATED",
            "input_values": {"place_id": "hz-west-lake"},
            "affected_stop_ids": ["stop-a", "stop-b"],
        },
        stop_names_by_id={"stop-a": "西湖", "stop-b": "西湖景区"},
    )
    overlap = finding_signature(
        {
            "reason_code": "TIME_CHAIN_BROKEN",
            "status": "VIOLATED",
            "input_values": {
                "day_stops": [
                    {"stop_id": "stop-a", "start_time": "09:00", "end_time": "11:00"},
                    {"stop_id": "stop-b", "start_time": "10:30", "end_time": "12:00"},
                ]
            },
            "affected_days": [0],
        },
        stop_names_by_id={"stop-a": "颐和园", "stop-b": "天坛公园"},
    )

    assert duplicate["subject"] == "hz-west-lake"
    assert "severity" in duplicate
    assert overlap["subject"] == "颐和园->天坛公园"


def test_repair_predicates_operation_allowlist_and_option_limit_are_all_scored():
    label = _label(
        "repair_postcheck",
        {
            "metric_version": "predicate-pass-rate-v1",
            "max_options": 1,
            "allowed_operation_types": ["MOVE", "SHIFT"],
            "required_predicates": [
                {"predicate": "postcheck_executed", "expected": True},
                {"predicate": "locked_items_preserved", "expected": True},
                {"predicate": "no_new_hard_violation", "expected": True},
            ],
        },
    )
    score = score_metric_oracles(
        label,
        {
            "repair_options": [
                {
                    "operation_types": ["REPLACE"],
                    "predicates": {
                        "postcheck_executed": True,
                        "locked_items_preserved": True,
                        "no_new_hard_violation": False,
                    },
                },
                {
                    "operation_types": ["MOVE"],
                    "predicates": {
                        "postcheck_executed": True,
                        "locked_items_preserved": True,
                        "no_new_hard_violation": True,
                    },
                },
            ]
        },
    )["metrics"]["repair_postcheck"]

    assert score["numerator"] == 2
    assert score["denominator"] == 5
    assert score["value"] == 0.4
    empty = score_metric_oracles(label, {"repair_options": []})
    assert empty["status"] == "INVALID"
    assert empty["metrics"]["repair_postcheck"]["status"] == "UNSCORED"


def test_graded_ndcg_uses_grades_and_order_while_recall_uses_relevant_set():
    ndcg_label = _label(
        "builder_ndcg_at_5",
        {
            "metric_version": "ndcg-v1",
            "k": 5,
            "relevance_items": [
                {"candidate_id": "best", "relevance_grade": 5},
                {"candidate_id": "good", "relevance_grade": 2},
            ],
        },
    )
    best = score_metric_oracles(ndcg_label, {"ranked_candidate_ids": ["best", "good"]})["metrics"]["builder_ndcg_at_5"]
    reversed_score = score_metric_oracles(ndcg_label, {"ranked_candidate_ids": ["good", "best"]})["metrics"]["builder_ndcg_at_5"]
    duplicate = score_metric_oracles(ndcg_label, {"ranked_candidate_ids": ["best", "best"]})["metrics"]["builder_ndcg_at_5"]

    assert best["value"] == 1
    assert 0 < reversed_score["value"] < 1
    assert duplicate["status"] == "INVALID"

    recall_label = _label(
        "builder_recall_at_5",
        {"metric_version": "recall-at-k-v1", "k": 5, "relevant_candidate_ids": ["a", "b", "c"]},
    )
    recall = score_metric_oracles(recall_label, {"ranked_candidate_ids": ["x", "a", "b"]})["metrics"]["builder_recall_at_5"]
    assert recall["numerator"] == 2
    assert recall["denominator"] == 3


def test_aggregation_reports_raw_denominators_coverage_and_case_ids_not_only_mean():
    label_a = _label(
        "parse_f1",
        {"metric_version": "set-f1-v1", "ground_truth_items": [{"stop_name": "A"}, {"stop_name": "B"}]},
        case_id="case-a",
    )
    label_b = _label(
        "parse_f1",
        {"metric_version": "set-f1-v1", "ground_truth_items": [{"stop_name": "C"}]},
        case_id="case-b",
    )
    label_na = _label("builder_recall_at_5", _na(), case_id="case-na")
    scores = [
        score_metric_oracles(label_a, {"parse_items": [{"stop_name": "A"}]}),
        score_metric_oracles(label_b, {}),
        score_metric_oracles(label_na, {}),
    ]
    aggregate = aggregate_metric_scores(scores)
    parse = aggregate["metrics"]["parse_f1"]

    assert parse["coverage"] == {"numerator": 1, "denominator": 2, "value": 0.5}
    assert parse["applicable_case_ids"] == ["case-a", "case-b"]
    assert parse["scored_case_ids"] == ["case-a"]
    assert parse["invalid_case_ids"] == ["case-b"]
    assert "case-na" in parse["n_a_case_ids"]


def test_threshold_gate_fails_closed_for_incomplete_coverage_or_no_denominator():
    applicable = _label(
        "parse_f1",
        {"metric_version": "set-f1-v1", "ground_truth_items": [{"stop_name": "A"}]},
        case_id="invalid-case",
    )
    aggregate = aggregate_metric_scores([score_metric_oracles(applicable, {})])
    gate = evaluate_metric_thresholds(aggregate, {"parse_f1": 0.95})[0]
    assert gate["status"] == "FAIL"
    assert "METRIC_COVERAGE_INCOMPLETE" in gate["reason_codes"]

    no_denominator = aggregate_metric_scores([score_metric_oracles(_label("parse_f1", _na()), {})])
    gate = evaluate_metric_thresholds(no_denominator, {"parse_f1": 0.95})[0]
    assert gate["status"] == "FAIL"
    assert "NO_APPLICABLE_DENOMINATOR" in gate["reason_codes"]

    release_aliases = evaluate_metric_thresholds(
        no_denominator,
        {"auto_match_precision": 0.98, "blocker_high_precision": 0.9, "blocker_high_recall": 0.85},
    )
    assert {item["id"] for item in release_aliases} == {
        "METRIC_THRESHOLD:auto_match_precision",
        "METRIC_THRESHOLD:blocker_high_precision",
        "METRIC_THRESHOLD:blocker_high_recall",
    }
    assert all(item["status"] == "FAIL" for item in release_aliases)


def test_all_78_development_oracles_are_consumable_and_na_never_enters_denominator():
    data_root = Path(__file__).resolve().parents[1] / "eval_data" / "dual_entry_v1"
    labels = [
        json.loads(line)
        for path in data_root.rglob("*.labels.jsonl")
        if "sealed" not in path.parts
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    scores = [score_metric_oracles(label, {}) for label in labels]
    aggregate = aggregate_metric_scores(scores)["metrics"]
    expected_denominators = {
        "parse_f1": 20,
        "entity_precision_recall": 19,
        "finding_precision_recall": 31,
        "repair_postcheck": 10,
        "builder_ndcg_at_5": 11,
        "builder_recall_at_5": 13,
    }

    assert len(scores) == 78
    for metric, denominator in expected_denominators.items():
        assert aggregate[metric]["coverage"]["denominator"] == denominator
        assert aggregate[metric]["coverage"]["numerator"] == 0
        assert len(aggregate[metric]["n_a_case_ids"]) == 78 - denominator
        assert len(aggregate[metric]["unscored_case_ids"]) == denominator
