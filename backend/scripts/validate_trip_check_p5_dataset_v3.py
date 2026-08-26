"""Fail-closed validator for the P5 v3 candidate or sealed dataset."""

from __future__ import annotations

import argparse
import hashlib
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

from evals.trip_check_v1.p5.adapters_v3 import validate_materialization_v3  # noqa: E402
from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3  # noqa: E402
from evals.trip_check_v1.p5.data_contract import digest, file_sha256, load_jsonl  # noqa: E402
from evals.trip_check_v1.p5.data_contract_v3 import (  # noqa: E402
    BLIND_INPUT_PATH_V3,
    BLIND_MATERIALIZATIONS_PATH_V3,
    BLIND_SEAL_PATH_V3,
    MANIFEST_PATH_V3,
    NONBLIND_MATERIALIZATIONS_PATH_V3,
    NONBLIND_PATH_V3,
    build_dataset_v3,
    build_manifest_v3,
    validate_v2_source_anchor,
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


def _walk_forbidden_paths(value: Any, *, path: str) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}"
            if str(key).casefold() in _FORBIDDEN_BLIND_KEYS:
                found.append(child)
            found.extend(_walk_forbidden_paths(item, path=child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_walk_forbidden_paths(item, path=f"{path}[{index}]"))
    return found


def _load_manifest() -> dict[str, Any]:
    value = json.loads(MANIFEST_PATH_V3.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("P5 v3 dataset manifest must be an object")
    return value


def _git_commit_is_current_ancestor(commit: str) -> bool:
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=BACKEND_ROOT.parent,
        capture_output=True,
        check=False,
        text=True,
    )
    if exists.returncode != 0:
        return False
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=BACKEND_ROOT.parent,
        capture_output=True,
        check=False,
        text=True,
    )
    return ancestor.returncode == 0


def _git_blob(commit: str, repository_path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f"{commit}:{repository_path}"],
        cwd=BACKEND_ROOT.parent,
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _git_candidate_tree_matches(
    commit: str, *, candidate_manifest_hash: str
) -> tuple[bool, str | None]:
    manifest_path = "backend/evals/trip_check_v1/p5/dataset_v3.manifest.json"
    manifest_bytes = _git_blob(commit, manifest_path)
    if manifest_bytes is None:
        return False, "candidate commit has no P5 v3 dataset manifest"
    try:
        candidate_manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False, "candidate commit P5 v3 dataset manifest is unreadable"
    if (
        not isinstance(candidate_manifest, Mapping)
        or candidate_manifest.get("manifest_hash") != candidate_manifest_hash
        or candidate_manifest.get("manifest_hash")
        != digest(
            {
                key: value
                for key, value in candidate_manifest.items()
                if key != "manifest_hash"
            }
        )
        or candidate_manifest.get("seal_status") != "PENDING_V3_SEAL"
        or candidate_manifest.get("frozen") is not False
        or candidate_manifest.get("formal_validation_eligible") is not False
        or "sealing_commitment" in candidate_manifest
    ):
        return False, "candidate commit manifest is not the exact pending freeze manifest"
    indexed_paths: list[tuple[str, str]] = []
    files = candidate_manifest.get("files")
    if not isinstance(files, Mapping):
        return False, "candidate commit manifest has no file index"
    for entry in files.values():
        if not isinstance(entry, Mapping):
            return False, "candidate commit manifest file entry is invalid"
        path = entry.get("path")
        expected_sha = entry.get("file_sha256")
        if not isinstance(path, str) or not isinstance(expected_sha, str):
            return False, "candidate commit manifest file binding is invalid"
        indexed_paths.append((f"backend/{path}", expected_sha))
    contracts = candidate_manifest.get("contract_hashes")
    if not isinstance(contracts, Mapping):
        return False, "candidate commit manifest has no contract index"
    for path_key, hash_key in (
        ("contracts_v3_path", "contracts_v3_sha256"),
        ("run_spec_template_path", "run_spec_template_sha256"),
        ("judge_rubric_path", "judge_rubric_sha256"),
    ):
        path = contracts.get(path_key)
        expected_sha = contracts.get(hash_key)
        if not isinstance(path, str) or not isinstance(expected_sha, str):
            return False, "candidate commit manifest contract binding is invalid"
        indexed_paths.append((f"backend/{path}", expected_sha))
    for path, expected_sha in indexed_paths:
        blob = _git_blob(commit, path)
        if blob is None or hashlib.sha256(blob).hexdigest() != expected_sha:
            return False, f"candidate commit blob differs from manifest: {path}"
    return True, None


