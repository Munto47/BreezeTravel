from __future__ import annotations

import fnmatch
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


METRIC_NAMES = (
    "parse_f1",
    "entity_precision_recall",
    "finding_precision_recall",
    "repair_postcheck",
    "builder_ndcg_at_5",
    "builder_recall_at_5",
)


def normalize_text(value: str) -> str:
    """Apply the normalization frozen by ``set-f1-v1`` labels."""

    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value).strip())


def _invalid(metric: str, code: str, detail: str) -> dict[str, Any]:
    return {
        "metric": metric,
        "status": "INVALID",
        "reason_code": code,
        "detail": detail,
    }


def _unscored(metric: str, code: str, detail: str) -> dict[str, Any]:
    return {
        "metric": metric,
        "status": "UNSCORED",
        "reason_code": code,
        "detail": detail,
    }


def _na(metric: str, oracle: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "metric": metric,
        "status": "N_A",
        "reason_code": oracle.get("reason_code", "ORACLE_NOT_APPLICABLE"),
    }


def _f1_components(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    precision = tp / precision_denominator if precision_denominator else 1.0
    recall = tp / recall_denominator if recall_denominator else 1.0
    f1_denominator = precision + recall
    f1 = 2 * precision * recall / f1_denominator if f1_denominator else 0.0
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": {
            "numerator": tp,
            "denominator": precision_denominator,
            "value": precision,
        },
        "recall": {"numerator": tp, "denominator": recall_denominator, "value": recall},
        "f1": {"value": f1},
    }


