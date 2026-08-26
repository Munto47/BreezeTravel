"""Regression coverage for the Trip Check V1 in-progress manifest.

The module name is retained to avoid breaking existing targeted test commands.
"""

from __future__ import annotations

import json

from scripts.build_release_manifest import build
from scripts.verify_dual_entry_delivery import verify


def test_manifest_binds_trip_check_authority_without_release_claims(tmp_path):
    target = build(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))

    assert payload["schema_version"] == "4.0"
    assert payload["release_status"] == "trip_check_v1_p3_input_provider_draft"
    assert payload["release_approval_granted"] is False
    assert payload["latest_migration"] == "024_advice_bundles.sql"
    assert payload["configuration"]["required_migration"] == "024_advice_bundles.sql"

    authority = payload["product_authority"]
    for name in (
        "agents",
        "project_charter",
        "trip_check_spec",
        "trip_check_api_contract",
        "portfolio_mission",
        "program",
        "current_goal",
        "roadmap",
        "release_gates",
        "capability_status",
    ):
        assert authority[name]["exists"] is True

    scope = payload["evaluation_scope"]
    assert scope["supported_and_claimed_cities"] == ["北京", "上海", "杭州"]
    assert scope["target_cases_per_city"] == 120
    assert scope["target_total_cases"] == 360
    assert scope["target_splits"] == {
        "pilot": 18,
        "dev": 180,
        "regression": 72,
        "frozen_blind": 90,
    }
    assert scope["dataset_status"] == "pilot_18_revalidated_p2_pass_dev_not_started"

    gates = payload["trip_check_v1_release_gate_evidence"]
    assert gates["overall_release_decision"] == "REJECT"
    assert gates["g1_offline"] == "NOT_RUN"
    assert gates["g4_live_providers"] == "NOT_RUN"
    assert gates["g6_release_manifest"] == "BASELINE_ONLY"
    assert gates["automated_proxy_judge"] == "NOT_RUN"
    assert "TRIP_CHECK_V1_P1_D1_NOT_PASSED" not in gates["release_blockers"]
    assert "TRIP_CHECK_V1_P2_RELIABILITY_NOT_PASSED" not in gates["release_blockers"]
    assert "TRIP_CHECK_V1_P3_INPUT_PROVIDER_NOT_PASSED" in gates["release_blockers"]
    assert gates["p2_reliability_gate"]["exists"] is True
    assert gates["p2_reliability_status"] == "PASS_CONTROLLED_POSTGRES_BROWSER"

    legacy = payload["legacy_dual_entry_delivery_evidence"]
    assert legacy["archived_final_plan"]["exists"] is True
    assert legacy["m1_dev_evidence_type"] == "synthetic_proxy"
    assert legacy["human_validated"] is False
    assert legacy["publicly_verified"] is False

    assert latest["manifest_reference_kind"] == "absolute_external"
    assert latest["manifest"] == str(target.resolve())


def test_verifier_accepts_externally_generated_trip_check_baseline(tmp_path):
    build(tmp_path)

    result = verify(tmp_path / "latest.json")

    assert result["status"] == "TRIP_CHECK_V1_IN_PROGRESS_EVIDENCE_VALID"
    assert result["latest_migration"] == "024_advice_bundles.sql"
    assert result["human_validated"] is False
    assert result["publicly_verified"] is False
    assert result["overall_release_decision"] == "REJECT"
    assert result["release_blockers"]
