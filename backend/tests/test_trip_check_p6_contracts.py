from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from copy import deepcopy
from pathlib import Path, PureWindowsPath

import pytest

from evals.trip_check_v1.p6 import contracts_v1

from evals.trip_check_v1.p6.contracts_v1 import (
    P5_GATE_MANIFEST_HASH,
    P6ContractError,
    canonical_bytes,
    candidate_final_disclosure_valid,
    candidate_gate_eligible,
    digest,
    validate_candidate_gate_decision,
    validate_candidate_gate_readback,
    validate_candidate_gate_receipt,
    validate_candidate_evidence,
    validate_candidate_run_spec,
    validate_final_candidate_evidence,
    validate_final_disclosure_readback,
    validate_release_manifest,
    validate_schemas,
)


SUBJECT = "a" * 40
HEX64 = "b" * 64
PROOF_LEVELS = {
    "g0": "repository_contract",
    "g1": "real_authorized_ocr",
    "g2": "postgresql_integration",
    "g3": "controlled_snapshot",
    "g4": "live_provider",
    "g5": "browser_local",
}
SCOPE = {
    "cities": ["北京", "上海", "杭州"],
    "single_city": True,
    "group_size": {"min": 2, "max": 5},
    "trip_days": {"min": 2, "max": 5},
    "input_types": ["TEXT", "SCREENSHOT"],
}


def _hashed(value: dict, field: str) -> dict:
    payload = deepcopy(value)
    payload[field] = digest(payload)
    return payload


def _run_spec(evidence_root: str | None = None) -> dict:
    return _hashed({
        "schema_version": "trip-check-p6-candidate-run-spec-v1",
        "subject_commit": SUBJECT,
        "upstream_ref": "origin/codex/trip-check-p6-candidate-evidence",
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
        "p5_gate_manifest_hash": P5_GATE_MANIFEST_HASH,
        "scope": SCOPE,
        "bindings": {
            "config_sha256": HEX64,
            "ocr_dataset_manifest_sha256": "c" * 64,
            "model_manifest_sha256": "d" * 64,
            "rule_manifest_sha256": "e" * 64,
            "snapshot_manifest_sha256": "f" * 64,
            "migration_manifest_sha256": "1" * 64,
        },
        "provider_live_matrix": {
            "max_calls": 18,
            "amap_route_calls": 12,
            "qweather_forecast_calls": 3,
            "qweather_alert_calls": 3,
            "retry_budget": 0,
            "fixture_fallback_required_zero": True,
        },
        "database": {
            "engine": "postgresql",
            "required_migration": "025_miniapp_identity_and_upload_batches.sql",
            "isolated": True,
            "migration_hash_readback_required": True,
        },
        "public_candidate": {
            "base_url": "https://www.breezetravel.cn",
            "controlled_snapshot_only": True,
            "health_path": "/health",
            "evidence_path": "/api/evidence/latest",
        },
        "evidence_root": evidence_root or (
            f"D:/munto/code/claudeProject/agentTravel-p6-artifacts/p6-candidate/{SUBJECT}"
        ),
        "human_evidence": False,
    }, "run_spec_hash")


def _evidence(status: str = "PASS") -> dict:
    return {
        "schema_version": "trip-check-p6-candidate-evidence-v1",
        "subject_commit": SUBJECT,
        "scope": SCOPE,
        "gates": {f"g{index}": status for index in range(7)},
        "evidence_levels": {
            "controlled_snapshot": "PASS",
            "live_provider": "PASS",
            "public_e2e": "PASS",
            "automated_proxy_judge": "PASS",
        },
        "public_e2e": {
            "status": "PASS",
            "url": "https://www.breezetravel.cn",
            "health_status": "PASS",
            "controlled_snapshot": True,
        },
        "known_gaps": ["HUMAN_EVIDENCE_NOT_RUN"],
        "human_evidence": False,
        "manifest_hash": HEX64,
        "candidate_gate_receipt_hash": "e" * 64 if status == "PASS" else None,
        "candidate_gate_status": status,
    }


