"""Read-only helpers shared by the P5 v2 end-to-end contract tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
P5_ROOT = BACKEND_ROOT / "evals" / "trip_check_v1" / "p5"
DATASET_MANIFEST_PATH = P5_ROOT / "dataset_v2.manifest.json"
NONBLIND_CASES_PATH = P5_ROOT / "cases_nonblind_v2.jsonl"
BLIND_CASES_PATH = P5_ROOT / "frozen_blind.v2.inputs.jsonl"
NONBLIND_MATERIALIZATIONS_PATH = P5_ROOT / "materializations_nonblind_v2.jsonl"
BLIND_MATERIALIZATIONS_PATH = P5_ROOT / "frozen_blind.v2.materializations.jsonl"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path.name} is not a JSON object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def materializations_by_case() -> dict[str, dict[str, Any]]:
    rows = [
        *load_jsonl(NONBLIND_MATERIALIZATIONS_PATH),
        *load_jsonl(BLIND_MATERIALIZATIONS_PATH),
    ]
    return {row["case_id"]: row for row in rows}