def validate(*, formal: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    required = (
        NONBLIND_PATH_V3,
        BLIND_INPUT_PATH_V3,
        NONBLIND_MATERIALIZATIONS_PATH_V3,
        BLIND_MATERIALIZATIONS_PATH_V3,
        MANIFEST_PATH_V3,
    )
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        return {
            "schema_version": "trip-check-p5-dataset-validation-v3",
            "status": "REJECT",
            "formal": formal,
            "errors": [f"missing required files: {missing}"],
        }
    try:
        source_anchor = validate_v2_source_anchor()
        manifest = _load_manifest()
        nonblind = load_jsonl(NONBLIND_PATH_V3)
        blind = load_jsonl(BLIND_INPUT_PATH_V3)
        nonblind_materializations = load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V3)
        blind_materializations = load_jsonl(BLIND_MATERIALIZATIONS_PATH_V3)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "schema_version": "trip-check-p5-dataset-validation-v3",
            "status": "INVALID_EVIDENCE",
            "formal": formal,
            "errors": [str(exc)],
        }

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

    materialization_by_case = {
        str(row.get("case_id")): row for row in all_materializations
    }
    for row in all_cases:
        case_id = str(row.get("case_id", "<missing>"))
        try:
            case = P5CaseV3.model_validate(row)
        except Exception as exc:  # Pydantic produces useful, stable field context.
            errors.append(f"{case_id}: case contract {exc}")
            continue
        materialization = materialization_by_case.get(case_id)
        if materialization is None:
            continue
        try:
            validate_materialization_v3(case, materialization)
        except (TypeError, ValueError) as exc:
            errors.append(f"{case_id}: materialization contract {exc}")

    for row in blind:
        paths = _walk_forbidden_paths(row, path=str(row.get("case_id", "blind")))
        if paths:
            errors.append(f"blind input contains label fields: {paths}")
    for row in blind_materializations:
        paths = _walk_forbidden_paths(
            row, path=f"{row.get('case_id', 'blind')}.materialization"
        )
        if paths:
            errors.append(f"blind materialization contains label fields: {paths}")

    expected_counts = {
        "total": 360,
        "by_split": {"dev": 180, "frozen_blind": 90, "pilot": 18, "regression": 72},
        "by_city": {"上海": 120, "北京": 120, "杭州": 120},
        "screenshots_by_split": {"dev": 90, "frozen_blind": 45, "regression": 36},
    }
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
    if counts != expected_counts:
        errors.append(f"dataset counts differ: {counts}")
    screenshot_materializations = [
        row for row in all_materializations if row.get("ocr_baseline_receipt") is not None
    ]
    image_hashes = [row["render_receipt"]["image_sha256"] for row in screenshot_materializations]
    if len(image_hashes) != 171 or len(set(image_hashes)) != 171:
        errors.append("historical screenshot receipts must contain 171 unique image hashes")

    try:
        rebuilt = build_dataset_v3()
        checked_in = (
            nonblind,
            blind,
            nonblind_materializations,
            blind_materializations,
        )
        if any(expected != actual for expected, actual in zip(rebuilt, checked_in, strict=True)):
            errors.append("checked-in P5 v3 rows differ from deterministic sealed-v2 rebuild")
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        errors.append(f"deterministic P5 v3 rebuild failed: {exc}")

    sealing_commitment = manifest.get("sealing_commitment")
    try:
        expected_manifest = build_manifest_v3(
            nonblind_cases=nonblind,
            blind_cases=blind,
            nonblind_materializations=nonblind_materializations,
            blind_materializations=blind_materializations,
            sealing_commitment=(
                sealing_commitment if isinstance(sealing_commitment, Mapping) else None
            ),
        )
        if manifest != expected_manifest:
            errors.append("dataset manifest differs from bound files and source contracts")
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
            or not isinstance(sealing_commitment, Mapping)
        ):
            errors.append("formal validation requires a SEALED v3 manifest commitment")
        elif not BLIND_SEAL_PATH_V3.is_file():
            errors.append("formal validation requires the v3 blind seal")
        elif sealing_commitment.get("blind_seal_file_sha256") != file_sha256(
            BLIND_SEAL_PATH_V3
        ):
            errors.append("v3 sealing commitment blind seal hash mismatch")
        else:
            candidate_commit = str(sealing_commitment.get("candidate_freeze_commit", ""))
            candidate_manifest_hash = str(
                sealing_commitment.get("candidate_dataset_manifest_hash", "")
            )
            tree_matches, tree_error = _git_candidate_tree_matches(
                candidate_commit,
                candidate_manifest_hash=candidate_manifest_hash,
            )
            if not _git_commit_is_current_ancestor(candidate_commit):
                errors.append(
                    "v3 candidate freeze commit is missing or is not an ancestor of HEAD"
                )
            elif not tree_matches:
                errors.append(f"v3 candidate freeze tree mismatch: {tree_error}")

    return {
        "schema_version": "trip-check-p5-dataset-validation-v3",
        "status": "PASS" if not errors else "REJECT",
        "formal": formal,
        "blind_labels_read": False,
        "errors": errors,
        "counts": counts,
        "historical_ocr": {
            "receipt_count": len(image_hashes),
            "unique_image_hashes": len(set(image_hashes)),
            "fresh_actual_ocr_execution": "NOT_RUN",
        },
        "source_v2_anchor": source_anchor,
        "manifest_hash": manifest.get("manifest_hash"),
        "manifest_file_sha256": file_sha256(MANIFEST_PATH_V3),
        "seal_status": manifest.get("seal_status"),
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
