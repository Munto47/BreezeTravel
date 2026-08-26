from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.trip_check.provider_integrity import ProviderCallReceipt
from evals.trip_check_v1.p6.contracts_v1 import P5_GATE_MANIFEST_HASH, P6ContractError, digest
from evals.trip_check_v1.p6.live_provider_runner import (
    CITIES,
    EXPECTED_OPERATION_COUNTS,
    run_live_provider_gate,
)


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


def _settings() -> Settings:
    return Settings(
        runtime_profile="test",
        amap_api_key="controlled-amap-secret",
        qweather_api_key="controlled-qweather-secret",
        qweather_auth_type="apikey",
    )


def _fake_live_runner(*, fallback: bool = False):  # noqa: ANN202
    async def run(*, commit_sha, output, settings, max_live_calls):  # noqa: ANN001, ANN202
        operations = list(EXPECTED_OPERATION_COUNTS)
        cases = []
        for city in CITIES:
            receipts = []
            for operation in operations:
                provider = (
                    "controlled_fixture"
                    if fallback and city == "北京" and operation == "route.walking"
                    else "amap"
                    if operation.startswith("route.")
                    else "qweather"
                    if operation == "weather.daily"
                    else "qweather_alert"
                )
                receipt = ProviderCallReceipt(
                    receipt_id=digest({"city": city, "operation": operation}),
                    provider=provider,
                    operation=operation,
                    execution_mode="live",
                    status="SUCCEEDED",
                    request_hash=digest({"request": city, "operation": operation}),
                    response_hash=digest({"response": city, "operation": operation}),
                    observed_at=datetime.now(timezone.utc),
                    source_url="https://restapi.amap.com/path" if operation.startswith("route.") else "https://devapi.qweather.com/path",
                )
                receipts.append(receipt.model_dump(mode="json"))
            city_root = output / "live" / city
            city_root.mkdir(parents=True, exist_ok=True)
            (city_root / "provider_receipts.json").write_text(
                json.dumps(receipts), encoding="utf-8"
            )
            (city_root / "evidence_observations.json").write_text("[]", encoding="utf-8")
            cases.append({"city": city, "status": "PASS", "receipt_count": 6, "failure_categories": []})
        manifest = {
            "schema_version": "trip-check-p3-live-provider-manifest-v1",
            "subject_commit": commit_sha,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "PASS",
            "query_budget": max_live_calls,
            "actual_network_call_count": 18,
            "actual_receipt_count": 18,
            "hidden_retry_count": 0,
            "cases": cases,
        }
        (output / "live_provider_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return manifest

    return run


def test_g4_runner_accepts_exact_redacted_18_call_matrix(tmp_path: Path) -> None:
    receipt = asyncio.run(
        run_live_provider_gate(
            candidate_run_spec_path=_spec(tmp_path),
            output_root=tmp_path / "g4",
            repo_root=Path(__file__).parents[2],
            formal=False,
            settings=_settings(),
            live_runner=_fake_live_runner(),
        )
    )
    assert receipt["status"] == "PASS"
    assert receipt["metrics"]["network_call_count"] == 18
    assert receipt["metrics"]["fixture_fallback_count"] == 0


def test_g4_runner_rejects_fixture_fallback_receipt(tmp_path: Path) -> None:
    with pytest.raises(P6ContractError, match="P6_G4_PROVIDER_RECEIPTS_INVALID"):
        asyncio.run(
            run_live_provider_gate(
                candidate_run_spec_path=_spec(tmp_path),
                output_root=tmp_path / "g4",
                repo_root=Path(__file__).parents[2],
                formal=False,
                settings=_settings(),
                live_runner=_fake_live_runner(fallback=True),
            )
        )


def test_g4_runner_rejects_missing_credentials_without_calling_runner(tmp_path: Path) -> None:
    called = False

    async def unexpected(**kwargs):  # noqa: ANN003, ANN202
        nonlocal called
        called = True
        return {}

    with pytest.raises(P6ContractError, match="P6_G4_CREDENTIALS_MISSING"):
        asyncio.run(
            run_live_provider_gate(
                candidate_run_spec_path=_spec(tmp_path),
                output_root=tmp_path / "g4",
                repo_root=Path(__file__).parents[2],
                formal=False,
                settings=Settings(
                    runtime_profile="test",
                    amap_api_key="",
                    qweather_api_key="",
                    qweather_private_key="",
                    qweather_key_id="",
                    qweather_project_id="",
                ),
                live_runner=unexpected,
            )
        )
    assert called is False
