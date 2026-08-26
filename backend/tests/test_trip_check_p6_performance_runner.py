from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p6.contracts_v1 import P5_GATE_MANIFEST_HASH, P6ContractError, digest
from evals.trip_check_v1.p6.local_browser_runner import EXPECTED_TITLES
from evals.trip_check_v1.p6.performance_runner import INTERNAL_SAMPLE_COUNT, run_performance_evidence


def _spec(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    subject = "1" * 40
    value: dict[str, object] = {
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
    return path, value


def _browser_report(tmp_path: Path) -> Path:
    specs = []
    for index, title in enumerate(sorted(EXPECTED_TITLES)):
        annotations = []
        if index < 3:
            annotations = [
                {"type": "p6_first_feedback_ms", "description": str(100 + index)},
                {"type": "p6_parse_confirmation_ui_ms", "description": str(200 + index)},
            ]
        specs.append({
            "title": title,
            "tests": [{"annotations": annotations}],
        })
    value = {
        "config": {
            "metadata": {
                "commit_sha": "1" * 40,
                "evidence_class": "CONTROLLED_BROWSER_FIXTURE",
                "evidence_scope": "P6_G5_LOCAL_CHAIN",
                "live_provider_evidence": False,
                "public_e2e_evidence": False,
                "human_evidence": False,
            }
        },
        "suites": [{"specs": specs, "suites": []}],
        "errors": [],
        "stats": {"expected": 6, "unexpected": 0, "flaky": 0, "skipped": 0},
    }
    path = tmp_path / "playwright-report.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _g1_receipt(
    tmp_path: Path,
    spec: dict[str, object],
    *,
    ocr_p95_ms: float,
    gpu_bound: bool = True,
) -> Path:
    metrics = {
        "authorized_source_count": 60,
        "beijing_count": 20,
        "shanghai_count": 20,
        "hangzhou_count": 20,
        "privacy_leak_count": 0,
        "cleanup_failure_count": 0,
        "key_field_micro_f1": 0.96,
        "must_confirm_recall": 1,
        "work_copy_cleanup_count": 60,
        "ocr_image_sample_count": 60,
        "three_image_batch_sample_count": 20,
        "three_image_ocr_p95_ms": ocr_p95_ms,
        "gpu_runtime_binding_count": int(gpu_bound),
        "gpu_device_count": int(gpu_bound),
        "gpu_compute_capability_major": 8,
        "gpu_compute_capability_minor": 9,
        "cudnn_version_warning_disclosed_count": int(gpu_bound),
    }
    receipt: dict[str, object] = {
        "schema_version": "trip-check-p6-gate-receipt-v1",
        "gate": "g1",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS",
        "evidence_level": "real_authorized_ocr",
        "checks_total": 18,
        "checks_passed": 18,
        "failure_count": 0,
        "metrics": metrics,
    }
    receipt["receipt_hash"] = digest(receipt)
    path = tmp_path / "g1_receipt.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    return path


def _scenario(value: float):  # noqa: ANN202
    async def run(subject_commit: str) -> dict[str, list[float]]:
        assert subject_commit == "1" * 40
        return {
            "parse_confirmation_engine_ms": [value] * INTERNAL_SAMPLE_COUNT,
            "base_report_ms": [value] * INTERNAL_SAMPLE_COUNT,
            "risk_report_ms": [value] * INTERNAL_SAMPLE_COUNT,
        }

    return run


@pytest.mark.asyncio
async def test_performance_runner_emits_pass_receipt(tmp_path: Path) -> None:
    spec_path, spec = _spec(tmp_path)
    receipt = await run_performance_evidence(
        candidate_run_spec_path=spec_path,
        browser_report_path=_browser_report(tmp_path),
        g1_receipt_path=_g1_receipt(tmp_path, spec, ocr_p95_ms=11_000),
        output_root=tmp_path / "performance",
        repo_root=Path(__file__).parents[2],
        formal=False,
        scenario_runner=_scenario(250),
    )
    assert receipt["status"] == "PASS"
    assert receipt["performance_threshold_failure_count"] == 0
    assert receipt["sample_counts"]["three_image_ocr_batches"] == 20


@pytest.mark.asyncio
async def test_performance_runner_records_ocr_threshold_failure(tmp_path: Path) -> None:
    spec_path, spec = _spec(tmp_path)
    receipt = await run_performance_evidence(
        candidate_run_spec_path=spec_path,
        browser_report_path=_browser_report(tmp_path),
        g1_receipt_path=_g1_receipt(tmp_path, spec, ocr_p95_ms=12_001),
        output_root=tmp_path / "performance",
        repo_root=Path(__file__).parents[2],
        formal=False,
        scenario_runner=_scenario(250),
    )
    assert receipt["status"] == "FAIL"
    assert receipt["threshold_failures"] == ["three_image_ocr_p95_ms"]
    assert receipt["performance_threshold_failure_count"] == 1


@pytest.mark.asyncio
async def test_performance_runner_rejects_unbound_cpu_ocr_metrics(tmp_path: Path) -> None:
    spec_path, spec = _spec(tmp_path)
    with pytest.raises(P6ContractError, match="P6_G5_PERFORMANCE_OCR_METRICS_INVALID"):
        await run_performance_evidence(
            candidate_run_spec_path=spec_path,
            browser_report_path=_browser_report(tmp_path),
            g1_receipt_path=_g1_receipt(
                tmp_path,
                spec,
                ocr_p95_ms=2_000,
                gpu_bound=False,
            ),
            output_root=tmp_path / "performance",
            repo_root=Path(__file__).parents[2],
            formal=False,
            scenario_runner=_scenario(250),
        )
