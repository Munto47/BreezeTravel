from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evals.trip_check_v1.p6.contracts_v1 import P5_GATE_MANIFEST_HASH, P6ContractError, digest
from evals.trip_check_v1.p6.local_browser_runner import EXPECTED_TITLES, run_local_browser_evidence


def _spec(tmp_path: Path) -> Path:
    subject = "1" * 40
    value = {
        "schema_version": "trip-check-p6-candidate-run-spec-v1",
        "subject_commit": subject,
        "upstream_ref": "origin/codex/trip-check-p6-candidate-evidence",
        "upstream_commit": subject,
        "dirty_tree": False,
        "p5_gate_manifest_hash": P5_GATE_MANIFEST_HASH,
        "scope": {
            "cities": ["北京", "上海", "杭州"],
            "single_city": True,
            "trip_days": {"min": 2, "max": 5},
            "group_size": {"min": 2, "max": 5},
            "input_types": ["TEXT", "SCREENSHOT"],
        },
        "bindings": {
            "config_sha256": "2" * 64,
            "ocr_dataset_manifest_sha256": "3" * 64,
            "model_manifest_sha256": "4" * 64,
            "rule_manifest_sha256": "5" * 64,
            "snapshot_manifest_sha256": "6" * 64,
            "migration_manifest_sha256": "7" * 64,
        },
        "provider_live_matrix": {
            "amap_route_calls": 12,
            "qweather_forecast_calls": 3,
            "qweather_alert_calls": 3,
            "max_calls": 18,
            "retry_budget": 0,
            "fixture_fallback_required_zero": True,
        },
        "database": {
            "engine": "postgresql",
            "isolated": True,
            "required_migration": "024_advice_bundles.sql",
            "migration_hash_readback_required": True,
        },
        "public_candidate": {
            "base_url": "https://www.breezetravel.cn",
            "controlled_snapshot_only": True,
            "health_path": "/health",
            "evidence_path": "/api/evidence/latest",
        },
        "evidence_root": (
            "D:\\munto\\code\\claudeProject\\agentTravel-p6-artifacts"
            "\\p6-candidate\\" + subject
        ),
        "human_evidence": False,
    }
    value["run_spec_hash"] = digest(value)
    path = tmp_path / "candidate_run_spec.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _report(*, missing_title: bool = False, wrong_commit: bool = False) -> dict[str, object]:
    titles = sorted(EXPECTED_TITLES)
    if missing_title:
        titles.pop()
    return {
        "config": {
            "metadata": {
                "commit_sha": "2" * 40 if wrong_commit else "1" * 40,
                "evidence_class": "CONTROLLED_BROWSER_FIXTURE",
                "evidence_scope": "P6_G5_LOCAL_CHAIN",
                "live_provider_evidence": False,
                "public_e2e_evidence": False,
                "human_evidence": False,
            }
        },
        "suites": [{"specs": [{"title": title} for title in titles], "suites": []}],
        "errors": [],
        "stats": {
            "expected": len(titles),
            "unexpected": 0,
            "flaky": 0,
            "skipped": 0,
        },
    }


def _runner(report: dict[str, object], *, returncode: int = 0):  # noqa: ANN202
    def run(command, **kwargs):  # noqa: ANN001, ANN003, ANN202
        report_path = Path(kwargs["env"]["P6_G5_PLAYWRIGHT_JSON"])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode, "browser stdout", "")

    return run


def test_local_browser_runner_emits_bound_receipt(tmp_path: Path) -> None:
    receipt = run_local_browser_evidence(
        candidate_run_spec_path=_spec(tmp_path),
        output_root=tmp_path / "g5-local",
        log_root=tmp_path / "logs",
        repo_root=Path(__file__).parents[2],
        formal=False,
        command_runner=_runner(_report()),
    )
    assert receipt["status"] == "PASS"
    assert receipt["test_counts"]["expected"] == 6
    assert "PROVIDER_PARTIAL_FAILURE" in receipt["coverage"]
    assert "PRIVACY_FAIL_CLOSED" in receipt["coverage"]


@pytest.mark.parametrize(
    ("report", "reason"),
    [
        (_report(missing_title=True), "P6_G5_LOCAL_BROWSER_MATRIX_FAILED"),
        (_report(wrong_commit=True), "P6_G5_LOCAL_REPORT_BINDING_INVALID"),
    ],
)
def test_local_browser_runner_rejects_invalid_report(
    tmp_path: Path,
    report: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(P6ContractError, match=reason):
        run_local_browser_evidence(
            candidate_run_spec_path=_spec(tmp_path),
            output_root=tmp_path / "g5-local",
            log_root=tmp_path / "logs",
            repo_root=Path(__file__).parents[2],
            formal=False,
            command_runner=_runner(report),
        )


def test_local_browser_runner_rejects_failed_command(tmp_path: Path) -> None:
    with pytest.raises(P6ContractError, match="P6_G5_LOCAL_BROWSER_MATRIX_FAILED"):
        run_local_browser_evidence(
            candidate_run_spec_path=_spec(tmp_path),
            output_root=tmp_path / "g5-local",
            log_root=tmp_path / "logs",
            repo_root=Path(__file__).parents[2],
            formal=False,
            command_runner=_runner(_report(), returncode=1),
        )
