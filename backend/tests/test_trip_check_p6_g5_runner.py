from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from evals.trip_check_v1.p6.contracts_v1 import P6ContractError, digest, file_sha256
from evals.trip_check_v1.p6.g5_runner import run_g5_gate
from evals.trip_check_v1.p6.local_browser_runner import REQUIRED_COVERAGE
from evals.trip_check_v1.p6.performance_runner import run_performance_evidence
from tests.test_trip_check_p6_performance_runner import (
    _browser_report,
    _g1_receipt,
    _scenario,
    _spec,
)


def _write(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


async def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    spec_path, spec = _spec(tmp_path)
    evidence = tmp_path / "evidence"
    local_root = evidence / "g5" / "local"
    local_root.mkdir(parents=True)
    browser_report = _browser_report(tmp_path)
    shutil.copyfile(browser_report, local_root / "playwright-report.json")
    local: dict[str, object] = {
        "schema_version": "trip-check-p6-local-browser-receipt-v1",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS",
        "evidence_level": "browser_local",
        "browser_report_sha256": file_sha256(local_root / "playwright-report.json"),
        "test_counts": {"expected": 6, "unexpected": 0, "flaky": 0, "skipped": 0},
        "test_titles_sha256": "2" * 64,
        "coverage": list(REQUIRED_COVERAGE),
        "command": {"returncode": 0, "stdout_sha256": "3" * 64, "stderr_sha256": "4" * 64},
        "human_evidence": False,
    }
    local["receipt_hash"] = digest(local)
    _write(local_root / "local_browser_receipt.json", local)
    g1_path = _g1_receipt(tmp_path, spec, ocr_p95_ms=11_000)
    (evidence / "g1").mkdir(parents=True)
    shutil.copyfile(g1_path, evidence / "g1" / "g1_receipt.json")
    await run_performance_evidence(
        candidate_run_spec_path=spec_path,
        browser_report_path=local_root / "playwright-report.json",
        g1_receipt_path=evidence / "g1" / "g1_receipt.json",
        output_root=evidence / "g5" / "performance",
        repo_root=Path(__file__).parents[2],
        formal=False,
        scenario_runner=_scenario(250),
    )
    g2: dict[str, object] = {
        "schema_version": "trip-check-p6-gate-receipt-v1",
        "gate": "g2",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS",
        "evidence_level": "postgresql_integration",
        "checks_total": 4,
        "checks_passed": 4,
        "failure_count": 0,
        "metrics": {
            "migration_failure_count": 0,
            "transaction_failure_count": 0,
            "restart_readback_failure_count": 0,
            "concurrency_failure_count": 0,
        },
    }
    g2["receipt_hash"] = digest(g2)
    _write(evidence / "g2" / "g2_receipt.json", g2)
    public_root = evidence / "g5" / "public"
    for kind, target in (("health", "/health"), ("e2e", "trip_check_full_chain")):
        public: dict[str, object] = {
            "schema_version": "trip-check-p6-public-receipt-v1",
            "kind": kind,
            "subject_commit": spec["subject_commit"],
            "run_spec_hash": spec["run_spec_hash"],
            "base_url": "https://www.breezetravel.cn",
            "target": target,
            "http_status": 200,
            "response_body_sha256": "5" * 64,
            "status": "PASS",
            "controlled_snapshot": True,
            "observed_at": "2026-08-26T00:00:00Z",
        }
        public["receipt_hash"] = digest(public)
        name = "public_health_receipt.json" if kind == "health" else "public_e2e_receipt.json"
        _write(public_root / name, public)
    return spec_path, evidence, spec


@pytest.mark.asyncio
async def test_g5_runner_aggregates_all_required_evidence(tmp_path: Path) -> None:
    spec_path, evidence, _ = await _fixture(tmp_path)
    receipt = run_g5_gate(
        candidate_run_spec_path=spec_path,
        output_root=evidence / "g5",
        repo_root=Path(__file__).parents[2],
        formal=False,
        dependency_root=evidence,
    )
    assert receipt["status"] == "PASS"
    assert receipt["metrics"]["performance_threshold_failure_count"] == 0
    assert receipt["metrics"]["public_receipt_count"] == 2


@pytest.mark.asyncio
async def test_g5_runner_rejects_tampered_performance_receipt(tmp_path: Path) -> None:
    spec_path, evidence, _ = await _fixture(tmp_path)
    path = evidence / "g5" / "performance" / "performance_receipt.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["metrics"]["three_image_ocr_p95_ms"] = 12_001
    _write(path, value)
    with pytest.raises(P6ContractError, match="P6_G5_PERFORMANCE_RECEIPT_HASH_MISMATCH"):
        run_g5_gate(
            candidate_run_spec_path=spec_path,
            output_root=evidence / "g5",
            repo_root=Path(__file__).parents[2],
            formal=False,
            dependency_root=evidence,
        )
