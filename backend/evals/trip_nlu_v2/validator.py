from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from app.trip_intake.models import TripIntakeExtraction, validate_extraction_evidence


VALIDATOR_VERSION = "trip-nlu-v2-validator-v1"
FORBIDDEN_BLIND_KEYS = {
    "expected",
    "oracle",
    "labels",
    "answer",
    "ground_truth",
    "metric_oracles",
    "coverage",
}
EXPECTED_TOTAL = {
    "count": 120,
    "difficulty": {"easy": 30, "medium": 54, "hard": 36},
    "destination": {"北京": 30, "上海": 30, "杭州": 30, "other": 12, "multiple": 6, "uncertain": 6, "missing": 6},
    "party": {"EXACT": 60, "RANGE": 18, "APPROXIMATE": 8, "AT_LEAST": 8, "AT_MOST": 8, "UNKNOWN": 18},
    "duration": {"EXACT": 54, "RANGE": 24, "APPROXIMATE": 8, "AT_LEAST": 8, "AT_MOST": 8, "UNKNOWN": 18},
    "generator_source": {"DETERMINISTIC": 60, "USER_PROMPT": 60},
    "minimums": {"preference": 96, "roles": 24, "interference": 30, "fictional": 12, "semantic_party": 24},
}
EXPECTED_REQUIREMENT_CATEGORIES = {
    "accessibility",
    "accommodation",
    "budget",
    "children",
    "dietary",
    "elderly",
    "pet",
    "physical",
    "time",
    "transport",
}


