from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evals.g07_candidate.browser_performance import (
    EXPECTED_BROWSER_FILE_COUNTS,
    PERFORMANCE_CHAIN_COUNT,
    _browser_database_name,
    _service_environment,
    run_live_performance_evidence,
    validate_browser_report,
)
from evals.trip_check_v1.p6.contracts_v1 import P6ContractError


REPOSITORY_ROOT = Path(__file__).parents[2]
SUBJECT = "7" * 40
TREE = "8" * 40


def _browser_report(*, skipped: int = 0) -> dict:
    specs = []
    index = 0
    for file_name, count in EXPECTED_BROWSER_FILE_COUNTS.items():
        for _ in range(count):
            index += 1
            specs.append(
                {
                    "title": f"candidate journey {index}",
                    "file": file_name,
                    "tests": [
                        {
                            "status": "expected",
                            "results": [{"status": "passed"}],
                        }
                    ],
                }
            )
    return {
        "config": {
            "metadata": {
                "commit_sha": SUBJECT,
                "evidence_class": "CONTROLLED_BROWSER_FIXTURE",
                "evidence_scope": "G07_G5_FULL_PRODUCT_CHAIN",
                "live_provider_evidence": False,
                "public_e2e_evidence": False,
                "human_evidence": False,
            }
        },
        "suites": [{"specs": specs, "suites": []}],
        "errors": [],
        "stats": {
            "expected": sum(EXPECTED_BROWSER_FILE_COUNTS.values()) - skipped,
            "unexpected": 0,
            "flaky": 0,
            "skipped": skipped,
        },
    }


def test_browser_report_requires_all_46_exact_subject_tests() -> None:
    proof = validate_browser_report(_browser_report(), SUBJECT)
    assert proof["test_count"] == 46
    assert proof["file_counts"] == EXPECTED_BROWSER_FILE_COUNTS


def test_browser_report_rejects_skipped_test() -> None:
    with pytest.raises(P6ContractError, match="G07_G5_BROWSER_MATRIX_FAILED"):
        validate_browser_report(_browser_report(skipped=1), SUBJECT)


def test_browser_fixture_services_are_isolated_from_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "must-not-propagate")
    monkeypatch.setenv("AMAP_API_KEY", "must-not-propagate")
    environment = _service_environment(
        database_url="postgresql://postgres:postgres@127.0.0.1:55433/browser",
        database_admin_url="postgresql://postgres:postgres@127.0.0.1:55433/postgres",
        redis_url="redis://127.0.0.1:56379",
    )
    assert environment["QWEN_API_KEY"] == ""
    assert environment["AMAP_API_KEY"] == ""
    assert environment["TRIP_UNDERSTANDING_PROVIDER_MODE"] == "fixture"
    assert environment["DATABASE_URL"].startswith("postgresql+asyncpg://")
    assert _browser_database_name(SUBJECT) == (
        "breezetravel_g07_browser_777777777777"
    )


def _chain(*, cards_ms: float = 4_000.0) -> dict[str, object]:
    return {
        "candidate_commit": SUBJECT,
        "candidate_tree": TREE,
        "exact_model_id": "qwen3.7-flash-2026-07-15",
        "source_sha256": "a" * 64,
        "create_to_progress_ms": 25.0,
        "create_to_editable_cards_ms": cards_ms,
        "public_result_status": "PARTIAL_RESULT",
        "public_card_count": 6,
        "public_forbidden_key_count": 0,
        "qwen_external_calls": 1,
        "qwen_repair_calls": 0,
        "qwen_input_tokens": 1_000,
        "qwen_output_tokens": 500,
        "qwen_estimated_cost_cny": 0.001,
        "persisted_activity_count": 10,
        "persisted_auto_match_count": 5,
        "persisted_coordinate_receipt_count": 5,
        "initial_map_job_count": 1,
        "initial_map_terminal_status": "AVAILABLE",
        "route_effect_count": 4,
        "route_external_call_count": 4,
        "edit_status": "APPLIED",
        "map_status_after_edit": "NEEDS_UPDATE",
        "automatic_route_calls_after_edit": 0,
        "raw_request_or_response_retained": False,
        "database_source": "ISOLATED_POSTGRESQL_APPLICATION_TABLES",
        "isolated_database_destroyed_after_receipt": True,
        "blind_inputs_read": 0,
        "blind_truth_read": 0,
        "human_evidence": False,
    }


def test_live_performance_requires_and_hashes_50_authentic_chains(
    tmp_path: Path,
) -> None:
    calls = 0

    async def runner(_args) -> dict[str, object]:  # noqa: ANN001
        nonlocal calls
        calls += 1
        return _chain()

    receipt = asyncio.run(
        run_live_performance_evidence(
            output_root=tmp_path / "performance",
            repo_root=REPOSITORY_ROOT,
            database_admin_url="postgresql://unused",
            formal=False,
            subject_commit=SUBJECT,
            candidate_tree=TREE,
            chain_runner=runner,
        )
    )
    assert calls == PERFORMANCE_CHAIN_COUNT == 50
    assert receipt["status"] == "PASS"
    assert receipt["sample_count"] == 50
    assert receipt["qwen_external_call_count"] == 50
    assert receipt["route_external_call_count"] == 200
    assert receipt["sample_file_sha256"]


def test_live_performance_preserves_threshold_failure(tmp_path: Path) -> None:
    async def slow_runner(_args) -> dict[str, object]:  # noqa: ANN001
        return _chain(cards_ms=8_001.0)

    receipt = asyncio.run(
        run_live_performance_evidence(
            output_root=tmp_path / "slow-performance",
            repo_root=REPOSITORY_ROOT,
            database_admin_url="postgresql://unused",
            formal=False,
            subject_commit=SUBJECT,
            candidate_tree=TREE,
            chain_runner=slow_runner,
        )
    )
    assert receipt["status"] == "FAIL"
    assert receipt["threshold_failures"] == [
        "create_to_editable_cards_p95_ms"
    ]


def test_live_performance_rejects_internal_output_root() -> None:
    async def runner(_args) -> dict[str, object]:  # noqa: ANN001
        return _chain()

    with pytest.raises(P6ContractError, match="G07_G5_EXTERNAL_ROOT_REQUIRED"):
        asyncio.run(
            run_live_performance_evidence(
                output_root=REPOSITORY_ROOT / "not-allowed",
                repo_root=REPOSITORY_ROOT,
                database_admin_url="postgresql://unused",
                formal=False,
                subject_commit=SUBJECT,
                candidate_tree=TREE,
                chain_runner=runner,
            )
        )
