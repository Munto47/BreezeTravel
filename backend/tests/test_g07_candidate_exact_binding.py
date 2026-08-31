from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.trip_nlu_v2.validator import _current_code_bindings
from scripts import run_g04_non_p5_regression


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = BACKEND_ROOT / "eval_data/trip_nlu_v2"
CANDIDATE_MANIFEST = (
    BACKEND_ROOT / "eval_data/trip_nlu_v2_remediation/candidate_manifest.json"
)
CUSTODY_MANIFEST_SHA256 = (
    "cab1056d3a435f7a4c576a97f0d6d75ef17b8d4ed6833721ea038b64db52b0ab"
)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_candidate_manifest_exactly_binds_current_evaluator_without_mutating_data() -> None:
    custody = _read_json(DATA_ROOT / "manifest.json")
    candidate = _read_json(CANDIDATE_MANIFEST)

    assert _sha256(DATA_ROOT / "manifest.json") == CUSTODY_MANIFEST_SHA256
    assert candidate["code_bindings"] == _current_code_bindings(BACKEND_ROOT)
    assert candidate["files"] == custody["files"]
    for relative, expected in candidate["files"].items():
        assert _sha256(DATA_ROOT / str(relative)) == expected


def test_scheme_a_runner_now_propagates_native_pytest_verdict(monkeypatch) -> None:
    observed: list[list[str]] = []
    monkeypatch.setattr(run_g04_non_p5_regression.os, "chdir", lambda path: None)

    def passing(args: list[str]) -> pytest.ExitCode:
        observed.append(args)
        return pytest.ExitCode.OK

    monkeypatch.setattr(run_g04_non_p5_regression.pytest, "main", passing)
    assert run_g04_non_p5_regression.main() == 0
    assert observed == [list(run_g04_non_p5_regression.NON_P5_PYTEST_ARGS)]

    monkeypatch.setattr(
        run_g04_non_p5_regression.pytest,
        "main",
        lambda args: pytest.ExitCode.TESTS_FAILED,
    )
    assert run_g04_non_p5_regression.main() == 1
    assert not hasattr(run_g04_non_p5_regression, "EXPECTED_FAILURE_FINGERPRINTS")
    assert not hasattr(run_g04_non_p5_regression, "PASS_VERDICT")
