from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evals.trip_check_v1.p6.contracts_v1 import P5_GATE_MANIFEST_HASH, P6ContractError, digest
from evals.trip_check_v1.p6.repository_runner import run_repository_gate


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
            "required_migration": "025_miniapp_identity_and_upload_batches.sql",
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


def _runner(*, failed_name: str | None = None):  # noqa: ANN202
    def run(command, **kwargs):  # noqa: ANN001, ANN003, ANN202
        junit = next((item for item in command if item.startswith("--junitxml=")), None)
        if junit:
            path = Path(junit.split("=", 1)[1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '<testsuite tests="100" failures="0" errors="0" skipped="4"></testsuite>',
                encoding="utf-8",
            )
        name = (
            "backend_full_pytest"
            if "pytest" in command
            else "frontend_build"
            if "npm" in Path(command[0]).name
            else "other"
        )
        return subprocess.CompletedProcess(command, 1 if name == failed_name else 0, "ok", "")

    return run


def test_g0_runner_emits_repository_receipt(tmp_path: Path) -> None:
    receipt = run_repository_gate(
        candidate_run_spec_path=_spec(tmp_path),
        output_root=tmp_path / "g0",
        log_root=tmp_path / "logs",
        repo_root=Path(__file__).parents[2],
        formal=False,
        command_runner=_runner(),
    )
    assert receipt["status"] == "PASS"
    assert receipt["metrics"]["backend_test_count"] == 100
    assert receipt["metrics"]["authority_conflict_count"] == 0


def test_g0_runner_rejects_failed_command(tmp_path: Path) -> None:
    with pytest.raises(P6ContractError, match="P6_G0_COMMAND_MATRIX_FAILED"):
        run_repository_gate(
            candidate_run_spec_path=_spec(tmp_path),
            output_root=tmp_path / "g0",
            log_root=tmp_path / "logs",
            repo_root=Path(__file__).parents[2],
            formal=False,
            command_runner=_runner(failed_name="frontend_build"),
        )
