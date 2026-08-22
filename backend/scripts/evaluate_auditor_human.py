"""Compute M1 gates from real-organizer labels without invoking any model Judge."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = BACKEND / "eval_data" / "auditor" / "manifest.json"
SCHEMA_ROOT = BACKEND / "eval_data" / "auditor"
MANIFEST_SCHEMA = SCHEMA_ROOT / "manifest.schema.json"
CASE_SCHEMA = SCHEMA_ROOT / "case.schema.json"
PREDICTION_SCHEMA = SCHEMA_ROOT / "prediction.schema.json"
MIN_REAL_ITINERARIES = 30
MIN_REAL_ORGANIZERS = 15
MAX_REAL_ORGANIZERS = 20
CRITICAL = {"BLOCKER", "HIGH"}
FIELDS = ("date", "time", "place", "fixed_commitment")
HUMAN_ONLY_BOUNDARY = (
    "Only a real organizer may add source text and human findings. Synthetic, model-generated, "
    "Judge, or agent labels must not increment human counts."
)
_ORGANIZER_HASH = re.compile(r"^[0-9a-f]{64}$")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_schema(value: dict[str, Any], schema_path: Path, label: str) -> None:
    schema = _load(schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
    raise ValueError(f"{label} schema validation failed at {location}: {error.message}")


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _f1(tp: int, fp: int, fn: int) -> float | None:
    denominator = 2 * tp + fp + fn
    return 2 * tp / denominator if denominator else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _blocked(manifest: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "status": "BLOCKED_HUMAN_DATA",
        "reason": reason,
        "human_labeled_itineraries": int(manifest.get("human_labeled_itineraries", 0)),
        "real_organizers": int(manifest.get("real_organizers", 0)),
        "gates_passed": False,
    }


def _inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"dataset path escapes manifest directory: {relative}") from exc
    return candidate


def evaluate(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = _load(manifest_path)
    if manifest.get("evidence_boundary") != HUMAN_ONLY_BOUNDARY:
        return _blocked(manifest, "manifest lacks the exact human-only evidence boundary")
    _validate_schema(manifest, MANIFEST_SCHEMA, "manifest")
    entries = list(manifest.get("cases") or [])
    if len(entries) < MIN_REAL_ITINERARIES:
        return _blocked(manifest, "fewer than 30 real itineraries have human labels")

    root = manifest_path.resolve().parent
    loaded_entries: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    case_ids: set[str] = set()
    source_organizers: dict[str, str] = {}
    organizer_hashes: set[str] = set()
    for entry in entries:
        case_path = _inside(root, str(entry["case_path"]))
        prediction_path = _inside(root, str(entry["prediction_path"]))
        case = _load(case_path)
        prediction = _load(prediction_path)
        _validate_schema(case, CASE_SCHEMA, f"case {case_path}")
        _validate_schema(prediction, PREDICTION_SCHEMA, f"prediction {prediction_path}")
        case_id = str(case["case_id"])
        if case_id in case_ids:
            raise ValueError(f"duplicate case_id: {case_id}")
        case_ids.add(case_id)
        if prediction.get("case_id") != case_id or entry.get("case_id") != case_id:
            raise ValueError(f"case identity mismatch for {case_path}")
        if case.get("source_kind") not in {"REAL_AI_ITINERARY", "REAL_MANUAL_ITINERARY"}:
            raise ValueError(f"case {case_id} is not a real itinerary source")
        if not case.get("consent_recorded"):
            raise ValueError(f"case {case_id} has no consent record")
        organizer_hash = str(case.get("organizer_id_hash") or "")
        if not _ORGANIZER_HASH.fullmatch(organizer_hash):
            raise ValueError(f"case {case_id} lacks a study-scoped SHA-256 organizer hash")
        source_document_id = str(case.get("source_document_id") or "")
        if not source_document_id:
            raise ValueError(f"case {case_id} lacks source_document_id")
        existing_organizer = source_organizers.setdefault(source_document_id, organizer_hash)
        if existing_organizer != organizer_hash:
            raise ValueError(f"source document {source_document_id} is assigned to multiple organizers")
        organizer_hashes.add(organizer_hash)
        finding_ids = [str(item["finding_id"]) for item in case.get("human_findings", [])]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError(f"case {case_id} has duplicate human finding IDs")
        prediction_ids = [str(item["prediction_id"]) for item in prediction.get("findings", [])]
        if len(prediction_ids) != len(set(prediction_ids)):
            raise ValueError(f"case {case_id} has duplicate prediction IDs")
        loaded_entries.append((entry, case, prediction))

    derived_itineraries = len(source_organizers)
    derived_organizers = len(organizer_hashes)
    manifest_for_result = {
        **manifest,
        "human_labeled_itineraries": derived_itineraries,
        "real_organizers": derived_organizers,
    }
    if derived_itineraries < MIN_REAL_ITINERARIES:
        return _blocked(manifest_for_result, "fewer than 30 unique real source documents have human labels")
    if derived_organizers < MIN_REAL_ORGANIZERS:
        return _blocked(manifest_for_result, "fewer than 15 unique real organizers are represented")
    if derived_organizers > MAX_REAL_ORGANIZERS:
        return _blocked(manifest_for_result, "more than 20 unique real organizers are represented")
    if int(manifest.get("human_labeled_itineraries", -1)) != derived_itineraries:
        return _blocked(manifest_for_result, "manifest itinerary count does not match unique source documents")
    if int(manifest.get("real_organizers", -1)) != derived_organizers:
        return _blocked(manifest_for_result, "manifest organizer count does not match unique organizer hashes")

    field_counts = {field: Counter(tp=0, fp=0, fn=0) for field in FIELDS}
    accepted_matches = correct_matches = 0
    fixed_expected = fixed_recalled = silent_mismatches = 0
    predicted_critical = correct_critical = 0
    human_critical_ids: set[tuple[str, str]] = set()
    matched_human_critical_ids: set[tuple[str, str]] = set()
    human_critical_ids_by_origin: dict[str, set[tuple[str, str]]] = {
        "original": set(),
        "controlled_injected": set(),
    }
    matched_human_critical_ids_by_origin: dict[str, set[tuple[str, str]]] = {
        "original": set(),
        "controlled_injected": set(),
    }
    evidence_readable = evidence_total = 0
    human_check_available = True
    human_check_checked = human_check_correct = 0
    human_check_unavailable_reasons: Counter[str] = Counter()
    durations: list[float] = []
    repairs_offered = repairs_accepted = 0
    rejection_reasons: Counter[str] = Counter()

    for _entry, case, prediction in loaded_entries:
        case_id = str(case["case_id"])
        human_findings = {str(item["finding_id"]): item for item in case.get("human_findings", [])}
        for finding_id, finding in human_findings.items():
            if finding.get("severity") in CRITICAL:
                identity = (case_id, finding_id)
                human_critical_ids.add(identity)
                origin = "original" if finding["is_original_error"] else "controlled_injected"
                human_critical_ids_by_origin[origin].add(identity)

        for field in FIELDS:
            counts = prediction["extraction_counts"][field]
            field_counts[field].update({key: int(counts[key]) for key in ("tp", "fp", "fn")})
        auto = prediction["auto_matches"]
        accepted_matches += int(auto["accepted"])
        correct_matches += int(auto["correct"])
        if int(auto["correct"]) > int(auto["accepted"]):
            raise ValueError(f"case {case_id} has more correct matches than accepted matches")
        fixed = prediction["fixed_commitments"]
        fixed_expected += int(fixed["expected"])
        fixed_recalled += int(fixed["recalled"])
        silent_mismatches += int(prediction["silent_mismatches"])
        durations.append(float(prediction["audit_duration_seconds"]))

        for finding in prediction.get("findings", []):
            if finding.get("severity") not in CRITICAL:
                continue
            predicted_critical += 1
            matched_ids = [str(value) for value in finding.get("matched_human_finding_ids", [])]
            unknown_ids = sorted(set(matched_ids) - set(human_findings))
            if unknown_ids:
                raise ValueError(
                    f"case {case_id} prediction references unknown human finding IDs: {unknown_ids}"
                )
            if finding.get("human_verdict") == "CORRECT":
                if not matched_ids:
                    raise ValueError(
                        f"case {case_id} marks a critical prediction CORRECT without a human finding match"
                    )
                correct_critical += 1
            evidence_total += 1
            evidence_readable += int(bool(finding.get("evidence_readable")))
            for finding_id in matched_ids:
                human = human_findings.get(str(finding_id))
                if human and human.get("severity") in CRITICAL:
                    identity = (case_id, str(finding_id))
                    matched_human_critical_ids.add(identity)
                    origin = "original" if human["is_original_error"] else "controlled_injected"
                    matched_human_critical_ids_by_origin[origin].add(identity)

        human_check = prediction["critical_human_check"]
        if human_check["status"] == "UNAVAILABLE":
            human_check_available = False
            human_check_unavailable_reasons[str(human_check["reason"])] += 1
        else:
            checked = int(human_check["checked"])
            correct = int(human_check["correct"])
            if correct > checked:
                raise ValueError(f"case {case_id} has more correct human checks than checked findings")
            human_check_checked += checked
            human_check_correct += correct

        repair = prediction["repair"]
        if repair["offered"]:
            repairs_offered += 1
            if repair["accepted"]:
                repairs_accepted += 1
            else:
                reason = str(repair.get("rejection_reason") or "UNSPECIFIED")
                rejection_reasons[reason] += 1
        elif repair["accepted"]:
            raise ValueError(f"case {case_id} accepts a repair that was not offered")

    field_f1 = {
        field: _f1(counts["tp"], counts["fp"], counts["fn"])
        for field, counts in field_counts.items()
    }
    metrics = {
        "field_f1": field_f1,
        "auto_match_precision": _ratio(correct_matches, accepted_matches),
        "fixed_commitment_recall": _ratio(fixed_recalled, fixed_expected),
        "silent_mismatches": silent_mismatches,
        "critical_precision": _ratio(correct_critical, predicted_critical),
        "critical_recall": _ratio(len(matched_human_critical_ids), len(human_critical_ids)),
        "critical_recall_by_origin": {
            origin: _ratio(
                len(matched_human_critical_ids_by_origin[origin]),
                len(human_critical_ids_by_origin[origin]),
            )
            for origin in human_critical_ids_by_origin
        },
        "critical_counts_by_origin": {
            origin: {
                "human_expected": len(human_critical_ids_by_origin[origin]),
                "matched": len(matched_human_critical_ids_by_origin[origin]),
            }
            for origin in human_critical_ids_by_origin
        },
        "critical_human_check_status": "AVAILABLE" if human_check_available else "UNAVAILABLE",
        "critical_human_check_accuracy": (
            _ratio(human_check_correct, human_check_checked) if human_check_available else None
        ),
        "critical_human_check_unavailable_reasons": dict(
            sorted(human_check_unavailable_reasons.items())
        ),
        "critical_evidence_readback_rate": _ratio(evidence_readable, evidence_total),
        "audit_duration_p80_seconds": _percentile(durations, 0.8),
        "repair_adoption_rate": _ratio(repairs_accepted, repairs_offered),
        "repair_rejection_reasons": dict(sorted(rejection_reasons.items())),
        "sample_size": len(entries),
        "real_organizers": derived_organizers,
    }
    gates = {
        "field_f1_at_least_0_90": all(value is not None and value >= 0.90 for value in field_f1.values()),
        "auto_match_precision_at_least_0_95": metrics["auto_match_precision"] is not None and metrics["auto_match_precision"] >= 0.95,
        "fixed_commitment_recall_at_least_0_95": metrics["fixed_commitment_recall"] is not None and metrics["fixed_commitment_recall"] >= 0.95,
        "silent_mismatches_zero": silent_mismatches == 0,
        "critical_precision_at_least_0_90": metrics["critical_precision"] is not None and metrics["critical_precision"] >= 0.90,
        "critical_recall_at_least_0_85": metrics["critical_recall"] is not None and metrics["critical_recall"] >= 0.85,
        "critical_human_check_accuracy_at_least_0_85": metrics["critical_human_check_accuracy"] is not None and metrics["critical_human_check_accuracy"] >= 0.85,
        "critical_evidence_readback_100_percent": metrics["critical_evidence_readback_rate"] == 1.0,
        "audit_duration_p80_at_most_180_seconds": metrics["audit_duration_p80_seconds"] is not None and metrics["audit_duration_p80_seconds"] <= 180,
    }
    return {
        "status": "M1_PASSED" if all(gates.values()) else "M1_FAILED",
        "gates_passed": all(gates.values()),
        "gates": gates,
        "metrics": metrics,
        "evidence_boundary": "human labels only; no LLM-as-Judge",
        "derived_unique_source_documents": derived_itineraries,
        "derived_unique_organizers": derived_organizers,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.manifest)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
