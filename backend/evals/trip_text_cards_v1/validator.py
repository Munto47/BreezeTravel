from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from evals.trip_text_cards_v1.contracts import TextCardInputCase, normalized_text


EXPECTED_SPLITS = {"dev": 54, "validation": 18, "frozen_blind": 18}
EXPECTED_COHORTS = {"DEEP_CITY": 60, "OTHER_CITY": 15, "ADVERSARIAL": 15}
EXPECTED_FAMILIES_BY_SPLIT = {"dev": 18, "validation": 6, "frozen_blind": 6}
TRUTH_BEARING_KEYS = {
    "answer",
    "answers",
    "annotation",
    "annotations",
    "canonical_place",
    "expected",
    "gold",
    "label",
    "labels",
    "oracle",
    "oracles",
    "prediction",
    "predictions",
    "truth",
}


class DatasetValidationError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DatasetValidationError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise DatasetValidationError(f"{path.name}:{line_number} must be a JSON object")
        values.append(value)
    return values


def _contains_truth_key(value: object) -> bool:
    if isinstance(value, dict):
        if any(str(key).casefold() in TRUTH_BEARING_KEYS for key in value):
            return True
        return any(_contains_truth_key(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_truth_key(child) for child in value)
    return False


def load_cases(data_root: Path) -> dict[str, list[TextCardInputCase]]:
    return {
        split: [TextCardInputCase.model_validate(item) for item in read_jsonl(data_root / filename)]
        for split, filename in (
            ("dev", "dev.inputs.jsonl"),
            ("validation", "validation.inputs.jsonl"),
            ("frozen_blind", "frozen_blind.inputs.jsonl"),
        )
    }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def validate_dataset(data_root: Path) -> dict[str, Any]:
    data_root = data_root.resolve(strict=True)
    backend_root = data_root.parents[1]
    contract = _read_json(data_root / "dataset_contract.json")
    if contract.get("schema_version") != "g01-text-card-dataset-contract-v1":
        raise DatasetValidationError("unexpected dataset contract version")
    if contract.get("dataset_version") != "g01-text-card-dataset-v1":
        raise DatasetValidationError("unexpected dataset version")

    for relative, expected_hash in contract.get("files", {}).items():
        path = data_root / relative
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise DatasetValidationError(f"dataset byte binding mismatch: {relative}")

    generator_path = backend_root / "scripts" / "generate_g01_text_card_inputs.py"
    if sha256_file(generator_path) != contract.get("generator_sha256"):
        raise DatasetValidationError("dataset generator binding mismatch")

    raw_by_split = {
        "dev": read_jsonl(data_root / "dev.inputs.jsonl"),
        "validation": read_jsonl(data_root / "validation.inputs.jsonl"),
        "frozen_blind": read_jsonl(data_root / "frozen_blind.inputs.jsonl"),
    }
    if any(_contains_truth_key(case) for cases in raw_by_split.values() for case in cases):
        raise DatasetValidationError("input corpus contains truth-bearing keys")
    split_cases = {
        split: [TextCardInputCase.model_validate(case) for case in cases]
        for split, cases in raw_by_split.items()
    }

    if {split: len(cases) for split, cases in split_cases.items()} != EXPECTED_SPLITS:
        raise DatasetValidationError("split counts must equal 54/18/18")
    all_cases = [case for split in EXPECTED_SPLITS for case in split_cases[split]]
    expected_ids = [f"G01-TC-{index:03d}" for index in range(1, 91)]
    if [case.case_id for case in all_cases] != expected_ids:
        raise DatasetValidationError("case IDs must be continuous and split ordered")
    if any(case.split != split for split, cases in split_cases.items() for case in cases):
        raise DatasetValidationError("case split disagrees with containing file")

    cohorts = Counter(case.cohort for case in all_cases)
    if dict(cohorts) != EXPECTED_COHORTS:
        raise DatasetValidationError("cohort counts must equal 60/15/15")
    texts = [normalized_text(case.input_text) for case in all_cases]
    if len(set(texts)) != 90:
        raise DatasetValidationError("normalized input texts must be unique")
    if len({case.normalized_input_sha256 for case in all_cases}) != 90:
        raise DatasetValidationError("normalized input hashes must be unique")

    families: dict[str, list[TextCardInputCase]] = defaultdict(list)
    for case in all_cases:
        families[case.family_id].append(case)
    if len(families) != 30:
        raise DatasetValidationError("dataset must contain exactly 30 families")
    family_split_counts: Counter[str] = Counter()
    for family_id, cases in families.items():
        if len(cases) != 3 or {case.variant_id for case in cases} != {"A", "B", "C"}:
            raise DatasetValidationError(f"{family_id} must contain variants A/B/C")
        if len({case.split for case in cases}) != 1:
            raise DatasetValidationError(f"{family_id} crosses splits")
        if len({case.cohort for case in cases}) != 1:
            raise DatasetValidationError(f"{family_id} crosses cohorts")
        if len({tuple(case.city_scope) for case in cases}) != 1:
            raise DatasetValidationError(f"{family_id} changes city scope")
        family_split_counts[cases[0].split] += 1
        parent = cases[0].case_id
        by_variant = {case.variant_id: case for case in cases}
        if by_variant["A"].lineage.mutation_parent_case_id is not None:
            raise DatasetValidationError(f"{family_id} base variant cannot have a mutation parent")
        if any(
            by_variant[variant].lineage.mutation_parent_case_id != parent
            for variant in ("B", "C")
        ):
            raise DatasetValidationError(f"{family_id} mutation lineage is incomplete")
    if dict(family_split_counts) != EXPECTED_FAMILIES_BY_SPLIT:
        raise DatasetValidationError("family split counts must equal 18/6/6")

    truth_files = [
        path
        for path in data_root.rglob("*")
        if path.is_file()
        and path.suffix in {".json", ".jsonl"}
        and any(token in path.name.casefold() for token in ("label", "gold", "oracle", "annotation"))
        and path.name not in {"annotation.schema.json", "adjudication.schema.json"}
    ]
    if truth_files:
        raise DatasetValidationError("repository data root must not contain annotation or oracle payloads")
    if not _is_within(data_root, backend_root):
        raise DatasetValidationError("dataset root is not inside the backend checkout")

    return {
        "schema_version": "g01-text-card-dataset-validation-receipt-v1",
        "dataset_version": "g01-text-card-dataset-v1",
        "valid": True,
        "case_count": len(all_cases),
        "split_counts": {split: len(cases) for split, cases in split_cases.items()},
        "cohort_counts": dict(cohorts),
        "family_count": len(families),
        "family_split_counts": dict(family_split_counts),
        "family_isolation": "PASS",
        "normalized_text_unique": True,
        "truth_bearing_input_keys": 0,
        "repository_human_labels": 0,
        "dev_validation_annotation_status": "HITL_PENDING",
        "frozen_blind_truth_status": "EXTERNAL_CUSTODIAN_NOT_PROVISIONED",
        "gate_status": "NOT_RUN",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("eval_data/trip_text_cards_v1"),
    )
    args = parser.parse_args()
    print(json.dumps(validate_dataset(args.data_root), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