def _pre_gate_evidence() -> dict:
    payload = _evidence("NOT_RUN")
    payload["gates"] = {f"g{index}": "PASS" for index in range(6)} | {"g6": "NOT_RUN"}
    payload["evidence_levels"] = {
        "controlled_snapshot": "PASS",
        "live_provider": "PASS",
        "public_e2e": "PASS",
        "automated_proxy_judge": "PASS",
    }
    payload["public_e2e"]["status"] = "PASS"
    payload["public_e2e"]["health_status"] = "PASS"
    return payload


def _manifest(status: str = "PASS") -> dict:
    gates = {
        f"g{index}": {
            "status": status,
            "receipt_sha256": f"{index + 1:x}" * 64,
            "evidence_level": PROOF_LEVELS[f"g{index}"],
        }
        for index in range(6)
    }
    return _hashed({
        "schema_version": "trip-check-p6-release-manifest-v1",
        "subject_commit": SUBJECT,
        "upstream_ref": "origin/codex/trip-check-p6-candidate-evidence",
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
        "candidate_run_spec_hash": _run_spec()["run_spec_hash"],
        "p5_gate_manifest_hash": P5_GATE_MANIFEST_HASH,
        "scope": SCOPE,
        "gates": gates,
        "artifacts": [
            {
                "logical_name": f"g{index}_receipt",
                "path": f"g{index}/receipt.json",
                "sha256": f"{index + 1:x}" * 64,
                "size_bytes": 100 + index,
                "evidence_level": PROOF_LEVELS[f"g{index}"],
            }
            for index in range(6)
        ] + [
            {
                "logical_name": "public_health_receipt",
                "path": "g5/public_health_receipt.json",
                "sha256": "8" * 64,
                "size_bytes": 208,
                "evidence_level": "public_e2e",
            },
            {
                "logical_name": "public_e2e_receipt",
                "path": "g5/public_e2e_receipt.json",
                "sha256": "9" * 64,
                "size_bytes": 209,
                "evidence_level": "public_e2e",
            },
        ],
        "public_e2e": {
            "status": "PASS",
            "url": "https://www.breezetravel.cn",
            "health_receipt_sha256": "8" * 64,
            "e2e_receipt_sha256": "9" * 64,
            "controlled_snapshot": True,
        },
        "known_gaps": ["HUMAN_EVIDENCE_NOT_RUN"],
        "human_evidence": False,
        "read_only_mount": True,
        "released_at": "2026-08-25T00:00:00Z",
    }, "manifest_hash")


