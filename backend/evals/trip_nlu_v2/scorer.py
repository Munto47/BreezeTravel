from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from app.trip_intake.models import QuantityQuantifier, TripIntakeExtraction
from evals.trip_nlu_v2.validator import DatasetValidationError, _read_jsonl


def _f1(predicted: set[tuple[Any, ...]], expected: set[tuple[Any, ...]]) -> tuple[int, int, int]:
    return len(predicted & expected), len(predicted - expected), len(expected - predicted)


def _evidence_atoms(prefix: str, evidence: list[Any]) -> set[tuple[Any, ...]]:
    return {
        (prefix, "evidence", item.source_id, item.start, item.end, item.quote)
        for item in evidence
    }


def _location_atoms(value: TripIntakeExtraction) -> set[tuple[Any, ...]]:
    atoms = {("status", value.locations.status.value)}
    atoms.update(
        (
            "mention",
            item.role.value,
            item.entity_type.value,
            item.normalized_name or item.raw_text,
        )
        for item in value.locations.mentions
    )
    for item in value.locations.mentions:
        identity = item.normalized_name or item.raw_text
        atoms.update(
            _evidence_atoms(
                f"mention:{item.role.value}:{item.entity_type.value}:{identity}",
                item.evidence,
            )
        )
    return atoms


def _location_roles(value: TripIntakeExtraction) -> dict[str, set[str]]:
    roles: dict[str, set[str]] = {}
    for item in value.locations.mentions:
        identity = item.normalized_name or item.raw_text
        roles.setdefault(identity, set()).add(item.role.value)
    return roles


def _has_origin_destination_reversal(wanted: set[str], actual: set[str]) -> bool:
    extra_roles = actual - wanted
    return bool(
        (wanted & {"ORIGIN", "RETURN_LOCATION"} and "PRIMARY_DESTINATION" in extra_roles)
        or ("PRIMARY_DESTINATION" in wanted and extra_roles & {"ORIGIN", "RETURN_LOCATION"})
    )


def _bounds(value: Any) -> set[int]:
    return {bound for bound in (value.min, value.max) if bound is not None}


def _quantities(value: TripIntakeExtraction) -> dict[str, Any]:
    quantities = {
        "party.total": value.party_size.total,
        "temporal.days": value.temporal.days,
        "temporal.nights": value.temporal.nights,
    }
    for name in ("adults", "children", "elderly"):
        quantity = getattr(value.party_size.composition, name)
        if quantity is not None:
            quantities[f"party.{name}"] = quantity
    return quantities


def _quantity_atoms(prefix: str, value: Any) -> set[tuple[Any, ...]]:
    atoms = {
        (prefix, "quantifier", value.quantifier.value),
        (prefix, "min", value.min),
        (prefix, "max", value.max),
        (prefix, "derivation", value.derivation.value),
    }
    atoms.update(_evidence_atoms(prefix, value.evidence))
    return atoms


def _party_atoms(value: TripIntakeExtraction) -> set[tuple[Any, ...]]:
    atoms = _quantity_atoms("total", value.party_size.total)
    atoms.update(("tag", item) for item in value.party_size.composition.tags)
    for name in ("adults", "children", "elderly"):
        quantity = getattr(value.party_size.composition, name)
        if quantity is not None:
            atoms.update(_quantity_atoms(name, quantity))
    return atoms


def _temporal_control_atoms(value: TripIntakeExtraction) -> set[tuple[Any, ...]]:
    atoms: set[tuple[Any, ...]] = set()
    date_range = value.temporal.date_range
    if date_range is not None:
        atoms.add(
            (
                "date_range",
                date_range.raw_text,
                date_range.start.year,
                date_range.start.month,
                date_range.start.day,
                date_range.end.year,
                date_range.end.month,
                date_range.end.day,
                date_range.inclusive,
            )
        )
        atoms.update(_evidence_atoms("date_range", date_range.evidence))
    for name in ("arrival", "departure"):
        commitment = getattr(value.temporal, name)
        if commitment is not None:
            atoms.add((name, commitment.location_text, commitment.at_text))
            atoms.update(_evidence_atoms(name, commitment.evidence))
    return atoms


def _duration_atoms(value: TripIntakeExtraction) -> set[tuple[Any, ...]]:
    return (
        _quantity_atoms("days", value.temporal.days)
        | _quantity_atoms("nights", value.temporal.nights)
        | _temporal_control_atoms(value)
    )


def _contract_control_atoms(value: TripIntakeExtraction) -> set[tuple[Any, ...]]:
    atoms = {("readiness", value.readiness.value), *_temporal_control_atoms(value)}
    atoms.update(
        ("issue", item.code, item.field_path, item.blocking)
        for item in value.issues
    )
    for item in value.issues:
        atoms.update(_evidence_atoms(f"issue:{item.code}:{item.field_path}", item.evidence))
    return atoms


