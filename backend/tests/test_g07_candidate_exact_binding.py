from __future__ import annotations

import hashlib
import json
import subprocess
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
RECEIPT_PATH = (
    BACKEND_ROOT.parent / "docs/governance/gate-results/G07.exact-binding.json"
)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(BACKEND_ROOT.parent), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _git_blob_sha256(commit: str, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(BACKEND_ROOT.parent), "show", f"{commit}:{path}"],
        check=True,
        capture_output=True,
    )
    return hashlib.sha256(result.stdout).hexdigest()


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


def test_exact_binding_receipt_binds_subject_and_preserves_historical_receipts() -> None:
    receipt = _read_json(RECEIPT_PATH)
    subject = receipt["subject"]
    binding = receipt["binding"]
    removal = receipt["exception_removal"]

    assert receipt["verdict"] == "PASS"
    assert _git("rev-parse", f"{subject['candidate_commit']}^{{tree}}") == subject[
        "candidate_tree"
    ]
    assert _git("merge-base", "--is-ancestor", subject["baseline_commit"], subject["candidate_commit"]) == ""
    assert binding["candidate_manifest_sha256"] == _git_blob_sha256(
        subject["candidate_commit"], binding["candidate_manifest_path"]
    )
    assert binding["custody_manifest_sha256"] == _git_blob_sha256(
        subject["candidate_commit"], binding["custody_manifest_path"]
    )
    assert binding["code_bindings"] == _current_code_bindings(BACKEND_ROOT)
    assert binding["data_files_match_custody_manifest"] is True
    assert binding["blind_oracle_bytes_changed"] is False
    assert removal["approved_exception_applied"] is False
    assert removal["approved_failure_count"] == removal["unexpected_failure_count"] == 0
    assert removal["full_pytest_pass"] is True
    assert removal["native_pytest_exit_code"] == 0
    for path_key, hash_key in (
        ("runner_path", "runner_sha256"),
        ("g01_guard_path", "g01_guard_sha256"),
        ("exact_binding_test_path", "exact_binding_test_sha256"),
    ):
        assert removal[hash_key] == _git_blob_sha256(
            subject["candidate_commit"], removal[path_key]
        )
    assert receipt["historical_receipts"] == {
        "mutated": False,
        "G04_sha256": _sha256(
            BACKEND_ROOT.parent / "docs/governance/gate-results/G04.product-delivery.json"
        ),
        "G05_sha256": _sha256(
            BACKEND_ROOT.parent / "docs/governance/gate-results/G05.product-delivery.json"
        ),
        "G06_sha256": _sha256(
            BACKEND_ROOT.parent / "docs/governance/gate-results/G06.product-delivery.json"
        ),
    }