def _validated_item_list(
    metric: str,
    actuals: Mapping[str, Any],
    field: str,
    required_fields: Sequence[str],
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    if field not in actuals:
        return None, _unscored(metric, "ACTUAL_FIELD_MISSING", f"required product field {field!r} is absent")
    raw = actuals[field]
    if not isinstance(raw, list):
        return None, _invalid(metric, "ACTUAL_FIELD_INVALID", f"product field {field!r} must be a list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return None, _invalid(metric, "ACTUAL_ITEM_INVALID", f"{field}[{index}] must be an object")
        missing = [key for key in required_fields if key not in item]
        if missing:
            return None, _invalid(
                metric,
                "ACTUAL_ITEM_FIELD_MISSING",
                f"{field}[{index}] lacks {', '.join(missing)}",
            )
        result.append(item)
    return result, None


def _score_parse(oracle: Mapping[str, Any], actuals: Mapping[str, Any]) -> dict[str, Any]:
    metric = "parse_f1"
    items, error = _validated_item_list(metric, actuals, "parse_items", ("stop_name",))
    if error:
        return error
    assert items is not None
    if any(not isinstance(item["stop_name"], str) or not normalize_text(item["stop_name"]) for item in items):
        return _invalid(metric, "ACTUAL_ITEM_INVALID", "every parse stop_name must be a non-empty string")
    truth = oracle.get("ground_truth_items")
    if not isinstance(truth, list) or not truth:
        return _invalid(metric, "ORACLE_DENOMINATOR_EMPTY", "parse oracle has no ground-truth items")
    try:
        expected = {normalize_text(item["stop_name"]) for item in truth}
    except (KeyError, TypeError):
        return _invalid(metric, "ORACLE_INVALID", "parse ground truth is malformed")
    actual_values = [normalize_text(item["stop_name"]) for item in items]
    actual = set(actual_values)
    components = _f1_components(len(expected & actual), len(actual - expected), len(expected - actual))
    return {
        "metric": metric,
        "status": "SCORED",
        "metric_version": oracle.get("metric_version"),
        "value": components["f1"]["value"],
        "components": components,
        "duplicate_actual_count": len(actual_values) - len(actual),
    }


def _entity_matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    if normalize_text(str(expected.get("raw_name", ""))) != normalize_text(str(actual.get("raw_name", ""))):
        return False
    expected_status = expected.get("status")
    if expected_status != actual.get("status"):
        return False
    expected_id = expected.get("canonical_place_id")
    actual_id = actual.get("canonical_place_id")
    if expected_id is not None:
        return expected_id == actual_id
    # A null ID in this dataset is a provider-independent selector: unresolved
    # statuses must remain null, while a successful resolution must expose a
    # concrete provider ID without baking that volatile ID into the label.
    if expected_status in {"AUTO_MATCHED", "USER_CONFIRMED"}:
        return isinstance(actual_id, str) and bool(actual_id.strip())
    return actual_id is None


def _selector_matches(expected: Any, actual: Any) -> bool:
    if expected is None:
        return actual is None
    if not isinstance(expected, str) or not isinstance(actual, str):
        return expected == actual
    normalized_expected = normalize_text(expected)
    normalized_actual = normalize_text(actual)
    if "*" in normalized_expected:
        return fnmatch.fnmatchcase(normalized_actual, normalized_expected)
    return normalized_actual == normalized_expected


def _finding_matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return (
        expected.get("reason_code") == actual.get("reason_code")
        and expected.get("status") == actual.get("status")
        and _selector_matches(expected.get("subject"), actual.get("subject"))
        and _selector_matches(expected.get("affected_member"), actual.get("affected_member"))
    )


def _maximum_bipartite_matches(
    expected: Sequence[Mapping[str, Any]],
    actual: Sequence[Mapping[str, Any]],
    matcher,
) -> int:
    adjacency = [[index for index, candidate in enumerate(actual) if matcher(item, candidate)] for item in expected]
    occupied: dict[int, int] = {}

    def visit(expected_index: int, seen: set[int]) -> bool:
        for actual_index in adjacency[expected_index]:
            if actual_index in seen:
                continue
            seen.add(actual_index)
            if actual_index not in occupied or visit(occupied[actual_index], seen):
                occupied[actual_index] = expected_index
                return True
        return False

    return sum(visit(index, set()) for index in range(len(expected)))


def _score_exact_items(
    metric: str,
    oracle: Mapping[str, Any],
    actuals: Mapping[str, Any],
    *,
    actual_field: str,
    required_fields: Sequence[str],
    matcher,
    allow_empty_truth: bool = False,
) -> dict[str, Any]:
    items, error = _validated_item_list(metric, actuals, actual_field, required_fields)
    if error:
        return error
    assert items is not None
    truth = oracle.get("ground_truth_items")
    if not isinstance(truth, list) or (not truth and not allow_empty_truth):
        return _invalid(metric, "ORACLE_DENOMINATOR_EMPTY", f"{metric} oracle has no ground-truth items")
    if any(not isinstance(item, dict) for item in truth):
        return _invalid(metric, "ORACLE_INVALID", f"{metric} ground truth is malformed")
    tp = _maximum_bipartite_matches(truth, items, matcher)
    components = _f1_components(tp, len(items) - tp, len(truth) - tp)
    return {
        "metric": metric,
        "status": "SCORED",
        "metric_version": oracle.get("metric_version"),
        "value": components["f1"]["value"],
        "components": components,
    }


def _score_entity(oracle: Mapping[str, Any], actuals: Mapping[str, Any]) -> dict[str, Any]:
    truth = oracle.get("ground_truth_items")
    scoped_actuals = actuals
    if isinstance(truth, list):
        scoped_names = {
            normalize_text(str(item.get("raw_name", "")))
            for item in truth
            if isinstance(item, dict)
        }
        raw_items = actuals.get("entity_items")
        if isinstance(raw_items, list):
            for index, item in enumerate(raw_items):
                if not isinstance(item, dict):
                    continue
                if (
                    not isinstance(item.get("raw_name"), str)
                    or not normalize_text(item["raw_name"])
                    or item.get("status") not in {"AUTO_MATCHED", "USER_CONFIRMED", "AMBIGUOUS", "NOT_FOUND"}
                    or (
                        item.get("canonical_place_id") is not None
                        and (
                            not isinstance(item.get("canonical_place_id"), str)
                            or not item["canonical_place_id"].strip()
                        )
                    )
                ):
                    return _invalid(
                        "entity_precision_recall",
                        "ACTUAL_ITEM_INVALID",
                        f"entity_items[{index}] has an invalid stable signature",
                    )
            scoped_actuals = {
                **actuals,
                # Entity labels intentionally name the resolutions whose
                # identity decision is grounded. Other parsed stops are N/A,
                # not false-positive identity decisions for this oracle.
                "entity_items": [
                    item
                    for item in raw_items
                    if isinstance(item, dict)
                    and normalize_text(str(item.get("raw_name", ""))) in scoped_names
                ],
            }
    return _score_exact_items(
        "entity_precision_recall",
        oracle,
        scoped_actuals,
        actual_field="entity_items",
        required_fields=("raw_name", "status", "canonical_place_id"),
        matcher=_entity_matches,
    )


def _score_findings(oracle: Mapping[str, Any], actuals: Mapping[str, Any]) -> dict[str, Any]:
    raw_items = actuals.get("finding_items")
    if isinstance(raw_items, list):
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            if (
                not isinstance(item.get("reason_code"), str)
                or not item["reason_code"].strip()
                or item.get("status") not in {"SATISFIED", "VIOLATED", "UNKNOWN"}
                or not isinstance(item.get("subject"), str)
                or not normalize_text(item["subject"])
                or (
                    item.get("affected_member") is not None
                    and not isinstance(item.get("affected_member"), str)
                )
            ):
                return _invalid(
                    "finding_precision_recall",
                    "ACTUAL_ITEM_INVALID",
                    f"finding_items[{index}] has an invalid stable signature",
                )
    scoped_actuals = actuals
    metric_version = oracle.get("metric_version")
    if metric_version == "exact-set-blocker-high-v1":
        severities = oracle.get("scope_severities")
        if severities != ["BLOCKER", "HIGH"]:
            return _invalid(
                "finding_precision_recall",
                "ORACLE_SCOPE_INVALID",
                "exact-set-blocker-high-v1 requires the explicit BLOCKER,HIGH scope",
            )
        if isinstance(raw_items, list):
            if any(
                isinstance(item, dict) and item.get("severity") not in {"BLOCKER", "HIGH", "MEDIUM", "LOW", "INFO"}
                for item in raw_items
            ):
                return _invalid(
                    "finding_precision_recall",
                    "ACTUAL_ITEM_INVALID",
                    "every scoped finding must carry a valid severity",
                )
            scoped_actuals = {
                **actuals,
                # This exclusion is part of the frozen metric version.  HIGH
                # UNKNOWN remains in scope; UNKNOWN is never promoted to pass.
                "finding_items": [
                    item
                    for item in raw_items
                    if isinstance(item, dict) and item.get("severity") in {"BLOCKER", "HIGH"}
                ],
            }
    return _score_exact_items(
        "finding_precision_recall",
        oracle,
        scoped_actuals,
        actual_field="finding_items",
        required_fields=("reason_code", "status", "subject", "affected_member"),
        matcher=_finding_matches,
        allow_empty_truth=metric_version == "exact-set-blocker-high-v1",
    )


def _score_repair(oracle: Mapping[str, Any], actuals: Mapping[str, Any]) -> dict[str, Any]:
    metric = "repair_postcheck"
    options, error = _validated_item_list(
        metric,
        actuals,
        "repair_options",
        ("operation_types", "predicates"),
    )
    if error:
        return error
    assert options is not None
    if not options:
        return _unscored(metric, "ACTUAL_DENOMINATOR_EMPTY", "no repair option is available for predicate scoring")
    max_options = oracle.get("max_options")
    allowed = oracle.get("allowed_operation_types")
    required = oracle.get("required_predicates")
    if (
        not isinstance(max_options, int)
        or max_options < 1
        or not isinstance(allowed, list)
        or not isinstance(required, list)
        or not required
    ):
        return _invalid(metric, "ORACLE_INVALID", "repair oracle is malformed")
    checks: list[dict[str, Any]] = [
        {
            "predicate": "option_count_within_max",
            "expected": True,
            "actual": len(options) <= max_options,
        }
    ]
    operations_valid = True
    for index, option in enumerate(options):
        operation_types = option["operation_types"]
        predicates = option["predicates"]
        if (
            not isinstance(operation_types, list)
            or not operation_types
            or any(not isinstance(value, str) for value in operation_types)
            or not isinstance(predicates, dict)
        ):
            return _invalid(metric, "ACTUAL_ITEM_INVALID", f"repair_options[{index}] is malformed")
        operations_valid = operations_valid and all(value in allowed for value in operation_types)
    checks.append({"predicate": "operations_allowed", "expected": True, "actual": operations_valid})
    for requirement in required:
        if not isinstance(requirement, dict) or not isinstance(requirement.get("predicate"), str):
            return _invalid(metric, "ORACLE_INVALID", "repair predicate truth is malformed")
        predicate = requirement["predicate"]
        expected = requirement.get("expected")
        values: list[bool] = []
        for index, option in enumerate(options):
            predicates = option["predicates"]
            if predicate not in predicates or not isinstance(predicates[predicate], bool):
                return _invalid(
                    metric,
                    "ACTUAL_PREDICATE_MISSING",
                    f"repair_options[{index}] lacks boolean predicate {predicate!r}",
                )
            values.append(predicates[predicate])
        checks.append({"predicate": predicate, "expected": expected, "actual": all(value == expected for value in values)})
    numerator = sum(check["actual"] is True for check in checks)
    denominator = len(checks)
    return {
        "metric": metric,
        "status": "SCORED",
        "metric_version": oracle.get("metric_version"),
        "value": numerator / denominator,
        "numerator": numerator,
        "denominator": denominator,
        "checks": checks,
    }


def _ranked_ids(metric: str, actuals: Mapping[str, Any]) -> tuple[list[str] | None, dict[str, Any] | None]:
    if "ranked_candidate_ids" not in actuals:
        return None, _unscored(metric, "ACTUAL_FIELD_MISSING", "ranked_candidate_ids is absent")
    ranked = actuals["ranked_candidate_ids"]
    if not isinstance(ranked, list) or any(not isinstance(value, str) or not value for value in ranked):
        return None, _invalid(metric, "ACTUAL_FIELD_INVALID", "ranked_candidate_ids must be a string list")
    if len(ranked) != len(set(ranked)):
        return None, _invalid(metric, "DUPLICATE_RANKED_CANDIDATE", "ranked candidate IDs must be unique")
    return ranked, None


def _score_ndcg(oracle: Mapping[str, Any], actuals: Mapping[str, Any]) -> dict[str, Any]:
    metric = "builder_ndcg_at_5"
    ranked, error = _ranked_ids(metric, actuals)
    if error:
        return error
    assert ranked is not None
    k = oracle.get("k")
    relevance_items = oracle.get("relevance_items")
    if not isinstance(k, int) or k < 1 or not isinstance(relevance_items, list) or not relevance_items:
        return _invalid(metric, "ORACLE_DENOMINATOR_EMPTY", "graded ranking oracle is empty or malformed")
    relevance: dict[str, float] = {}
    for item in relevance_items:
        if not isinstance(item, dict) or not isinstance(item.get("candidate_id"), str):
            return _invalid(metric, "ORACLE_INVALID", "graded relevance item is malformed")
        grade = item.get("relevance_grade")
        if isinstance(grade, bool) or not isinstance(grade, (int, float)) or not 0 <= grade <= 5:
            return _invalid(metric, "ORACLE_INVALID", "relevance grades must be in [0, 5]")
        if item["candidate_id"] in relevance:
            return _invalid(metric, "ORACLE_INVALID", "graded relevance candidate IDs must be unique")
        relevance[item["candidate_id"]] = float(grade)

    def dcg(grades: Sequence[float]) -> float:
        return sum((2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(grades[:k]))

    numerator = dcg([relevance.get(candidate_id, 0.0) for candidate_id in ranked])
    denominator = dcg(sorted(relevance.values(), reverse=True))
    if denominator <= 0:
        return _invalid(metric, "ORACLE_DENOMINATOR_EMPTY", "ideal DCG is zero")
    return {
        "metric": metric,
        "status": "SCORED",
        "metric_version": oracle.get("metric_version"),
        "value": numerator / denominator,
        "numerator": numerator,
        "denominator": denominator,
        "k": k,
    }


def _score_recall(oracle: Mapping[str, Any], actuals: Mapping[str, Any]) -> dict[str, Any]:
    metric = "builder_recall_at_5"
    ranked, error = _ranked_ids(metric, actuals)
    if error:
        return error
    assert ranked is not None
    k = oracle.get("k")
    relevant = oracle.get("relevant_candidate_ids")
    if not isinstance(k, int) or k < 1 or not isinstance(relevant, list) or not relevant:
        return _invalid(metric, "ORACLE_DENOMINATOR_EMPTY", "relevant candidate oracle is empty or malformed")
    if any(not isinstance(value, str) or not value for value in relevant) or len(relevant) != len(set(relevant)):
        return _invalid(metric, "ORACLE_INVALID", "relevant candidate IDs must be unique strings")
    hits = len(set(ranked[:k]) & set(relevant))
    return {
        "metric": metric,
        "status": "SCORED",
        "metric_version": oracle.get("metric_version"),
        "value": hits / len(relevant),
        "numerator": hits,
        "denominator": len(relevant),
        "k": k,
    }


_SCORERS = {
    "parse_f1": _score_parse,
    "entity_precision_recall": _score_entity,
    "finding_precision_recall": _score_findings,
    "repair_postcheck": _score_repair,
    "builder_ndcg_at_5": _score_ndcg,
    "builder_recall_at_5": _score_recall,
}


def score_metric_oracles(label: Mapping[str, Any], actuals: Mapping[str, Any]) -> dict[str, Any]:
    """Score one frozen label without reading any dataset or product service."""

    case_id = label.get("case_id")
    oracles = label.get("metric_oracles")
    if not isinstance(case_id, str) or not isinstance(oracles, dict):
        return {
            "schema_version": "dual-entry-metric-score-v1",
            "case_id": case_id,
            "status": "INVALID",
            "metrics": {},
            "errors": ["LABEL_OR_METRIC_ORACLES_INVALID"],
        }
    metrics: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for metric in METRIC_NAMES:
        oracle = oracles.get(metric)
        if not isinstance(oracle, dict):
            result = _invalid(metric, "ORACLE_MISSING", "metric oracle is absent")
        elif oracle.get("applicability") == "N_A":
            result = _na(metric, oracle)
        elif oracle.get("applicability") == "APPLICABLE":
            result = _SCORERS[metric](oracle, actuals)
        else:
            result = _invalid(metric, "ORACLE_APPLICABILITY_INVALID", "applicability must be APPLICABLE or N_A")
        metrics[metric] = result
        if result["status"] in {"INVALID", "UNSCORED"}:
            errors.append(f"{metric}:{result['reason_code']}")
    return {
        "schema_version": "dual-entry-metric-score-v1",
        "case_id": case_id,
        "status": "INVALID" if errors else "SCORED",
        "metrics": metrics,
        "errors": errors,
    }


def aggregate_metric_scores(case_scores: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    aggregates: dict[str, dict[str, Any]] = {}
    all_case_ids = [str(score.get("case_id")) for score in case_scores]
    for metric in METRIC_NAMES:
        applicable: list[str] = []
        scored_ids: list[str] = []
        invalid_ids: list[str] = []
        unscored_ids: list[str] = []
        na_ids: list[str] = []
        scored: list[Mapping[str, Any]] = []
        for case_score in case_scores:
            case_id = str(case_score.get("case_id"))
            result = case_score.get("metrics", {}).get(metric, {})
            status = result.get("status")
            if status == "N_A":
                na_ids.append(case_id)
                continue
            applicable.append(case_id)
            if status == "SCORED":
                scored_ids.append(case_id)
                scored.append(result)
            else:
                invalid_ids.append(case_id)
                if status == "UNSCORED":
                    unscored_ids.append(case_id)
        coverage_denominator = len(applicable)
        coverage_numerator = len(scored)
        aggregate: dict[str, Any] = {
            "metric": metric,
            "status": "SCORED" if coverage_denominator and not invalid_ids else "INVALID",
            "selected_case_ids": all_case_ids,
            "applicable_case_ids": applicable,
            "scored_case_ids": scored_ids,
            "invalid_case_ids": invalid_ids,
            "unscored_case_ids": unscored_ids,
            "n_a_case_ids": na_ids,
            "coverage": {
                "numerator": coverage_numerator,
                "denominator": coverage_denominator,
                "value": coverage_numerator / coverage_denominator if coverage_denominator else None,
            },
        }
        if metric in {"parse_f1", "entity_precision_recall", "finding_precision_recall"}:
            tp = sum(int(item["components"]["true_positive"]) for item in scored)
            fp = sum(int(item["components"]["false_positive"]) for item in scored)
            fn = sum(int(item["components"]["false_negative"]) for item in scored)
            aggregate["components"] = _f1_components(tp, fp, fn) if scored else None
            aggregate["value"] = aggregate["components"]["f1"]["value"] if scored else None
        elif metric == "repair_postcheck":
            numerator = sum(float(item["numerator"]) for item in scored)
            denominator = sum(float(item["denominator"]) for item in scored)
            aggregate.update(
                {
                    "numerator": numerator,
                    "denominator": denominator,
                    "value": numerator / denominator if denominator else None,
                    "case_mean": sum(float(item["value"]) for item in scored) / len(scored) if scored else None,
                }
            )
        else:
            raw_numerator = sum(float(item["numerator"]) for item in scored)
            raw_denominator = sum(float(item["denominator"]) for item in scored)
            numerator = sum(float(item["value"]) for item in scored)
            denominator = len(scored)
            aggregate.update(
                {
                    # Ranking metrics are query-level metrics, so the gate uses
                    # the macro mean. Raw gain/hit denominators remain visible.
                    "numerator": numerator,
                    "denominator": denominator,
                    "value": numerator / denominator if denominator else None,
                    "raw_components": {
                        "numerator": raw_numerator,
                        "denominator": raw_denominator,
                        "value": raw_numerator / raw_denominator if raw_denominator else None,
                    },
                }
            )
        aggregates[metric] = aggregate
    return {"schema_version": "dual-entry-metric-aggregate-v1", "metrics": aggregates}


def evaluate_metric_thresholds(
    aggregate: Mapping[str, Any], thresholds: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Evaluate configured metric thresholds and always require full oracle coverage."""

    gates: list[dict[str, Any]] = []
    metrics = aggregate.get("metrics", {})
    threshold_bindings = {
        "parse_f1": ("parse_f1", "value"),
        "entity_precision": ("entity_precision_recall", "precision"),
        "auto_match_precision": ("entity_precision_recall", "precision"),
        "entity_recall": ("entity_precision_recall", "recall"),
        "finding_precision": ("finding_precision_recall", "precision"),
        "blocker_high_precision": ("finding_precision_recall", "precision"),
        "finding_recall": ("finding_precision_recall", "recall"),
        "blocker_high_recall": ("finding_precision_recall", "recall"),
        "repair_postcheck": ("repair_postcheck", "value"),
        "builder_ndcg_at_5": ("builder_ndcg_at_5", "value"),
        "builder_recall_at_5": ("builder_recall_at_5", "value"),
    }
    for threshold_name, (metric_name, component) in threshold_bindings.items():
        if threshold_name not in thresholds:
            continue
        metric = metrics.get(metric_name, {})
        coverage = metric.get("coverage", {})
        denominator = coverage.get("denominator", 0)
        coverage_value = coverage.get("value")
        if component in {"precision", "recall"}:
            components = metric.get("components") or {}
            actual = (components.get(component) or {}).get("value")
        else:
            actual = metric.get(component)
        reasons: list[str] = []
        if not denominator:
            reasons.append("NO_APPLICABLE_DENOMINATOR")
        if coverage_value is None or coverage_value < 1.0:
            reasons.append("METRIC_COVERAGE_INCOMPLETE")
        if actual is None:
            reasons.append("METRIC_VALUE_UNAVAILABLE")
        elif actual < float(thresholds[threshold_name]):
            reasons.append("THRESHOLD_NOT_MET")
        gates.append(
            {
                "id": f"METRIC_THRESHOLD:{threshold_name}",
                "status": "PASS" if not reasons else "FAIL",
                "threshold": thresholds[threshold_name],
                "actual": actual,
                "coverage": coverage,
                "applicable_case_ids": metric.get("applicable_case_ids", []),
                "scored_case_ids": metric.get("scored_case_ids", []),
                "invalid_case_ids": metric.get("invalid_case_ids", []),
                "reason_codes": reasons,
            }
        )
    return gates


def import_metric_actuals(
    import_readback: Mapping[str, Any],
    *,
    audit_report: Mapping[str, Any] | None,
    repair_options: list[dict[str, Any]] | None,
    stop_names_by_id: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Project public HTTP readbacks into the scorer's stable, UUID-free units."""

    raw_stops = [item for item in import_readback.get("raw_stops", []) if isinstance(item, dict)]
    names = {item.get("raw_stop_id"): item.get("raw_name") for item in raw_stops}
    actuals: dict[str, Any] = {
        "parse_items": [{"stop_name": item.get("raw_name")} for item in raw_stops],
        "entity_items": [
            {
                "raw_name": names.get(item.get("raw_stop_id")),
                "status": item.get("resolution_status"),
                "canonical_place_id": item.get("canonical_place_id"),
            }
            for item in import_readback.get("resolutions", [])
            if isinstance(item, dict)
        ],
    }
    if audit_report is not None:
        actuals["finding_items"] = [
            finding_signature(item, stop_names_by_id=stop_names_by_id)
            for item in audit_report.get("findings", [])
            if isinstance(item, dict) and item.get("status") in {"VIOLATED", "UNKNOWN"}
        ]
    if repair_options is not None:
        actuals["repair_options"] = repair_options
    return actuals


def builder_metric_actuals(output: Mapping[str, Any]) -> dict[str, Any]:
    rounds = output.get("rounds")
    if not isinstance(rounds, list) or not rounds or not isinstance(rounds[0], dict):
        return {}
    candidates = rounds[0].get("suggestion_set", {}).get("candidates")
    if not isinstance(candidates, list):
        return {}
    return {
        "ranked_candidate_ids": [
            item.get("candidate_id") for item in candidates if isinstance(item, dict)
        ]
    }


def finding_signature(
    finding: Mapping[str, Any], *, stop_names_by_id: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Build the label's stable finding signature from a public AuditFinding."""

    values = finding.get("input_values") if isinstance(finding.get("input_values"), dict) else {}
    affected_stops = finding.get("affected_stop_ids") if isinstance(finding.get("affected_stop_ids"), list) else []
    affected_days = finding.get("affected_days") if isinstance(finding.get("affected_days"), list) else []
    affected_members = (
        finding.get("affected_member_ids") if isinstance(finding.get("affected_member_ids"), list) else []
    )
    stop_names_by_id = stop_names_by_id or {}
    subject = finding.get("subject") or values.get("subject")
    reason = finding.get("reason_code")
    if subject is None and reason == "DUPLICATE_PLACE" and values.get("place_id"):
        subject = values["place_id"]
    if subject is None and reason == "TIME_CHAIN_BROKEN" and isinstance(values.get("day_stops"), list):
        day_stops = [item for item in values["day_stops"] if isinstance(item, dict)]
        for left, right in zip(day_stops, day_stops[1:], strict=False):
            left_end = left.get("end_time")
            right_start = right.get("start_time")
            if isinstance(left_end, str) and isinstance(right_start, str) and right_start < left_end:
                left_id = str(left.get("stop_id"))
                right_id = str(right.get("stop_id"))
                subject = f"{stop_names_by_id.get(left_id, left_id)}->{stop_names_by_id.get(right_id, right_id)}"
                break
    if subject is None and len(affected_stops) == 2:
        left = stop_names_by_id.get(str(affected_stops[0]), str(affected_stops[0]))
        right = stop_names_by_id.get(str(affected_stops[1]), str(affected_stops[1]))
        subject = f"{left}->{right}"
    if subject is None and len(affected_stops) == 1:
        stop_id = str(affected_stops[0])
        subject = values.get("place_id") or stop_names_by_id.get(stop_id, stop_id)
    if subject is None and affected_days:
        subject = f"day:{affected_days[0]}"
    if subject is None and reason == "ACCESSIBILITY_EVIDENCE_MISSING":
        subject = f"member:{affected_members[0]}" if affected_members else "member:unknown"
    if subject is None and reason == "DIETARY_EVIDENCE_MISSING":
        subject = "restaurant:unknown"
    affected_member = finding.get("affected_member")
    if affected_member is None and affected_members:
        affected_member = str(affected_members[0])
    return {
        "reason_code": reason,
        "status": finding.get("status"),
        "severity": finding.get("severity"),
        "subject": subject,
        "affected_member": affected_member,
    }
