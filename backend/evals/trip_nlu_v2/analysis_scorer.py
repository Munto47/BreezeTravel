from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.trip_intake.models import (
    QuantityQuantifier,
    TripIntakeExtraction,
    validate_extraction_evidence,
)
from evals.trip_nlu_v2.scorer import (
    _contract_control_atoms,
    _duration_atoms,
    _location_atoms,
    _location_roles,
    _metric,
    _party_atoms,
    _preference_atoms,
    _quantities,
)
from evals.trip_nlu_v2.validator import DatasetValidationError, _read_jsonl


CRITICAL_ERROR_NAMES = (
    "hallucination",
    "origin_destination_reversal",
    "negation_reversal",
    "old_plan_reversal",
    "numeric_cross_class",
    "unknown_promoted_to_exact",
)


def _bounds(value: Any) -> set[int]:
    return {bound for bound in (value.min, value.max) if bound is not None}


def _critical_errors(
    predicted: TripIntakeExtraction,
    expected: TripIntakeExtraction,
    input_text: str,
) -> dict[str, int]:
    critical = {name: 0 for name in CRITICAL_ERROR_NAMES}
    expected_roles = _location_roles(expected)
    predicted_roles = _location_roles(predicted)
    for identity in predicted_roles.keys() - expected_roles.keys():
        critical["hallucination"] += 1
    for identity in expected_roles.keys() & predicted_roles.keys():
        wanted = expected_roles[identity]
        actual = predicted_roles[identity]
        if (
            (wanted & {"ORIGIN", "RETURN_LOCATION"} and "PRIMARY_DESTINATION" in actual)
            or ("PRIMARY_DESTINATION" in wanted and actual & {"ORIGIN", "RETURN_LOCATION"})
        ):
            critical["origin_destination_reversal"] += 1
        if "EXCLUDED" in wanted and any(role != "EXCLUDED" for role in actual):
            critical["negation_reversal"] += 1
        if "OTHER_MENTION" in wanted and actual & {
            "PRIMARY_DESTINATION",
            "DESTINATION_CANDIDATE",
            "REQUESTED_PLACE",
        }:
            critical["old_plan_reversal"] += 1
    expected_polarity = {
        (item.category, item.label): item.polarity.value
        for item in expected.preferences.items
    }
    for item in predicted.preferences.items:
        key = (item.category, item.label)
        if key in expected_polarity and {
            expected_polarity[key],
            item.polarity.value,
        } == {"LIKE", "DISLIKE"}:
            critical["negation_reversal"] += 1

    input_numbers = {int(value) for value in re.findall(r"\d+", input_text)}
    expected_quantities = _quantities(expected)
    predicted_quantities = _quantities(predicted)
    for path in expected_quantities.keys() | predicted_quantities.keys():
        expected_quantity = expected_quantities.get(path)
        predicted_quantity = predicted_quantities.get(path)
        if expected_quantity is None and predicted_quantity is not None:
            critical["hallucination"] += 1
            continue
        if predicted_quantity is None:
            continue
        if (
            expected_quantity.quantifier == QuantityQuantifier.UNKNOWN
            and predicted_quantity.quantifier != QuantityQuantifier.UNKNOWN
        ):
            critical["unknown_promoted_to_exact"] += 1
        extra_bounds = _bounds(predicted_quantity) - _bounds(expected_quantity)
        if extra_bounds:
            if extra_bounds & input_numbers:
                critical["numeric_cross_class"] += 1
            else:
                critical["hallucination"] += 1

    expected_preference_atoms = _preference_atoms(expected)
    predicted_preference_atoms = _preference_atoms(predicted)
    if any(atom[0] == "item" for atom in predicted_preference_atoms - expected_preference_atoms):
        critical["hallucination"] += 1
    if set(predicted.party_size.composition.tags) - set(expected.party_size.composition.tags):
        critical["hallucination"] += 1
    return critical