class DatasetValidationError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DatasetValidationError(f"expected JSON object: {path.name}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise DatasetValidationError(f"{path.name}:{line_number} must be an object")
        values.append(value)
    return values


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_ids_hash(case_ids: list[str]) -> str:
    return hashlib.sha256("\n".join(case_ids).encode()).hexdigest()


def _current_code_bindings(backend_root: Path) -> dict[str, str]:
    return {
        "schema_sha256": hashlib.sha256(
            json.dumps(
                TripIntakeExtraction.model_json_schema(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "validator_sha256": _sha256(Path(__file__)),
        "scorer_sha256": _sha256(backend_root / "evals" / "trip_nlu_v2" / "scorer.py"),
        "gate_sha256": _sha256(backend_root / "evals" / "trip_nlu_v2" / "gate.py"),
        "generator_sha256": _sha256(backend_root / "scripts" / "generate_trip_nlu_v2.py"),
    }


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        if any(str(key).casefold() in FORBIDDEN_BLIND_KEYS for key in value):
            return True
        return any(_contains_forbidden_key(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden_key(child) for child in value)
    return False


def _validate_labelled(case: dict[str, Any]) -> None:
    extraction = TripIntakeExtraction.model_validate(case["expected"])
    source_id = case["source_id"]
    validate_extraction_evidence(extraction, {source_id: case["input_text"]})
    coverage = case["annotation"]["coverage"]
    if extraction.party_size.total.quantifier.value != coverage["party"]:
        raise DatasetValidationError(f"{case['case_id']} party coverage disagrees with truth")
    if extraction.temporal.days.quantifier.value != coverage["duration"]:
        raise DatasetValidationError(f"{case['case_id']} duration coverage disagrees with truth")
    if coverage["preference"] != (extraction.preferences.status.value != "UNSPECIFIED"):
        raise DatasetValidationError(f"{case['case_id']} preference coverage disagrees with truth")
    if coverage["semantic_party"] != (
        extraction.party_size.total.derivation.value == "SEMANTIC_INFERENCE"
    ):
        raise DatasetValidationError(f"{case['case_id']} semantic party coverage disagrees with truth")
    if not extraction.party_size.total.evidence:
        raise DatasetValidationError(f"{case['case_id']} party total lacks explicit evidence")
    if not extraction.temporal.days.evidence:
        raise DatasetValidationError(f"{case['case_id']} duration lacks explicit evidence")
    if any(not mention.evidence for mention in extraction.locations.mentions):
        raise DatasetValidationError(f"{case['case_id']} location mention lacks evidence")
    if extraction.preferences.status.value == "SPECIFIED":
        if not extraction.preferences.items or any(not item.evidence for item in extraction.preferences.items):
            raise DatasetValidationError(f"{case['case_id']} preference item lacks evidence")
        if not extraction.preferences.pace.evidence:
            raise DatasetValidationError(f"{case['case_id']} pace lacks evidence")
    if extraction.preferences.status.value == "NO_PREFERENCE" and not extraction.preferences.no_preference_evidence:
        raise DatasetValidationError(f"{case['case_id']} no-preference lacks evidence")
    if any(not issue.evidence for issue in extraction.issues):
        raise DatasetValidationError(f"{case['case_id']} issue lacks evidence")
    mentions_by_raw: dict[str, list[Any]] = {}
    for mention in extraction.locations.mentions:
        mentions_by_raw.setdefault(mention.raw_text, []).append(mention)
    for raw_text, mentions in mentions_by_raw.items():
        if len(mentions) > 1 and case["input_text"].count(raw_text) >= len(mentions):
            spans = {(item.evidence[0].start, item.evidence[0].end) for item in mentions}
            if len(spans) != len(mentions):
                raise DatasetValidationError(
                    f"{case['case_id']} repeated location roles reuse the wrong occurrence"
                )
    issue_codes = {item.code for item in extraction.issues}
    location_issue = (
        "PRIMARY_CITY_CONFIRMATION_REQUIRED"
        if extraction.locations.status.value == "EXACT"
        else "DESTINATION_NEEDS_CONFIRMATION"
    )
    if location_issue not in issue_codes:
        raise DatasetValidationError(f"{case['case_id']} location confirmation issue missing")
    party_issue = (
        "PARTY_SIZE_CONFIRMATION_REQUIRED"
        if extraction.party_size.total.quantifier.value == "EXACT"
        else "PARTY_SIZE_NEEDS_CONFIRMATION"
    )
    if party_issue not in issue_codes:
        raise DatasetValidationError(f"{case['case_id']} party confirmation issue missing")
    if "DATE_RANGE_MISSING_OR_INCOMPLETE" not in issue_codes:
        raise DatasetValidationError(f"{case['case_id']} complete date confirmation issue missing")
    if (
        extraction.temporal.days.quantifier.value != "EXACT"
        and "DURATION_NEEDS_CONFIRMATION" not in issue_codes
    ):
        raise DatasetValidationError(f"{case['case_id']} duration confirmation issue missing")
    has_departure_requirement = any(
        item.category == "time" and item.value == "LAST_DAY_NOON"
        for item in extraction.preferences.items
    )
    if has_departure_requirement and extraction.temporal.departure is None:
        raise DatasetValidationError(f"{case['case_id']} departure commitment missing")


def _coverage(cases: list[dict[str, Any]]) -> dict[str, Any]:
    hard = [item for item in cases if item["annotation"]["difficulty"] == "hard"]
    return {
        "count": len(cases),
        "difficulty": dict(Counter(item["annotation"]["difficulty"] for item in cases)),
        "destination": dict(Counter(item["annotation"]["coverage"]["destination"] for item in cases)),
        "party": dict(Counter(item["annotation"]["coverage"]["party"] for item in cases)),
        "duration": dict(Counter(item["annotation"]["coverage"]["duration"] for item in cases)),
        "generator_source": dict(Counter(item["annotation"]["generator_source"] for item in cases)),
        "minimums": {
            key: sum(bool(item["annotation"]["coverage"][key]) for item in cases)
            for key in ("preference", "roles", "interference", "fictional", "semantic_party")
        },
        "hard_fields": {
            "destination_non_exact": sum(
                item["annotation"]["coverage"]["destination"]
                in {"multiple", "uncertain", "missing"}
                for item in hard
            ),
            "party_unknown_or_range": sum(
                item["annotation"]["coverage"]["party"] in {"UNKNOWN", "RANGE"}
                for item in hard
            ),
            "duration_unknown_or_range": sum(
                item["annotation"]["coverage"]["duration"] in {"UNKNOWN", "RANGE"}
                for item in hard
            ),
        },
        "requirement_categories": dict(
            Counter(
                preference["category"]
                for case in cases
                for preference in case["expected"]["preferences"]["items"]
                if preference["polarity"] == "REQUIREMENT"
            )
        ),
    }


def _sum_coverage(parts: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"count": sum(part["count"] for part in parts)}
    for section in ("difficulty", "destination", "party", "duration", "generator_source", "minimums"):
        counter: Counter[str] = Counter()
        for part in parts:
            counter.update(part[section])
        result[section] = dict(counter)
    return result


def validate_dataset(
    data_root: Path,
    *,
    external_blind_labels: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    data_root = data_root.resolve(strict=True)
    repo_root = data_root.parents[2]
    backend_root = data_root.parents[1]
    custody_manifest = _read_json(data_root / "manifest.json")
    manifest = _read_json(
        manifest_path.resolve(strict=True)
        if manifest_path is not None
        else data_root / "manifest.json"
    )
    if manifest.get("code_bindings") != _current_code_bindings(backend_root):
        raise DatasetValidationError("manifest evaluator/schema code binding mismatch")
    for relative, expected_hash in manifest["files"].items():
        path = data_root / relative
        if _sha256(path) != expected_hash:
            raise DatasetValidationError(f"manifest hash mismatch: {relative}")

    dev = _read_jsonl(data_root / "dev.jsonl")
    validation = _read_jsonl(data_root / "validation.jsonl")
    blind_inputs = _read_jsonl(data_root / "frozen_blind.inputs.jsonl")
    if any(_contains_forbidden_key(item) for item in blind_inputs):
        raise DatasetValidationError("frozen blind inputs contain truth-bearing fields")
    labelled = [*dev, *validation]
    for case in labelled:
        _validate_labelled(case)

    all_ids = [item["case_id"] for item in [*labelled, *blind_inputs]]
    expected_ids = [f"TRIP_NLU_{index:04d}" for index in range(1, 121)]
    if all_ids != expected_ids:
        raise DatasetValidationError("case IDs must be continuous and split-ordered")
    normalized_texts = [item["input_text"].replace("\r\n", "\n") for item in [*labelled, *blind_inputs]]
    if len(normalized_texts) != len(set(normalized_texts)):
        raise DatasetValidationError("normalized duplicate input text detected")

    families = _read_jsonl(data_root / "family_registry.jsonl")
    if len(families) != 40 or any(item["case_count"] != 3 for item in families):
        raise DatasetValidationError("family registry must contain forty three-case families")
    family_split = {item["family_id"]: item["split"] for item in families}
    if len(family_split) != len(families):
        raise DatasetValidationError("family IDs must be unique across splits")
    case_to_family = {
        case_id: family["family_id"]
        for family in families
        for case_id in family.get("case_ids", [])
    }
    if len(case_to_family) != 120:
        raise DatasetValidationError("family registry must bind every case ID exactly once")
    observed_family_counts = Counter(case_to_family[item["case_id"]] for item in [*labelled, *blind_inputs])
    if set(observed_family_counts) != set(family_split) or any(count != 3 for count in observed_family_counts.values()):
        raise DatasetValidationError("case/family registry mismatch")
    cases_by_split = {"dev": dev, "validation": validation, "frozen_blind": blind_inputs}
    family_by_id = {item["family_id"]: item for item in families}
    for split, split_cases in cases_by_split.items():
        for case in split_cases:
            family = family_by_id[case_to_family[case["case_id"]]]
            if family["split"] != split:
                raise DatasetValidationError("case assigned to family from a different split")
            if split != "frozen_blind":
                if case["annotation"]["template_family_id"] != family["template_family_id"]:
                    raise DatasetValidationError("case/template family mismatch")
                if case["annotation"]["generator_family_id"] != family["generator_family_id"]:
                    raise DatasetValidationError("case/generator family mismatch")
                if case["annotation"]["renderer_id"] != family["renderer_id"]:
                    raise DatasetValidationError("case/renderer mismatch")
    near_duplicates = []
    for left_split, right_split in (("dev", "validation"), ("dev", "frozen_blind"), ("validation", "frozen_blind")):
        for left in cases_by_split[left_split]:
            left_text = unicodedata.normalize("NFC", left["input_text"].replace("\r\n", "\n"))
            for right in cases_by_split[right_split]:
                right_text = unicodedata.normalize("NFC", right["input_text"].replace("\r\n", "\n"))
                ratio = difflib.SequenceMatcher(None, left_text, right_text).ratio()
                if ratio >= 0.82:
                    near_duplicates.append((left["case_id"], right["case_id"], ratio))
    if near_duplicates:
        left, right, ratio = max(near_duplicates, key=lambda item: item[2])
        raise DatasetValidationError(
            f"cross-split near duplicate detected: {left}/{right} ratio={ratio:.4f}"
        )

    public_coverage = [_coverage(dev), _coverage(validation)]
    seal = _read_json(data_root / "sealed" / "frozen_blind.labels.jsonl")
    if seal.get("scoring_payload_present") is not False or seal.get("case_count") != 24:
        raise DatasetValidationError("blind label seal must be metadata-only")
    blind_labels_read = False
    if external_blind_labels is not None:
        external_blind_labels = external_blind_labels.resolve(strict=True)
        try:
            external_blind_labels.relative_to(repo_root)
        except ValueError:
            pass
        else:
            raise DatasetValidationError("external blind labels must be outside the repository")
        if _sha256(external_blind_labels) != seal["external_label_sha256"]:
            raise DatasetValidationError("external blind label commitment mismatch")
        blind_labels = _read_jsonl(external_blind_labels)
        if [item["case_id"] for item in blind_labels] != expected_ids[96:]:
            raise DatasetValidationError("blind label case set mismatch")
        for label, blind_input in zip(blind_labels, blind_inputs, strict=True):
            if label["input_text"] != blind_input["input_text"]:
                raise DatasetValidationError("blind input/label text mismatch")
            _validate_labelled(label)
        blind_coverage = _coverage(blind_labels)
        blind_labels_read = True
    else:
        receipt_path = data_root / "sealed" / "frozen_blind.validation_receipt.json"
        if "sealed/frozen_blind.validation_receipt.json" not in manifest["files"]:
            raise DatasetValidationError("isolated blind validation receipt is not finalized")
        receipt = _read_json(receipt_path)
        if receipt["external_label_sha256"] != seal["external_label_sha256"]:
            raise DatasetValidationError("blind validation receipt is not bound to the seal")
        expected_receipt_contract = {
            "schema_version": "trip-nlu-v2-blind-validation-receipt-v2",
            "validator_version": VALIDATOR_VERSION,
            "blind_labels_read": True,
            "case_count": 24,
            "case_details_present": False,
            "blind_input_sha256": _sha256(data_root / "frozen_blind.inputs.jsonl"),
            "family_registry_sha256": _sha256(data_root / "family_registry.jsonl"),
            "source_registry_sha256": _sha256(data_root / "source_registry.jsonl"),
            "generator_registry_sha256": _sha256(data_root / "generator_registry.json"),
            "seal_sha256": _sha256(data_root / "sealed" / "frozen_blind.labels.jsonl"),
            "case_ids_sha256": _case_ids_hash(expected_ids[96:]),
            "validator_sha256": custody_manifest["code_bindings"]["validator_sha256"],
            "scorer_sha256": custody_manifest["code_bindings"]["scorer_sha256"],
            "schema_sha256": custody_manifest["code_bindings"]["schema_sha256"],
        }
        if any(receipt.get(key) != value for key, value in expected_receipt_contract.items()):
            raise DatasetValidationError("isolated blind validation receipt binding mismatch")
        blind_coverage = receipt["coverage"]
    aggregate = _sum_coverage([*public_coverage, blind_coverage])
    for split_coverage in [*public_coverage, blind_coverage]:
        if any(value < 1 for value in split_coverage["hard_fields"].values()):
            raise DatasetValidationError("hard subset does not cover ambiguous key fields")
    for section, expected in EXPECTED_TOTAL.items():
        if section == "minimums":
            if any(aggregate[section].get(key, 0) < value for key, value in expected.items()):
                raise DatasetValidationError("minimum coverage quota not met")
        elif aggregate[section] != expected:
            raise DatasetValidationError(f"exact coverage mismatch: {section}")
    observed_requirement_categories = {
        item["category"]
        for case in labelled
        for item in case["expected"]["preferences"]["items"]
        if item["polarity"] == "REQUIREMENT"
    }
    if external_blind_labels is not None:
        observed_requirement_categories.update(
            item["category"]
            for case in blind_labels
            for item in case["expected"]["preferences"]["items"]
            if item["polarity"] == "REQUIREMENT"
        )
    else:
        observed_requirement_categories.update(receipt["coverage"]["requirement_categories"])
    if observed_requirement_categories != EXPECTED_REQUIREMENT_CATEGORIES:
        raise DatasetValidationError("required preference categories are not fully covered")

    return {
        "schema_version": "trip-nlu-v2-validation-receipt-v1",
        "validator_version": VALIDATOR_VERSION,
        "valid": True,
        "case_count": 120,
        "evidence_span_validity": 1.0,
        "blind_labels_read": blind_labels_read,
        "blind_label_commitment": seal["external_label_sha256"],
        "coverage": aggregate,
    }


def finalize_isolated_receipt(data_root: Path, external_blind_labels: Path) -> dict[str, Any]:
    data_root = data_root.resolve(strict=True)
    external_blind_labels = external_blind_labels.resolve(strict=True)
    result = validate_dataset(data_root, external_blind_labels=external_blind_labels)
    blind_labels = _read_jsonl(external_blind_labels)
    expected_ids = [f"TRIP_NLU_{index:04d}" for index in range(97, 121)]
    receipt = {
        "schema_version": "trip-nlu-v2-blind-validation-receipt-v2",
        "validator_version": VALIDATOR_VERSION,
        "blind_labels_read": True,
        "producer": "ISOLATED_VALIDATOR",
        "external_label_sha256": _sha256(external_blind_labels),
        "blind_input_sha256": _sha256(data_root / "frozen_blind.inputs.jsonl"),
        "family_registry_sha256": _sha256(data_root / "family_registry.jsonl"),
        "source_registry_sha256": _sha256(data_root / "source_registry.jsonl"),
        "generator_registry_sha256": _sha256(data_root / "generator_registry.json"),
        "seal_sha256": _sha256(data_root / "sealed" / "frozen_blind.labels.jsonl"),
        "case_ids_sha256": _case_ids_hash(expected_ids),
        "validator_sha256": _read_json(data_root / "manifest.json")["code_bindings"]["validator_sha256"],
        "scorer_sha256": _read_json(data_root / "manifest.json")["code_bindings"]["scorer_sha256"],
        "schema_sha256": _read_json(data_root / "manifest.json")["code_bindings"]["schema_sha256"],
        "case_count": 24,
        "coverage": _coverage(blind_labels),
        "case_details_present": False,
    }
    receipt_path = data_root / "sealed" / "frozen_blind.validation_receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest_path = data_root / "manifest.json"
    manifest = _read_json(manifest_path)
    manifest["files"]["sealed/frozen_blind.validation_receipt.json"] = _sha256(receipt_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_root", type=Path)
    parser.add_argument("--external-blind-labels", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--finalize-isolated-receipt", action="store_true")
    args = parser.parse_args()
    if args.finalize_isolated_receipt:
        if args.external_blind_labels is None:
            raise DatasetValidationError("receipt finalization requires external blind labels")
        result = finalize_isolated_receipt(args.data_root, args.external_blind_labels)
    else:
        result = validate_dataset(
            args.data_root,
            external_blind_labels=args.external_blind_labels,
            manifest_path=args.manifest,
        )
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
