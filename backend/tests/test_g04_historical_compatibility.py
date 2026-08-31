from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_g04_non_p5_regression import (
    BACKEND_ROOT,
    EXPECTED_FAILURE_FINGERPRINTS,
    FAIL_VERDICT,
    NON_P5_PYTEST_ARGS,
    PASS_VERDICT,
    FailedReport,
    RegressionObserver,
    evaluate_historical_compatibility,
    exact_compatibility_input_mismatches,
)


def _expected_failed_reports() -> list[FailedReport]:
    return [
        FailedReport(nodeid=nodeid, when="call", longrepr=f"DatasetValidationError: {fingerprint}")
        for nodeid, fingerprint in EXPECTED_FAILURE_FINGERPRINTS.items()
    ]


def _evaluate(
    *,
    exit_code: int = int(pytest.ExitCode.TESTS_FAILED),
    collected_nodeids: set[str] | None = None,
    failed_reports: list[FailedReport] | None = None,
    collection_failure_count: int = 0,
    internal_error_count: int = 0,
    keyboard_interrupt_count: int = 0,
    frozen_input_mismatches: tuple[str, ...] = (),
) -> dict[str, object]:
    return evaluate_historical_compatibility(
        exit_code=exit_code,
        collected_nodeids=set(EXPECTED_FAILURE_FINGERPRINTS)
        if collected_nodeids is None
        else collected_nodeids,
        failed_reports=_expected_failed_reports() if failed_reports is None else failed_reports,
        collection_failure_count=collection_failure_count,
        internal_error_count=internal_error_count,
        keyboard_interrupt_count=keyboard_interrupt_count,
        frozen_input_mismatches=frozen_input_mismatches,
    )


def test_exact_two_historical_failures_are_accepted_without_claiming_full_pytest_pass() -> None:
    result = _evaluate()

    assert result["verdict"] == PASS_VERDICT
    assert result["approved_exception_applied"] is True
    assert result["full_pytest_pass"] is False
    assert result["failure_reasons"] == []


def test_pytest_observer_uses_identity_hashing_for_plugin_registration() -> None:
    observer = RegressionObserver()

    assert isinstance(hash(observer), int)


def test_missing_expected_failure_is_rejected() -> None:
    result = _evaluate(failed_reports=_expected_failed_reports()[:1])

    assert result["verdict"] == FAIL_VERDICT
    assert "EXPECTED_FAILED_REPORT_MISSING" in result["failure_reasons"]


def test_extra_failure_is_rejected() -> None:
    failures = _expected_failed_reports()
    failures.append(FailedReport(nodeid="tests/test_other.py::test_new_failure", when="call", longrepr="boom"))

    result = _evaluate(failed_reports=failures)

    assert result["verdict"] == FAIL_VERDICT
    assert result["unexpected_failed_reports"] == ["tests/test_other.py::test_new_failure::call"]


def test_duplicate_expected_failure_is_rejected() -> None:
    failures = _expected_failed_reports()
    failures.append(failures[0])

    result = _evaluate(failed_reports=failures)

    assert result["verdict"] == FAIL_VERDICT
    assert result["unexpected_failed_reports"] == [f"{failures[0].nodeid}::call"]


def test_wrong_failure_fingerprint_is_rejected() -> None:
    failures = _expected_failed_reports()
    failures[0] = FailedReport(nodeid=failures[0].nodeid, when="call", longrepr="different historical error")

    result = _evaluate(failed_reports=failures)

    assert result["verdict"] == FAIL_VERDICT
    assert result["fingerprint_mismatches"] == [failures[0].nodeid]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"exit_code": int(pytest.ExitCode.OK)}, "PYTEST_EXIT_CODE_NOT_TESTS_FAILED"),
        ({"exit_code": int(pytest.ExitCode.INTERRUPTED)}, "PYTEST_EXIT_CODE_NOT_TESTS_FAILED"),
        ({"exit_code": int(pytest.ExitCode.INTERNAL_ERROR)}, "PYTEST_EXIT_CODE_NOT_TESTS_FAILED"),
        ({"exit_code": int(pytest.ExitCode.USAGE_ERROR)}, "PYTEST_EXIT_CODE_NOT_TESTS_FAILED"),
        ({"exit_code": int(pytest.ExitCode.NO_TESTS_COLLECTED)}, "PYTEST_EXIT_CODE_NOT_TESTS_FAILED"),
        ({"collection_failure_count": 1}, "COLLECTION_FAILURE"),
        ({"internal_error_count": 1}, "PYTEST_INTERNAL_ERROR"),
        ({"keyboard_interrupt_count": 1}, "PYTEST_KEYBOARD_INTERRUPT"),
        ({"frozen_input_mismatches": ("FILE_HASH_MISMATCH:manifest",)}, "EXACT_COMPATIBILITY_INPUT_MISMATCH"),
    ],
)
def test_non_test_failures_and_changed_frozen_inputs_are_rejected(
    kwargs: dict[str, object],
    reason: str,
) -> None:
    result = _evaluate(**kwargs)  # type: ignore[arg-type]

    assert result["verdict"] == FAIL_VERDICT
    assert reason in result["failure_reasons"]


def test_setup_or_teardown_failure_is_not_confused_with_an_approved_call_failure() -> None:
    failures = _expected_failed_reports()
    failures[0] = FailedReport(nodeid=failures[0].nodeid, when="setup", longrepr=failures[0].longrepr)

    result = _evaluate(failed_reports=failures)

    assert result["verdict"] == FAIL_VERDICT
    assert "EXPECTED_FAILED_REPORT_MISSING" in result["failure_reasons"]
    assert "UNEXPECTED_FAILED_REPORT" in result["failure_reasons"]


def test_expected_nodes_must_be_collected() -> None:
    result = _evaluate(collected_nodeids=set())

    assert result["verdict"] == FAIL_VERDICT
    assert "EXPECTED_NODE_NOT_COLLECTED" in result["failure_reasons"]


def test_windows_nodeids_are_normalized_before_exact_comparison() -> None:
    failures = [
        FailedReport(
            nodeid=report.nodeid.replace("/", "\\"),
            when=report.when,
            longrepr=report.longrepr,
        )
        for report in _expected_failed_reports()
    ]
    collected = {nodeid.replace("/", "\\") for nodeid in EXPECTED_FAILURE_FINGERPRINTS}

    result = _evaluate(collected_nodeids=collected, failed_reports=failures)

    assert result["verdict"] == PASS_VERDICT


def test_current_exact_compatibility_inputs_and_discovery_command_are_frozen() -> None:
    assert BACKEND_ROOT == Path(__file__).resolve().parents[1]
    assert exact_compatibility_input_mismatches(BACKEND_ROOT) == []
    assert NON_P5_PYTEST_ARGS == (
        "-q",
        "tests",
        "--ignore-glob=tests/test_trip_check_p5*.py",
    )
