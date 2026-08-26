from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.trip_check.provider_integrity import provider_snapshot_sha256
from evals.trip_check_v1.input_provider_integrity_runner import run_snapshot_matrix
from evals.trip_check_v1.p6.contracts_v1 import P5_GATE_MANIFEST_HASH, P6ContractError, digest
from evals.trip_check_v1.p6.snapshot_runner import run_snapshot_gate


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
            "snapshot_manifest_sha256": provider_snapshot_sha256(),
            "migration_manifest_sha256": "6" * 64,
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


def test_g3_runner_replays_two_byte_stable_no_network_rounds(tmp_path: Path) -> None:
    receipt = asyncio.run(
        run_snapshot_gate(
            candidate_run_spec_path=_spec(tmp_path),
            output_root=tmp_path / "g3",
            repo_root=Path(__file__).parents[2],
            formal=False,
        )
    )
    assert receipt["status"] == "PASS"
    assert receipt["metrics"]["network_call_count"] == 0
    assert receipt["metrics"]["snapshot_artifact_count"] == 36
    assert receipt["metrics"]["replay_mismatch_count"] == 0


def test_g3_runner_rejects_internal_network_count(tmp_path: Path) -> None:
    async def bad_runner(**kwargs):  # noqa: ANN003, ANN202
        manifest = await run_snapshot_matrix(**kwargs)
        manifest["network_call_count"] = 1
        return manifest

    with pytest.raises(P6ContractError, match="P6_G3_ROUND_INVALID"):
        asyncio.run(
            run_snapshot_gate(
                candidate_run_spec_path=_spec(tmp_path),
                output_root=tmp_path / "g3",
                repo_root=Path(__file__).parents[2],
                formal=False,
                matrix_runner=bad_runner,
            )
        )


def test_g3_runner_rejects_snapshot_binding_mismatch(tmp_path: Path) -> None:
    spec_path = _spec(tmp_path)
    value = json.loads(spec_path.read_text(encoding="utf-8"))
    value["bindings"]["snapshot_manifest_sha256"] = "f" * 64
    value["run_spec_hash"] = digest({key: item for key, item in value.items() if key != "run_spec_hash"})
    spec_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(P6ContractError, match="P6_G3_SNAPSHOT_BINDING_INVALID"):
        asyncio.run(
            run_snapshot_gate(
                candidate_run_spec_path=spec_path,
                output_root=tmp_path / "g3",
                repo_root=Path(__file__).parents[2],
                formal=False,
            )
        )