def _preference_atoms(value: TripIntakeExtraction) -> set[tuple[Any, ...]]:
    atoms = {("status", value.preferences.status.value), ("pace", value.preferences.pace.value.value)}
    atoms.update(
        (
            "item",
            item.polarity.value,
            item.category,
            item.operator.value if item.operator else None,
            json.dumps(item.value, ensure_ascii=False, sort_keys=True),
            item.unit,
            item.currency,
            item.applies_to,
            item.label,
        )
        for item in value.preferences.items
    )
    for item in value.preferences.items:
        atoms.update(_evidence_atoms(f"preference:{item.item_id}", item.evidence))
    atoms.update(_evidence_atoms("pace", value.preferences.pace.evidence))
    atoms.update(_evidence_atoms("no_preference", value.preferences.no_preference_evidence))
    return atoms


def _metric(
    pairs: list[tuple[TripIntakeExtraction, TripIntakeExtraction]],
    atomizer: Callable[[TripIntakeExtraction], set[tuple[Any, ...]]],
) -> dict[str, float | int]:
    tp = fp = fn = 0
    for predicted, expected in pairs:
        current = _f1(atomizer(predicted), atomizer(expected))
        tp += current[0]
        fp += current[1]
        fn += current[2]
    denominator = 2 * tp + fp + fn
    return {"tp": tp, "fp": fp, "fn": fn, "micro_f1": 1.0 if denominator == 0 else 2 * tp / denominator}


def score_predictions(predictions_path: Path, labels_path: Path) -> dict[str, Any]:
    predictions = _read_jsonl(predictions_path)
    labels = _read_jsonl(labels_path)
    if len(predictions) != 24 or len(labels) != 24:
        raise DatasetValidationError("blind scoring requires complete 24-case files")
    prediction_by_id = {item["case_id"]: item for item in predictions}
    if len(prediction_by_id) != 24 or set(prediction_by_id) != {item["case_id"] for item in labels}:
        raise DatasetValidationError("prediction coverage must be exactly 24/24")
    pairs = [
        (
            TripIntakeExtraction.model_validate(prediction_by_id[label["case_id"]]["prediction"]),
            TripIntakeExtraction.model_validate(label["expected"]),
        )
        for label in labels
    ]
    critical = {
        "hallucination": 0,
        "origin_destination_reversal": 0,
        "negation_reversal": 0,
        "old_plan_reversal": 0,
        "numeric_cross_class": 0,
        "unknown_promoted_to_exact": 0,
    }
    for (predicted, expected), label in zip(pairs, labels, strict=True):
        expected_roles = _location_roles(expected)
        predicted_roles = _location_roles(predicted)
        for identity in predicted_roles.keys() - expected_roles.keys():
            critical["hallucination"] += 1
        for identity in expected_roles.keys() & predicted_roles.keys():
            wanted = expected_roles[identity]
            actual = predicted_roles[identity]
            if _has_origin_destination_reversal(wanted, actual):
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
        input_numbers = {int(value) for value in re.findall(r"\d+", label["input_text"])}
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
            expected_bounds = _bounds(expected_quantity)
            predicted_bounds = _bounds(predicted_quantity)
            extra_bounds = predicted_bounds - expected_bounds
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
    metrics = {
        "locations": _metric(pairs, _location_atoms),
        "party_size": _metric(pairs, _party_atoms),
        "duration": _metric(pairs, _duration_atoms),
        "preferences_requirements": _metric(pairs, _preference_atoms),
        "contract_controls": _metric(pairs, _contract_control_atoms),
    }
    hard_pairs = [
        pair for pair, label in zip(pairs, labels, strict=True) if label["annotation"]["difficulty"] == "hard"
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
    return {
        "schema_version": "trip-nlu-v2-gate-receipt-v1",
        "case_count": 24,
        "output_coverage": 1.0,
        "schema_validity": 1.0,
        "critical_errors": critical,
        "metrics": metrics,
        "hard_key_fields": hard_metric,
        "gate": "PASS" if passed else "REJECT",
        "case_details_present": False,
        "predictions_sha256": hashlib.sha256(predictions_path.read_bytes()).hexdigest(),
        "labels_sha256": hashlib.sha256(labels_path.read_bytes()).hexdigest(),
        "case_ids_sha256": hashlib.sha256(
            "\n".join(label["case_id"] for label in labels).encode()
        ).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("predictions", type=Path)
    parser.add_argument("external_blind_labels", type=Path)
    args = parser.parse_args()
    print(json.dumps(score_predictions(args.predictions, args.external_blind_labels), sort_keys=True))


if __name__ == "__main__":
    main()
