from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Sequence

import pytest

from app.trip_intake.models import TripIntakeExtraction


BACKEND_ROOT = Path(__file__).resolve().parents[1]
NON_P5_PYTEST_ARGS: Final[tuple[str, ...]] = (
    "-q",
    "tests",
    "--ignore-glob=tests/test_trip_check_p5*.py",
)
EXPECTED_FAILURE_FINGERPRINTS: Final[dict[str, str]] = {
    (
        "tests/test_trip_nlu_v2_gate.py::"
        "test_public_validator_proves_exact_120_case_contract_without_reading_blind_truth"
    ): "manifest evaluator/schema code binding mismatch",
    (
        "tests/test_trip_nlu_v2_gate.py::"
        "test_external_labels_inside_repository_are_rejected"
    ): "manifest evaluator/schema code binding mismatch",
}
EXACT_COMPATIBILITY_FILE_SHA256: Final[dict[str, str]] = {
    "eval_data/trip_nlu_v2/manifest.json": "cab1056d3a435f7a4c576a97f0d6d75ef17b8d4ed6833721ea038b64db52b0ab",
    (
        "eval_data/trip_nlu_v2_remediation/candidate_manifest.json"
    ): "638ee916bb16f6f0774262aa8e0a51e04da976e87cd7f9507a59b6209da76fd9",
    "evals/trip_nlu_v2/validator.py": "9acf725ad2b827083841f3e7ad16cda0740ca868181c6a3e995c2a0bdafc574e",
    "evals/trip_nlu_v2/scorer.py": "b602297e3c6f6697c116772b7976c78fea11f4c0279b2b1e3f218383cb320be5",
    "evals/trip_nlu_v2/gate.py": "6469db3eb556aaf016fdb89fa757385433389c02ddb5628f624b44f059c84683",
    "tests/test_trip_nlu_v2_gate.py": "5bb7db7e69af3797ec493a8f6bcda8d3b1ea77ace8d7ad7633dbaf62829f1996",
    "scripts/generate_trip_nlu_v2.py": "b8853f04db846fe72b77d8428b289eb2472aea4adae443bbaa69ee49e320756f",
}
EXACT_SCHEMA_SHA256: Final[str] = "fe5f80bb8d173079021751aaac78b54703b49ef435e2a4fffc8c29b9f64d3b4f"
PASS_VERDICT: Final[str] = "PASS_WITH_APPROVED_HISTORICAL_EXCEPTION"
FAIL_VERDICT: Final[str] = "FAIL_CLOSED"


@dataclass(frozen=True)
class FailedReport:
    nodeid: str
    when: str
    longrepr: str


@dataclass(eq=False)
class RegressionObserver:
    collected_nodeids: set[str] = field(default_factory=set)
    failed_reports: list[FailedReport] = field(default_factory=list)
    collection_failure_count: int = 0
    internal_error_count: int = 0
    keyboard_interrupt_count: int = 0

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        self.collected_nodeids.update(_normalize_nodeid(item.nodeid) for item in items)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.collection_failure_count += 1

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.failed:
            longreprtext = getattr(report, "longreprtext", None)
            self.failed_reports.append(
                FailedReport(
                    nodeid=_normalize_nodeid(report.nodeid),
                    when=report.when,
                    longrepr=longreprtext if longreprtext else str(report.longrepr),
                )
            )

    def pytest_internalerror(self, excrepr: object, excinfo: object) -> None:
        del excrepr, excinfo
        self.internal_error_count += 1

    def pytest_keyboard_interrupt(self, excinfo: object) -> None:
        del excinfo
        self.keyboard_interrupt_count += 1


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_nodeid(nodeid: str) -> str:
    return nodeid.replace("\\", "/")