def _case_metric(
    predicted: TripIntakeExtraction,
    expected: TripIntakeExtraction,
) -> dict[str, float]:
    pairs = [(predicted, expected)]
    return {
        "locations": float(_metric(pairs, _location_atoms)["micro_f1"]),
        "party_size": float(_metric(pairs, _party_atoms)["micro_f1"]),
        "duration": float(_metric(pairs, _duration_atoms)["micro_f1"]),
        "preferences_requirements": float(
            _metric(pairs, _preference_atoms)["micro_f1"]
        ),
        "contract_controls": float(_metric(pairs, _contract_control_atoms)["micro_f1"]),
    }


def score_nonblind(
    predictions_path: Path,
    labels_path: Path,
    *,
    include_case_details: bool = True,
) -> dict[str, Any]:
    predictions = _read_jsonl(predictions_path)
    labels = _read_jsonl(labels_path)
    if not labels:
        raise DatasetValidationError("non-blind scoring requires at least one labelled case")
    if any(int(item["case_id"].rsplit("_", 1)[1]) > 96 for item in labels):
        raise DatasetValidationError("analysis scorer refuses frozen blind case IDs")
    prediction_by_id = {item["case_id"]: item for item in predictions}
    label_ids = [item["case_id"] for item in labels]
    if len(prediction_by_id) != len(predictions) or set(prediction_by_id) != set(label_ids):
        raise DatasetValidationError("prediction coverage must exactly match labelled cases")

    pairs: list[tuple[TripIntakeExtraction, TripIntakeExtraction]] = []
    critical = {name: 0 for name in CRITICAL_ERROR_NAMES}
    case_details: list[dict[str, Any]] = []
    for label in labels:
        predicted = TripIntakeExtraction.model_validate(
            prediction_by_id[label["case_id"]]["prediction"]
        )
        expected = TripIntakeExtraction.model_validate(label["expected"])
        validate_extraction_evidence(predicted, {label["source_id"]: label["input_text"]})
        pairs.append((predicted, expected))
        case_critical = _critical_errors(predicted, expected, label["input_text"])
        for name, value in case_critical.items():
            critical[name] += value
        if include_case_details:
            case_details.append(
                {
                    "case_id": label["case_id"],
                    "difficulty": label["annotation"]["difficulty"],
                    "metrics": _case_metric(predicted, expected),
                    "critical_errors": case_critical,
                }
            )

    metrics = {
        "locations": _metric(pairs, _location_atoms),
        "party_size": _metric(pairs, _party_atoms),
        "duration": _metric(pairs, _duration_atoms),
        "preferences_requirements": _metric(pairs, _preference_atoms),
        "contract_controls": _metric(pairs, _contract_control_atoms),
    }
    hard_pairs = [
        pair
        for pair, label in zip(pairs, labels, strict=True)
        if label["annotation"]["difficulty"] == "hard"
    ]
    hard_metric = _metric(
        hard_pairs,
        lambda value: _location_atoms(value) | _party_atoms(value) | _duration_atoms(value),
    )
    passed = (
        all(value == 0 for value in critical.values())
        and metrics["locations"]["micro_f1"] >= 0.95
        and metrics["party_size"]["micro_f1"] >= 0.95
        and metrics["duration"]["micro_f1"] >= 0.95
        and metrics["preferences_requirements"]["micro_f1"] >= 0.90
        and metrics["contract_controls"]["micro_f1"] == 1.0
        and hard_metric["micro_f1"] >= 0.90
    )
    result = {
        "schema_version": "trip-nlu-v2-analysis-receipt-v1",
        "case_count": len(labels),
        "output_coverage": 1.0,
        "schema_validity": 1.0,
        "critical_errors": critical,
        "metrics": metrics,
        "hard_key_fields": hard_metric,
        "gate": "PASS" if passed else "REJECT",
        "case_details_present": include_case_details,
        "predictions_sha256": hashlib.sha256(predictions_path.read_bytes()).hexdigest(),
        "labels_sha256": hashlib.sha256(labels_path.read_bytes()).hexdigest(),
    }
    if include_case_details:
        result["case_details"] = case_details
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("predictions", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--aggregate-only", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            score_nonblind(
                args.predictions,
                args.labels,
                include_case_details=not args.aggregate_only,
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
