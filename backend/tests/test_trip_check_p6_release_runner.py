from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p6.contracts_v1 import P6ContractError, digest
from evals.trip_check_v1.p6 import release_runner
from evals.trip_check_v1.p6.release_runner import (
    DELIVERABLES,
    build_candidate_gate_decision,
    build_pre_gate_release,
    capture_final_disclosure,
    capture_pre_gate_readback,
)
from tests.test_trip_check_p6_contracts import PROOF_LEVELS, _run_spec_for_root


def _write(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _gate_metrics(gate: str) -> dict[str, float]:
    return {
        "g0": {"authority_conflict_count": 0},
        "g1": {
            "authorized_source_count": 60,
            "beijing_count": 20,
            "shanghai_count": 20,
            "hangzhou_count": 20,
            "privacy_leak_count": 0,
            "cleanup_failure_count": 0,
            "key_field_micro_f1": 0.96,
            "must_confirm_recall": 1,
            "work_copy_cleanup_count": 60,
        },
        "g2": {
            "migration_failure_count": 0,
            "transaction_failure_count": 0,
            "restart_readback_failure_count": 0,
            "concurrency_failure_count": 0,
        },
        "g3": {"network_call_count": 0, "replay_mismatch_count": 0},
        "g4": {
            "network_call_count": 18,
            "provider_receipt_count": 18,
            "amap_route_call_count": 12,
            "qweather_forecast_call_count": 3,
            "qweather_alert_call_count": 3,
            "fixture_fallback_count": 0,
            "provider_failure_count": 0,
        },
        "g5": {
            "local_browser_failure_count": 0,
            "public_e2e_failure_count": 0,
            "performance_threshold_failure_count": 0,
            "privacy_failure_count": 0,
        },
    }[gate]


def _fixture(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:  # noqa: ANN001
    evidence_root = tmp_path / "p6-candidate" / ("a" * 40)
    evidence_root.mkdir(parents=True)
    spec = _run_spec_for_root(monkeypatch, evidence_root)
    spec_path = tmp_path / "candidate_run_spec.json"
    _write(spec_path, spec)
    for index in range(6):
        gate = f"g{index}"
        receipt: dict[str, object] = {
            "schema_version": "trip-check-p6-gate-receipt-v1",
            "gate": gate,
            "subject_commit": spec["subject_commit"],
            "run_spec_hash": spec["run_spec_hash"],
            "status": "PASS",
            "evidence_level": PROOF_LEVELS[gate],
            "checks_total": 10,
            "checks_passed": 10,
            "failure_count": 0,
            "metrics": _gate_metrics(gate),
        }
        receipt["receipt_hash"] = digest(receipt)
        _write(evidence_root / gate / f"{gate}_receipt.json", receipt)
    for kind, target, name in (
        ("health", "/health", "public_health_receipt.json"),
        ("e2e", "trip_check_full_chain", "public_e2e_receipt.json"),
    ):
        receipt = {
            "schema_version": "trip-check-p6-public-receipt-v1",
            "kind": kind,
            "subject_commit": spec["subject_commit"],
            "run_spec_hash": spec["run_spec_hash"],
            "base_url": "https://www.breezetravel.cn",
            "target": target,
            "http_status": 200,
            "response_body_sha256": "f" * 64,
            "status": "PASS",
            "controlled_snapshot": True,
            "observed_at": "2026-08-26T00:00:00Z",
        }
        receipt["receipt_hash"] = digest(receipt)
        _write(evidence_root / "g5" / "public" / name, receipt)
    for logical_name, (relative, _) in DELIVERABLES.items():
        path = evidence_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"p6 deliverable {logical_name}".encode())
    return spec_path, evidence_root


def test_release_runner_builds_bound_manifest_and_pre_gate_evidence(tmp_path: Path, monkeypatch) -> None:
    spec_path, evidence_root = _fixture(tmp_path, monkeypatch)
    manifest, evidence = build_pre_gate_release(
        candidate_run_spec_path=spec_path,
        repo_root=Path(__file__).parents[2],
        formal=False,
        released_at="2026-08-26T00:01:00Z",
    )
    assert manifest["read_only_mount"] is True
    assert len(manifest["artifacts"]) == 14
    assert evidence["gates"]["g6"] == "NOT_RUN"
    assert evidence["candidate_gate_status"] == "NOT_RUN"
    assert (evidence_root / "g6" / "release_manifest.json").is_file()
    assert (evidence_root / "g6" / "candidate_evidence_pre_gate.json").is_file()


def test_release_runner_rejects_missing_demo_delivery(tmp_path: Path, monkeypatch) -> None:
    spec_path, evidence_root = _fixture(tmp_path, monkeypatch)
    (evidence_root / DELIVERABLES["demo_90_seconds"][0]).unlink()
    with pytest.raises(P6ContractError, match="P6_G6_ARTIFACT_UNREADABLE"):
        build_pre_gate_release(
            candidate_run_spec_path=spec_path,
            repo_root=Path(__file__).parents[2],
            formal=False,
        )


def test_release_runner_stages_pre_gate_decision_and_final_disclosure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    spec_path, evidence_root = _fixture(tmp_path, monkeypatch)
    _, pre_gate = build_pre_gate_release(
        candidate_run_spec_path=spec_path,
        repo_root=Path(__file__).parents[2],
        formal=False,
        released_at="2026-08-25T00:00:00Z",
    )
    mounted = {"evidence": pre_gate}

    def public_readback(_base_url: str) -> dict[str, object]:
        return {
            "health_http_status": 200,
            "evidence_http_status": 200,
            "health_response_body_sha256": "1" * 64,
            "evidence_response_body_sha256": "2" * 64,
            "candidate_evidence": mounted["evidence"],
        }

    monkeypatch.setattr(release_runner, "read_public_candidate", public_readback)
    monkeypatch.setattr(release_runner, "candidate_gate_eligible", lambda *args: True)
    readback = capture_pre_gate_readback(
        candidate_run_spec_path=spec_path,
        repo_root=Path(__file__).parents[2],
    )
    assert readback["evidence_status"] == "PASS"
    receipt, final = build_candidate_gate_decision(
        candidate_run_spec_path=spec_path,
        repo_root=Path(__file__).parents[2],
    )
    assert receipt["decision"] == "PASS"
    assert final["candidate_gate_status"] == "PASS"
    mounted["evidence"] = final
    monkeypatch.setattr(release_runner, "candidate_final_disclosure_valid", lambda *args: True)
    disclosure = capture_final_disclosure(
        candidate_run_spec_path=spec_path,
        repo_root=Path(__file__).parents[2],
    )
    assert disclosure["candidate_gate_receipt_hash"] == receipt["receipt_hash"]
    assert (evidence_root / "g6" / "final_disclosure_readback.json").is_file()