def _schema_sha256() -> str:
    return hashlib.sha256(
        json.dumps(
            TripIntakeExtraction.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def exact_compatibility_input_mismatches(backend_root: Path = BACKEND_ROOT) -> list[str]:
    mismatches: list[str] = []
    for relative_path, expected_hash in EXACT_COMPATIBILITY_FILE_SHA256.items():
        path = backend_root / relative_path
        if not path.is_file():
            mismatches.append(f"MISSING_FILE:{relative_path}")
            continue
        if _sha256(path) != expected_hash:
            mismatches.append(f"FILE_HASH_MISMATCH:{relative_path}")
    if _schema_sha256() != EXACT_SCHEMA_SHA256:
        mismatches.append("SCHEMA_HASH_MISMATCH:TripIntakeExtraction")
    return mismatches


def evaluate_historical_compatibility(
    *,
    exit_code: int,
    collected_nodeids: set[str],
    failed_reports: Sequence[FailedReport],
    collection_failure_count: int = 0,
    internal_error_count: int = 0,
    keyboard_interrupt_count: int = 0,
    frozen_input_mismatches: Sequence[str] = (),
) -> dict[str, object]:
    normalized_collected_nodeids = {_normalize_nodeid(nodeid) for nodeid in collected_nodeids}
    normalized_failed_reports = [
        FailedReport(
            nodeid=_normalize_nodeid(report.nodeid),
            when=report.when,
            longrepr=report.longrepr,
        )
        for report in failed_reports
    ]
    expected_reports = Counter((nodeid, "call") for nodeid in EXPECTED_FAILURE_FINGERPRINTS)
    observed_reports = Counter((report.nodeid, report.when) for report in normalized_failed_reports)
    expected_nodeids = set(EXPECTED_FAILURE_FINGERPRINTS)
    missing_collected_nodes = sorted(expected_nodeids - normalized_collected_nodeids)
    missing_failed_reports = sorted(
        f"{nodeid}::{when}"
        for (nodeid, when), count in (expected_reports - observed_reports).items()
        for _ in range(count)
    )
    unexpected_failed_reports = sorted(
        f"{nodeid}::{when}"
        for (nodeid, when), count in (observed_reports - expected_reports).items()
        for _ in range(count)
    )
    fingerprint_mismatches = sorted(
        nodeid
        for nodeid, fingerprint in EXPECTED_FAILURE_FINGERPRINTS.items()
        if len(
            matching_reports := [
                report
                for report in normalized_failed_reports
                if report.nodeid == nodeid and report.when == "call"
            ]
        )
        == 1
        and fingerprint not in matching_reports[0].longrepr
    )

    reasons: list[str] = []
    if exit_code != int(pytest.ExitCode.TESTS_FAILED):
        reasons.append("PYTEST_EXIT_CODE_NOT_TESTS_FAILED")
    if missing_collected_nodes:
        reasons.append("EXPECTED_NODE_NOT_COLLECTED")
    if missing_failed_reports:
        reasons.append("EXPECTED_FAILED_REPORT_MISSING")
    if unexpected_failed_reports:
        reasons.append("UNEXPECTED_FAILED_REPORT")
    if fingerprint_mismatches:
        reasons.append("EXPECTED_FAILURE_FINGERPRINT_MISMATCH")
    if collection_failure_count:
        reasons.append("COLLECTION_FAILURE")
    if internal_error_count:
        reasons.append("PYTEST_INTERNAL_ERROR")
    if keyboard_interrupt_count:
        reasons.append("PYTEST_KEYBOARD_INTERRUPT")
    if frozen_input_mismatches:
        reasons.append("EXACT_COMPATIBILITY_INPUT_MISMATCH")

    approved = not reasons
    return {
        "schema_version": "g04-historical-compatibility-v1",
        "policy": "HISTORICAL_BINDING_INVALID_FROZEN",
        "verdict": PASS_VERDICT if approved else FAIL_VERDICT,
        "approved_exception_applied": approved,
        "full_pytest_pass": exit_code == int(pytest.ExitCode.OK),
        "pytest_exit_code": exit_code,
        "expected_failure_count": len(EXPECTED_FAILURE_FINGERPRINTS),
        "observed_failed_report_count": len(normalized_failed_reports),
        "missing_collected_nodes": missing_collected_nodes,
        "missing_failed_reports": missing_failed_reports,
        "unexpected_failed_reports": unexpected_failed_reports,
        "fingerprint_mismatches": fingerprint_mismatches,
        "collection_failure_count": collection_failure_count,
        "internal_error_count": internal_error_count,
        "keyboard_interrupt_count": keyboard_interrupt_count,
        "frozen_input_mismatches": sorted(frozen_input_mismatches),
        "failure_reasons": reasons,
    }


def main() -> int:
    frozen_input_mismatches = exact_compatibility_input_mismatches()
    if frozen_input_mismatches:
        result = evaluate_historical_compatibility(
            exit_code=-1,
            collected_nodeids=set(),
            failed_reports=(),
            frozen_input_mismatches=frozen_input_mismatches,
        )
        print("G04_HISTORICAL_COMPATIBILITY " + json.dumps(result, sort_keys=True))
        return 1

    observer = RegressionObserver()
    os.chdir(BACKEND_ROOT)
    exit_code = int(pytest.main(list(NON_P5_PYTEST_ARGS), plugins=[observer]))
    result = evaluate_historical_compatibility(
        exit_code=exit_code,
        collected_nodeids=observer.collected_nodeids,
        failed_reports=observer.failed_reports,
        collection_failure_count=observer.collection_failure_count,
        internal_error_count=observer.internal_error_count,
        keyboard_interrupt_count=observer.keyboard_interrupt_count,
    )
    print("G04_HISTORICAL_COMPATIBILITY " + json.dumps(result, sort_keys=True))
    return 0 if result["approved_exception_applied"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
