"""Regression coverage for the dual-entry local-delivery manifest."""

from __future__ import annotations

import json

from scripts.build_release_manifest import build
from scripts.verify_dual_entry_delivery import verify


def test_manifest_binds_dual_entry_local_delivery_without_human_or_public_claims(tmp_path):
    target = build(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))

    assert payload["schema_version"] == "3.0"
    assert payload["release_status"] == "dual_entry_local_delivery_candidate"
    assert payload["release_approval_granted"] is False
    assert payload["latest_migration"] == "021_atomic_suggestion_undo.sql"
    assert payload["configuration"]["required_migration"] == "021_atomic_suggestion_undo.sql"

    delivery = payload["dual_entry_delivery_evidence"]
    assert delivery["final_plan"]["exists"] is True
    assert delivery["local_delivery_acceptance"]["exists"] is True
    assert delivery["m1_dev_dataset_manifest"]["exists"] is True
    assert delivery["m1_dev_proxy_gate"]["exists"] is True
    assert delivery["m1_dev_evidence_type"] == "synthetic_proxy"
    assert delivery["human_validated"] is False
    assert delivery["publicly_verified"] is False

    release_gates = payload["dual_entry_release_gate_evidence"]
    assert release_gates["dataset_manifest"]["exists"] is True
    assert release_gates["latest_import_http_gate"]["exists"] is True
    assert release_gates["latest_builder_http_gate"]["exists"] is True
    assert release_gates["g5_restart_evidence"]["exists"] is True
    assert release_gates["full_backend_junit"]["exists"] is True
    assert release_gates["g5_restart_status"] == "PASSED"
    assert release_gates["overall_release_decision"] == "REJECT"
    assert release_gates["independent_paired_judge_decision"] == "NOT_RUN"
    assert release_gates["baseline_candidate_promotion_decision"] == "NOT_RUN"
    assert release_gates["external_blind_bundle_provisioned"] is False
    assert release_gates["human_calibration_case_count"] == 0
    assert "HUMAN_CALIBRATION_IS_0_OF_30" in release_gates["release_blockers"]

    commands = payload["dual_entry_local_delivery_verification_commands"]
    assert any("run_m1_dev_proxy_gate" in command for command in commands)
    assert any("test_dual_entry_postgres_integration.py" in command for command in commands)
    assert latest["manifest_reference_kind"] == "absolute_external"
    assert latest["manifest"] == str(target.resolve())


def test_delivery_verifier_accepts_an_externally_generated_manifest(tmp_path):
    build(tmp_path)

    result = verify(tmp_path / "latest.json")

    assert result["status"] == "LOCAL_DELIVERY_EVIDENCE_VALID"
    assert result["latest_migration"] == "021_atomic_suggestion_undo.sql"
    assert result["human_validated"] is False
    assert result["publicly_verified"] is False
    assert result["overall_release_decision"] == "REJECT"
    assert result["release_blockers"]
