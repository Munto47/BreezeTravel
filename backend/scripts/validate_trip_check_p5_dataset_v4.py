"""Fail-closed validator for the P5 v4 candidate or sealed dataset."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.active_contract import (  # noqa: E402
    P5ContractNotReadyError,
    require_v4_formal_ready,
)
from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3  # noqa: E402
from evals.trip_check_v1.p5.data_contract import digest, file_sha256, load_jsonl  # noqa: E402
from evals.trip_check_v1.p5.data_contract_v3 import (  # noqa: E402
    BLIND_INPUT_PATH_V3,
    BLIND_MATERIALIZATIONS_PATH_V3,
)
from evals.trip_check_v1.p5.data_contract_v4 import (  # noqa: E402
    BLIND_INPUT_PATH_V4,
    BLIND_MATERIALIZATIONS_PATH_V4,
    BLIND_SEAL_PATH_V4,
    MANIFEST_PATH_V4,
    NONBLIND_MATERIALIZATIONS_PATH_V4,
    NONBLIND_PATH_V4,
    build_dataset_v4,
    build_manifest_v4,
    validate_materialization_v4,
    validate_v3_source_anchor,
)


_FORBIDDEN_BLIND_KEYS = {
    "answer",
    "blind_label",
    "expected",
    "ground_truth",
    "human_label",
    "label",
    "oracle",
    "oracle_sha256",
}


def _walk_forbidden(value: Any, *, path: str) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}"
            if str(key).casefold() in _FORBIDDEN_BLIND_KEYS:
                found.append(child)
            found.extend(_walk_forbidden(item, path=child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_walk_forbidden(item, path=f"{path}[{index}]"))
    return found


def _load_manifest() -> dict[str, Any]:
    value = json.loads(MANIFEST_PATH_V4.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("P5 v4 manifest must be an object")
    return value


def _git_candidate_is_ancestor(commit: str) -> bool:
    if len(commit) != 40:
        return False
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=BACKEND_ROOT.parent,
        capture_output=True,
        check=False,
    )
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=BACKEND_ROOT.parent,
        capture_output=True,
        check=False,
    )
    return exists.returncode == 0 and ancestor.returncode == 0


def validate(*, formal: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    required = (
        NONBLIND_PATH_V4,
        BLIND_INPUT_PATH_V4,
        NONBLIND_MATERIALIZATIONS_PATH_V4,
        BLIND_MATERIALIZATIONS_PATH_V4,
        MANIFEST_PATH_V4,
    )
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        return {
            "schema_version": "trip-check-p5-dataset-validation-v4",
            "status": "REJECT",
            "formal": formal,
            "blind_labels_read": False,
            "external_bundle_read": False,
            "errors": [f"missing required files: {missing}"],
        }
    try:
        source_anchor = validate_v3_source_anchor()
        manifest = _load_manifest()
        nonblind = load_jsonl(NONBLIND_PATH_V4)
        blind = load_jsonl(BLIND_INPUT_PATH_V4)
        nonblind_materializations = load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V4)
        blind_materializations = load_jsonl(BLIND_MATERIALIZATIONS_PATH_V4)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "schema_version": "trip-check-p5-dataset-validation-v4",
            "status": "INVALID_EVIDENCE",
            "formal": formal,
            "blind_labels_read": False,
            "external_bundle_read": False,
            "errors": [str(exc)],
        }

    if BLIND_INPUT_PATH_V4.read_bytes() != BLIND_INPUT_PATH_V3.read_bytes():
        errors.append("v4 blind input bytes must equal frozen v3 bytes")
    if (
        BLIND_MATERIALIZATIONS_PATH_V4.read_bytes()
        != BLIND_MATERIALIZATIONS_PATH_V3.read_bytes()
    ):
        errors.append("v4 blind materialization bytes must equal frozen v3 bytes")
    for row in [*blind, *blind_materializations]:
        forbidden = _walk_forbidden(row, path=str(row.get("case_id", "blind")))
        if forbidden:
            errors.append(f"blind row contains label fields: {forbidden}")

    all_cases = [*nonblind, *blind]
    all_materializations = [*nonblind_materializations, *blind_materializations]
    case_ids = [str(row.get("case_id")) for row in all_cases]
    materialization_ids = [str(row.get("case_id")) for row in all_materializations]
    if len(case_ids) != len(set(case_ids)):
        errors.append("case IDs must be unique")
    if len(materialization_ids) != len(set(materialization_ids)):
        errors.append("materialization case IDs must be unique")
    if set(case_ids) != set(materialization_ids):
        errors.append("case/materialization case-id sets differ")
    materialization_by_case = {row["case_id"]: row for row in all_materializations}
    for row in all_cases:
        case_id = str(row.get("case_id", "<missing>"))
        try:
            case = P5CaseV3.model_validate(row)
            validate_materialization_v4(case, materialization_by_case[case_id])
        except Exception as exc:  # Pydantic context is part of the evidence.
            errors.append(f"{case_id}: v4 payload contract {exc}")

    counts = {
        "total": len(all_cases),
        "by_split": dict(sorted(Counter(row.get("split") for row in all_cases).items())),
        "by_city": dict(sorted(Counter(row.get("city") for row in all_cases).items())),
        "screenshots_by_split": dict(
            sorted(
                Counter(
                    row.get("split")
                    for row in all_cases
                    if row.get("input_kind") == "SYNTHETIC_SCREENSHOT"
                ).items()
            )
        ),
    }
    expected_counts = {
        "total": 360,
        "by_split": {"dev": 180, "frozen_blind": 90, "pilot": 18, "regression": 72},
        "by_city": {"上海": 120, "北京": 120, "杭州": 120},
        "screenshots_by_split": {"dev": 90, "frozen_blind": 45, "regression": 36},
    }
    if counts != expected_counts:
        errors.append(f"dataset counts differ: {counts}")

    try:
        rebuilt = build_dataset_v4()
        checked_in = (
            nonblind,
            blind,
            nonblind_materializations,
            blind_materializations,
        )
        if any(expected != actual for expected, actual in zip(rebuilt, checked_in, strict=True)):
            errors.append("checked-in P5 v4 rows differ from deterministic v3/P1 rebuild")
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        errors.append(f"deterministic P5 v4 rebuild failed: {exc}")

    commitment = manifest.get("sealing_commitment")
    try:
        expected_manifest = build_manifest_v4(
            nonblind_cases=nonblind,
            blind_cases=blind,
            nonblind_materializations=nonblind_materializations,
            blind_materializations=blind_materializations,
            sealing_commitment=(commitment if isinstance(commitment, Mapping) else None),
        )
        if manifest != expected_manifest:
            errors.append("dataset manifest differs from bound v4 files/contracts")
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        errors.append(f"dataset manifest rebuild failed: {exc}")
    if manifest.get("manifest_hash") != digest(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    ):
        errors.append("dataset manifest hash mismatch")

    if formal:
        if (
            manifest.get("frozen") is not True
            or manifest.get("formal_validation_eligible") is not True
            or manifest.get("seal_status") != "SEALED"
            or not isinstance(commitment, Mapping)
        ):
            errors.append("formal validation requires a SEALED v4 manifest")
        elif not BLIND_SEAL_PATH_V4.is_file():
            errors.append("formal validation requires the v4 blind seal")
        elif commitment.get("blind_seal_file_sha256") != file_sha256(BLIND_SEAL_PATH_V4):
            errors.append("v4 sealing commitment blind seal hash mismatch")
        elif not _git_candidate_is_ancestor(
            str(commitment.get("candidate_freeze_commit", ""))
        ):
            errors.append("v4 candidate freeze commit is missing/not an ancestor")
        try:
            require_v4_formal_ready()
        except P5ContractNotReadyError as exc:
            errors.append(str(exc))

    return {
        "schema_version": "trip-check-p5-dataset-validation-v4",
        "status": "PASS" if not errors else "REJECT",
        "formal": formal,
        "blind_labels_read": False,
        "external_bundle_read": False,
        "errors": errors,
        "counts": counts,
        "source_v3_anchor": source_anchor,
        "manifest_hash": manifest.get("manifest_hash"),
        "manifest_file_sha256": file_sha256(MANIFEST_PATH_V4),
        "seal_status": manifest.get("seal_status"),
        "route_evidence_repairs": [
            "p5.pilot.bj.004",
            "p5.pilot.sh.001",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--formal", action="store_true")
    args = parser.parse_args()
    result = validate(formal=args.formal)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