def _materialize_manifest(tmp_path: Path, status: str = "PASS", run_spec: dict | None = None) -> dict:
    payload = _manifest(status)
    bound_spec = run_spec or _run_spec()
    payload["candidate_run_spec_hash"] = bound_spec["run_spec_hash"]
    metrics = {
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
    }
    by_name = {}
    for artifact in payload["artifacts"]:
        path = tmp_path / artifact["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        logical_name = artifact["logical_name"]
        if logical_name.startswith("g"):
            gate = logical_name[:2]
            receipt = _hashed({
                "schema_version": "trip-check-p6-gate-receipt-v1",
                "gate": gate,
                "subject_commit": SUBJECT,
                "run_spec_hash": bound_spec["run_spec_hash"],
                "status": "PASS",
                "evidence_level": PROOF_LEVELS[gate],
                "checks_total": 10,
                "checks_passed": 10,
                "failure_count": 0,
                "metrics": metrics[gate],
            }, "receipt_hash")
        else:
            kind = "health" if logical_name == "public_health_receipt" else "e2e"
            receipt = _hashed({
                "schema_version": "trip-check-p6-public-receipt-v1",
                "kind": kind,
                "subject_commit": SUBJECT,
                "run_spec_hash": bound_spec["run_spec_hash"],
                "base_url": "https://www.breezetravel.cn",
                "target": "/health" if kind == "health" else "trip_check_full_chain",
                "http_status": 200,
                "response_body_sha256": "a" * 64,
                "status": "PASS",
                "controlled_snapshot": True,
                "observed_at": "2026-08-25T00:00:30Z",
            }, "receipt_hash")
        content = canonical_bytes(receipt)
        path.write_bytes(content)
        artifact["size_bytes"] = len(content)
        artifact["sha256"] = hashlib.sha256(content).hexdigest()
        by_name[artifact["logical_name"]] = artifact
    for gate in payload["gates"]:
        payload["gates"][gate]["receipt_sha256"] = by_name[f"{gate}_receipt"]["sha256"]
    payload["public_e2e"]["health_receipt_sha256"] = by_name["public_health_receipt"]["sha256"]
    payload["public_e2e"]["e2e_receipt_sha256"] = by_name["public_e2e_receipt"]["sha256"]
    payload["manifest_hash"] = digest({key: item for key, item in payload.items() if key != "manifest_hash"})
    return payload


def _readback(evidence: dict, manifest: dict) -> dict:
    return _hashed({
        "schema_version": "trip-check-p6-candidate-gate-readback-v1",
        "subject_commit": SUBJECT,
        "manifest_hash": manifest["manifest_hash"],
        "candidate_evidence_sha256": digest(evidence),
        "url": "https://www.breezetravel.cn",
        "health_route": "/health",
        "evidence_route": "/api/evidence/latest",
        "health_http_status": 200,
        "evidence_http_status": 200,
        "health_response_body_sha256": "c" * 64,
        "evidence_response_body_sha256": "d" * 64,
        "health_status": "PASS",
        "evidence_status": "PASS",
        "read_only_mount": True,
        "observed_at": "2026-08-25T00:01:00Z",
    }, "receipt_hash")


def _candidate_gate_receipt(evidence: dict, manifest: dict, readback: dict) -> dict:
    return _hashed({
        "schema_version": "trip-check-p6-candidate-gate-receipt-v1",
        "subject_commit": SUBJECT,
        "upstream_ref": "origin/codex/trip-check-p6-candidate-evidence",
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
        "run_spec_hash": manifest["candidate_run_spec_hash"],
        "manifest_hash": manifest["manifest_hash"],
        "pre_gate_evidence_sha256": digest(evidence),
        "pre_gate_evidence_response_body_sha256": readback["evidence_response_body_sha256"],
        "pre_gate_health_response_body_sha256": readback["health_response_body_sha256"],
        "g6_readback_receipt_hash": readback["receipt_hash"],
        "decision": "PASS",
        "decided_at": "2026-08-25T00:02:00Z",
    }, "receipt_hash")


def _final_disclosure(final_evidence: dict, manifest: dict, gate_receipt: dict) -> dict:
    return _hashed({
        "schema_version": "trip-check-p6-final-disclosure-readback-v1",
        "subject_commit": SUBJECT,
        "manifest_hash": manifest["manifest_hash"],
        "candidate_gate_receipt_hash": gate_receipt["receipt_hash"],
        "final_evidence_sha256": digest(final_evidence),
        "url": "https://www.breezetravel.cn",
        "health_route": "/health",
        "evidence_route": "/api/evidence/latest",
        "health_http_status": 200,
        "evidence_http_status": 200,
        "health_response_body_sha256": "c" * 64,
        "evidence_response_body_sha256": "f" * 64,
        "observed_at": "2026-08-25T00:03:00Z",
    }, "receipt_hash")


def _repo_state() -> dict:
    return {
        "subject_commit": SUBJECT,
        "upstream_ref": "origin/codex/trip-check-p6-candidate-evidence",
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
    }


def _run_spec_for_root(monkeypatch, root: Path) -> dict:
    monkeypatch.setattr(contracts_v1, "P6_EVIDENCE_ROOT_PARENT", PureWindowsPath(str(root.parent)))
    monkeypatch.setattr(contracts_v1, "read_actual_repo_state", lambda _repo_root: _repo_state())
    return _run_spec(str(root))


def _patch_public_readback(monkeypatch, evidence: dict, readback: dict) -> None:
    monkeypatch.setattr(contracts_v1, "read_public_candidate", lambda _base_url: {
        "health_http_status": readback["health_http_status"],
        "evidence_http_status": readback["evidence_http_status"],
        "health_response_body_sha256": readback["health_response_body_sha256"],
        "evidence_response_body_sha256": readback["evidence_response_body_sha256"],
        "candidate_evidence": deepcopy(evidence),
    })


def _candidate_root(tmp_path: Path) -> Path:
    root = tmp_path / "p6-candidate" / SUBJECT
    root.mkdir(parents=True)
    return root


def test_schemas_and_valid_contracts_pass():
    assert set(validate_schemas()) == {
        "candidate_run_spec", "candidate_evidence", "release_manifest", "candidate_gate_readback",
        "gate_receipt", "public_receipt", "candidate_gate_receipt",
        "final_disclosure_readback", "real_ocr_dataset_manifest",
    }
    assert validate_candidate_run_spec(_run_spec())["provider_live_matrix"]["max_calls"] == 18
    assert validate_candidate_evidence(_evidence())["human_evidence"] is False
    assert validate_release_manifest(_manifest())["read_only_mount"] is True
    evidence = _evidence()
    manifest = _manifest()
    evidence["manifest_hash"] = manifest["manifest_hash"]
    assert validate_candidate_gate_readback(_readback(evidence, manifest))["read_only_mount"] is True
    pre_gate = _pre_gate_evidence()
    pre_gate["manifest_hash"] = manifest["manifest_hash"]
    readback = _readback(pre_gate, manifest)
    receipt = _candidate_gate_receipt(pre_gate, manifest, readback)
    assert validate_candidate_gate_receipt(receipt)["decision"] == "PASS"
    assert validate_candidate_gate_decision(receipt, pre_gate, manifest, readback)["decision"] == "PASS"
    final_evidence = deepcopy(pre_gate)
    final_evidence["gates"]["g6"] = "PASS"
    final_evidence["candidate_gate_status"] = "PASS"
    final_evidence["candidate_gate_receipt_hash"] = receipt["receipt_hash"]
    assert validate_final_candidate_evidence(
        final_evidence, pre_gate, manifest, receipt, readback,
    )["candidate_gate_status"] == "PASS"
    assert validate_final_disclosure_readback(
        _final_disclosure(final_evidence, manifest, receipt)
    )["evidence_http_status"] == 200
    hidden_gap = deepcopy(final_evidence)
    hidden_gap["known_gaps"] = ["HUMAN_EVIDENCE_NOT_RUN", "FINAL_ONLY_GAP"]
    with pytest.raises(P6ContractError, match="P6_FINAL_CANDIDATE_EVIDENCE_BINDING_INVALID"):
        validate_final_candidate_evidence(hidden_gap, pre_gate, manifest, receipt, readback)
    forged_receipt = deepcopy(receipt)
    forged_receipt["pre_gate_evidence_sha256"] = "0" * 64
    forged_receipt["g6_readback_receipt_hash"] = "1" * 64
    forged_receipt["decided_at"] = "2000-01-01T00:00:00Z"
    forged_receipt["receipt_hash"] = digest({
        key: item for key, item in forged_receipt.items() if key != "receipt_hash"
    })
    with pytest.raises(P6ContractError, match="P6_CANDIDATE_GATE_DECISION_BINDING_INVALID"):
        validate_candidate_gate_decision(forged_receipt, pre_gate, manifest, readback)
    not_run_manifest = _manifest("NOT_RUN")
    not_run_pre_gate = _pre_gate_evidence()
    not_run_pre_gate["manifest_hash"] = not_run_manifest["manifest_hash"]
    not_run_readback = _readback(not_run_pre_gate, not_run_manifest)
    not_run_receipt = _candidate_gate_receipt(
        not_run_pre_gate, not_run_manifest, not_run_readback,
    )
    with pytest.raises(P6ContractError, match="P6_CANDIDATE_GATE_DECISION_BINDING_INVALID"):
        validate_candidate_gate_decision(
            not_run_receipt, not_run_pre_gate, not_run_manifest, not_run_readback,
        )


@pytest.mark.parametrize("mutation,reason", [
    (lambda value: value.update(dirty_tree=True), "P6_CANDIDATE_RUN_SPEC_SCHEMA_INVALID"),
    (lambda value: value.update(upstream_commit="f" * 40), "P6_CANDIDATE_RUN_SPEC_REPO_BINDING_INVALID"),
    (lambda value: value.update(p5_gate_manifest_hash="f" * 64), "P6_CANDIDATE_RUN_SPEC_SCHEMA_INVALID"),
    (lambda value: value["provider_live_matrix"].update(max_calls=19), "P6_CANDIDATE_RUN_SPEC_SCHEMA_INVALID"),
    (lambda value: value.update(unexpected=True), "P6_CANDIDATE_RUN_SPEC_SCHEMA_INVALID"),
])
def test_run_spec_rejects_contract_drift(mutation, reason):
    payload = _run_spec()
    mutation(payload)
    payload["run_spec_hash"] = digest({key: item for key, item in payload.items() if key != "run_spec_hash"})
    with pytest.raises(P6ContractError, match=reason):
        validate_candidate_run_spec(payload)


def test_run_spec_rejects_self_hash_mismatch():
    payload = _run_spec()
    payload["bindings"]["config_sha256"] = "0" * 64
    with pytest.raises(P6ContractError, match="P6_CANDIDATE_RUN_SPEC_HASH_MISMATCH"):
        validate_candidate_run_spec(payload)


def test_candidate_evidence_cannot_claim_pass_before_all_gates_and_public_e2e():
    payload = _evidence()
    payload["gates"]["g4"] = "BLOCKED_EXTERNAL"
    with pytest.raises(P6ContractError, match="P6_CANDIDATE_EVIDENCE_PREMATURE_PASS"):
        validate_candidate_evidence(payload)


def test_candidate_evidence_cannot_hide_human_gap_or_blocked_evidence_level():
    payload = _evidence()
    payload["known_gaps"] = ["SOME_OTHER_GAP"]
    with pytest.raises(P6ContractError, match="P6_HUMAN_EVIDENCE_GAP_MISSING"):
        validate_candidate_evidence(payload)
    payload = _evidence()
    payload["evidence_levels"]["live_provider"] = "BLOCKED_EXTERNAL"
    with pytest.raises(P6ContractError, match="P6_CANDIDATE_EVIDENCE_PREMATURE_PASS"):
        validate_candidate_evidence(payload)


def test_release_manifest_rejects_hash_and_unsafe_paths():
    payload = _manifest()
    payload["artifacts"][0]["path"] = "../secret.json"
    payload["manifest_hash"] = digest({key: item for key, item in payload.items() if key != "manifest_hash"})
    with pytest.raises(P6ContractError, match="P6_RELEASE_ARTIFACT_PATH_INVALID"):
        validate_release_manifest(payload)
    payload = _manifest()
    payload["artifacts"][0]["path"] = "C:\\private\\receipt.json"
    payload["manifest_hash"] = digest({key: item for key, item in payload.items() if key != "manifest_hash"})
    with pytest.raises(P6ContractError, match="P6_RELEASE_ARTIFACT_PATH_INVALID"):
        validate_release_manifest(payload)


def test_release_manifest_rejects_duplicate_or_unbound_gate_receipts():
    payload = _manifest()
    payload["artifacts"][1]["logical_name"] = payload["artifacts"][0]["logical_name"]
    payload["manifest_hash"] = digest({key: item for key, item in payload.items() if key != "manifest_hash"})
    with pytest.raises(P6ContractError, match="P6_RELEASE_ARTIFACT_DUPLICATE"):
        validate_release_manifest(payload)
    payload = _manifest()
    payload["gates"]["g0"]["receipt_sha256"] = "f" * 64
    payload["manifest_hash"] = digest({key: item for key, item in payload.items() if key != "manifest_hash"})
    with pytest.raises(P6ContractError, match="P6_RELEASE_GATE_RECEIPT_UNBOUND"):
        validate_release_manifest(payload)
    payload = _manifest()
    payload["public_e2e"]["health_receipt_sha256"] = "f" * 64
    payload["manifest_hash"] = digest({key: item for key, item in payload.items() if key != "manifest_hash"})
    with pytest.raises(P6ContractError, match="P6_PUBLIC_E2E_RECEIPT_UNBOUND"):
        validate_release_manifest(payload)
    payload = _manifest()
    payload["manifest_hash"] = "0" * 64
    with pytest.raises(P6ContractError, match="P6_RELEASE_MANIFEST_HASH_MISMATCH"):
        validate_release_manifest(payload)


def test_candidate_gate_requires_same_manifest_and_every_gate_pass(tmp_path, monkeypatch):
    artifact_root = _candidate_root(tmp_path)
    evidence = _pre_gate_evidence()
    run_spec = _run_spec_for_root(monkeypatch, artifact_root)
    manifest = _materialize_manifest(artifact_root, run_spec=run_spec)
    evidence["manifest_hash"] = manifest["manifest_hash"]
    readback = _readback(evidence, manifest)
    _patch_public_readback(monkeypatch, evidence, readback)
    assert candidate_gate_eligible(
        evidence, manifest, run_spec, readback, tmp_path,
    ) is True
    blocked = _evidence("BLOCKED")
    blocked["manifest_hash"] = manifest["manifest_hash"]
    blocked_readback = _readback(blocked, manifest)
    _patch_public_readback(monkeypatch, blocked, blocked_readback)
    assert candidate_gate_eligible(
        blocked, manifest, run_spec, blocked_readback, tmp_path,
    ) is False
    mismatched = deepcopy(evidence)
    mismatched["manifest_hash"] = "0" * 64
    mismatched_readback = _readback(mismatched, manifest)
    _patch_public_readback(monkeypatch, mismatched, mismatched_readback)
    assert candidate_gate_eligible(
        mismatched, manifest, run_spec, mismatched_readback, tmp_path,
    ) is False


def test_candidate_pass_requires_fresh_final_public_disclosure(tmp_path, monkeypatch):
    artifact_root = _candidate_root(tmp_path)
    run_spec = _run_spec_for_root(monkeypatch, artifact_root)
    manifest = _materialize_manifest(artifact_root, run_spec=run_spec)
    pre_gate = _pre_gate_evidence()
    pre_gate["manifest_hash"] = manifest["manifest_hash"]
    readback = _readback(pre_gate, manifest)
    gate_receipt = _candidate_gate_receipt(pre_gate, manifest, readback)
    final_evidence = deepcopy(pre_gate)
    final_evidence["gates"]["g6"] = "PASS"
    final_evidence["candidate_gate_status"] = "PASS"
    final_evidence["candidate_gate_receipt_hash"] = gate_receipt["receipt_hash"]
    disclosure = _final_disclosure(final_evidence, manifest, gate_receipt)
    monkeypatch.setattr(contracts_v1, "read_public_candidate", lambda _base_url: {
        "health_http_status": 200,
        "evidence_http_status": 200,
        "health_response_body_sha256": disclosure["health_response_body_sha256"],
        "evidence_response_body_sha256": disclosure["evidence_response_body_sha256"],
        "candidate_evidence": deepcopy(final_evidence),
    })
    assert candidate_final_disclosure_valid(
        final_evidence,
        pre_gate,
        manifest,
        run_spec,
        gate_receipt,
        readback,
        disclosure,
        tmp_path,
    ) is True
    mismatched_manifest = deepcopy(manifest)
    mismatched_manifest["candidate_run_spec_hash"] = "0" * 64
    mismatched_manifest["manifest_hash"] = digest({
        key: item for key, item in mismatched_manifest.items() if key != "manifest_hash"
    })
    mismatched_pre = deepcopy(pre_gate)
    mismatched_pre["manifest_hash"] = mismatched_manifest["manifest_hash"]
    mismatched_readback = _readback(mismatched_pre, mismatched_manifest)
    mismatched_gate_receipt = _candidate_gate_receipt(
        mismatched_pre, mismatched_manifest, mismatched_readback,
    )
    mismatched_final = deepcopy(mismatched_pre)
    mismatched_final["gates"]["g6"] = "PASS"
    mismatched_final["candidate_gate_status"] = "PASS"
    mismatched_final["candidate_gate_receipt_hash"] = mismatched_gate_receipt["receipt_hash"]
    mismatched_disclosure = _final_disclosure(
        mismatched_final, mismatched_manifest, mismatched_gate_receipt,
    )
    monkeypatch.setattr(contracts_v1, "read_public_candidate", lambda _base_url: {
        "health_http_status": 200,
        "evidence_http_status": 200,
        "health_response_body_sha256": mismatched_disclosure["health_response_body_sha256"],
        "evidence_response_body_sha256": mismatched_disclosure["evidence_response_body_sha256"],
        "candidate_evidence": deepcopy(mismatched_final),
    })
    assert candidate_final_disclosure_valid(
        mismatched_final,
        mismatched_pre,
        mismatched_manifest,
        run_spec,
        mismatched_gate_receipt,
        mismatched_readback,
        mismatched_disclosure,
        tmp_path,
    ) is False
    monkeypatch.setattr(contracts_v1, "read_public_candidate", lambda _base_url: {
        "health_http_status": 200,
        "evidence_http_status": 200,
        "health_response_body_sha256": disclosure["health_response_body_sha256"],
        "evidence_response_body_sha256": disclosure["evidence_response_body_sha256"],
        "candidate_evidence": deepcopy(pre_gate),
    })
    assert candidate_final_disclosure_valid(
        final_evidence,
        pre_gate,
        manifest,
        run_spec,
        gate_receipt,
        readback,
        disclosure,
        tmp_path,
    ) is False


def test_candidate_gate_rejects_run_spec_binding_or_public_url_mismatch(tmp_path, monkeypatch):
    artifact_root = _candidate_root(tmp_path)
    evidence = _pre_gate_evidence()
    run_spec = _run_spec_for_root(monkeypatch, artifact_root)
    manifest = _materialize_manifest(artifact_root, run_spec=run_spec)
    evidence["manifest_hash"] = manifest["manifest_hash"]
    manifest["candidate_run_spec_hash"] = "0" * 64
    manifest["manifest_hash"] = digest({key: item for key, item in manifest.items() if key != "manifest_hash"})
    evidence["manifest_hash"] = manifest["manifest_hash"]
    readback = _readback(evidence, manifest)
    _patch_public_readback(monkeypatch, evidence, readback)
    assert candidate_gate_eligible(
        evidence,
        manifest,
        run_spec,
        readback,
        tmp_path,
    ) is False


def test_candidate_gate_rejects_actual_git_mismatch_and_missing_artifact(tmp_path, monkeypatch):
    artifact_root = _candidate_root(tmp_path)
    evidence = _pre_gate_evidence()
    run_spec = _run_spec_for_root(monkeypatch, artifact_root)
    manifest = _materialize_manifest(artifact_root, run_spec=run_spec)
    evidence["manifest_hash"] = manifest["manifest_hash"]
    readback = _readback(evidence, manifest)
    _patch_public_readback(monkeypatch, evidence, readback)
    fake_state = _repo_state()
    fake_state["subject_commit"] = "f" * 40
    monkeypatch.setattr(contracts_v1, "read_actual_repo_state", lambda _repo_root: fake_state)
    with pytest.raises(P6ContractError, match="P6_ACTUAL_REPO_BINDING_INVALID"):
        candidate_gate_eligible(evidence, manifest, run_spec, readback, tmp_path)
    monkeypatch.setattr(contracts_v1, "read_actual_repo_state", lambda _repo_root: _repo_state())
    (artifact_root / manifest["artifacts"][0]["path"]).unlink()
    with pytest.raises(P6ContractError, match="P6_RELEASE_ARTIFACT_UNREADABLE"):
        candidate_gate_eligible(evidence, manifest, run_spec, readback, tmp_path)


def test_release_manifest_rejects_swapped_receipts_and_wrong_proof_class():
    payload = _manifest()
    payload["gates"]["g0"]["receipt_sha256"], payload["gates"]["g1"]["receipt_sha256"] = (
        payload["gates"]["g1"]["receipt_sha256"], payload["gates"]["g0"]["receipt_sha256"],
    )
    payload["manifest_hash"] = digest({key: item for key, item in payload.items() if key != "manifest_hash"})
    with pytest.raises(P6ContractError, match="P6_RELEASE_GATE_RECEIPT_UNBOUND"):
        validate_release_manifest(payload)
    payload = _manifest()
    payload["gates"]["g4"]["evidence_level"] = "repository_contract"
    payload["manifest_hash"] = digest({key: item for key, item in payload.items() if key != "manifest_hash"})
    with pytest.raises(P6ContractError, match="P6_RELEASE_GATE_RECEIPT_UNBOUND"):
        validate_release_manifest(payload)


def test_candidate_gate_rejects_hidden_gap_and_readback_drift(tmp_path, monkeypatch):
    artifact_root = _candidate_root(tmp_path)
    evidence = _pre_gate_evidence()
    run_spec = _run_spec_for_root(monkeypatch, artifact_root)
    manifest = _materialize_manifest(artifact_root, run_spec=run_spec)
    manifest["known_gaps"].append("PUBLIC_LIMITATION")
    manifest["manifest_hash"] = digest({key: item for key, item in manifest.items() if key != "manifest_hash"})
    evidence["manifest_hash"] = manifest["manifest_hash"]
    readback = _readback(evidence, manifest)
    _patch_public_readback(monkeypatch, evidence, readback)
    assert candidate_gate_eligible(
        evidence, manifest, run_spec, readback, tmp_path,
    ) is False
    evidence["known_gaps"] = manifest["known_gaps"]
    stale_readback = readback
    _patch_public_readback(monkeypatch, evidence, _readback(evidence, manifest))
    assert candidate_gate_eligible(
        evidence, manifest, run_spec, stale_readback, tmp_path,
    ) is False
    current_readback = _readback(evidence, manifest)
    current_readback["observed_at"] = "2000-01-01T00:00:00Z"
    current_readback["receipt_hash"] = digest({
        key: item for key, item in current_readback.items() if key != "receipt_hash"
    })
    _patch_public_readback(monkeypatch, evidence, current_readback)
    assert candidate_gate_eligible(
        evidence, manifest, run_spec, current_readback, tmp_path,
    ) is False


def test_candidate_gate_parses_gate_receipt_semantics(tmp_path, monkeypatch):
    artifact_root = _candidate_root(tmp_path)
    run_spec = _run_spec_for_root(monkeypatch, artifact_root)
    manifest = _materialize_manifest(artifact_root, run_spec=run_spec)
    artifact = next(item for item in manifest["artifacts"] if item["logical_name"] == "g4_receipt")
    receipt_path = artifact_root / artifact["path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["metrics"]["fixture_fallback_count"] = 1
    receipt["receipt_hash"] = digest({key: item for key, item in receipt.items() if key != "receipt_hash"})
    content = canonical_bytes(receipt)
    receipt_path.write_bytes(content)
    artifact["sha256"] = hashlib.sha256(content).hexdigest()
    artifact["size_bytes"] = len(content)
    manifest["gates"]["g4"]["receipt_sha256"] = artifact["sha256"]
    manifest["manifest_hash"] = digest({key: item for key, item in manifest.items() if key != "manifest_hash"})
    evidence = _pre_gate_evidence()
    evidence["manifest_hash"] = manifest["manifest_hash"]
    readback = _readback(evidence, manifest)
    _patch_public_readback(monkeypatch, evidence, readback)
    with pytest.raises(P6ContractError, match="P6_GATE_RECEIPT_METRICS_INVALID"):
        candidate_gate_eligible(
            evidence, manifest, run_spec, readback, tmp_path,
        )


def test_public_origin_rejects_query_fragment_and_private_hosts():
    for url in (
        "https://www.breezetravel.cn?token=secret",
        "https://www.breezetravel.cn/#fragment",
        "https://127.0.0.1",
        "https://localhost",
        "https://internal.local",
        "https://intranet",
        "https://metadata.google.internal",
    ):
        payload = _run_spec()
        payload["public_candidate"]["base_url"] = url
        payload["run_spec_hash"] = digest({key: item for key, item in payload.items() if key != "run_spec_hash"})
        with pytest.raises(P6ContractError, match="P6_PUBLIC_BASE_URL_INVALID"):
            validate_candidate_run_spec(payload)


def test_contract_validator_cli_starts_without_pythonpath():
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(backend / "scripts" / "validate_trip_check_p6_contracts.py"), "--schemas-only"],
        cwd=backend.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert json.loads(result.stdout)["status"] == "SCHEMA_VALID"
