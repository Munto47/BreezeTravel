from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from evals.agent_gate_v1.contracts import (
    BLIND_ERROR_CATEGORY_ORDER,
    AgentGatePassReceipt,
    AutomatedCheckExecution,
    AutomatedProductExecutionManifest,
    SealedAgentBlindReceipt,
)


ROOT = Path(__file__).resolve().parents[2]
COMMIT = "1" * 40
TREE = "2" * 40
SHA = "3" * 64
NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _blind_metrics() -> dict[str, float | int | bool]:
    return {
        "forbidden_content_as_place_count": 0,
        "severe_wrong_auto_match_count": 0,
        "wrong_city_auto_match_count": 0,
        "wrong_category_auto_match_count": 0,
        "auto_match.precision": 1.0,
        "auto_match.denominator": 54,
        "executable_mentions.precision": 1.0,
        "executable_mentions.recall": 1.0,
        "executable_mentions.fp": 0,
        "executable_mentions.fn": 0,
        "day_assignment.f1": 1.0,
        "day_assignment.error_count": 0,
        "role_macro_f1": 1.0,
        "role_classification.error_count": 0,
        "provider_resolution.error_count": 0,
        "evidence_span_validity": 1.0,
        "destination.exact_name_accuracy": 1.0,
        "destination.basis_accuracy": 1.0,
        "destination.explicit_evidence_validity": 1.0,
        "public_projection.forbidden_key_hits": 0,
        "public_projection.full_source_leak_hits": 0,
        "public_api_latency.cards_ready_p95_ms": 6000.0,
        "latency.violation_count": 0,
        "other_aggregated_error_count": 0,
    }


def test_core_automation_pass_uses_clean_checkout_without_oci_claims() -> None:
    execution = AutomatedCheckExecution(
        check_id="backend.ruff",
        argv_sha256=SHA,
        workdir="backend",
        stdout_sha256=SHA,
        stderr_sha256=SHA,
        started_at=NOW,
        completed_at=NOW,
    )
    manifest = AutomatedProductExecutionManifest(
        goal_id="TC-VNEXT-G01-TEXT-CARDS",
        gate_profile="CORE_AGENT_GATE",
        candidate_commit=COMMIT,
        candidate_tree=TREE,
        candidate_config_sha256=SHA,
        candidate_data_sha256=SHA,
        gate_contract_sha256=SHA,
        isolation_mode="FRESH_CLEAN_CHECKOUT",
        network_access=False,
        host_mount_count=0,
        host_pid_namespace=False,
        synthetic_profile=False,
        authority_secret_mount_count=0,
        checks=[execution],
        checks_not_run=[],
        verdict="PASS",
    )
    assert manifest.runner_image_id is None
    with pytest.raises(ValueError, match="fresh clean checkout"):
        manifest.model_copy(
            update={"isolation_mode": "OCI_EPHEMERAL_NO_HOST_MOUNTS"}
        ).__class__.model_validate(
            {
                **manifest.model_dump(mode="json"),
                "isolation_mode": "OCI_EPHEMERAL_NO_HOST_MOUNTS",
            }
        )


def test_core_sealed_receipt_never_requires_or_claims_hardened_custody() -> None:
    receipt = SealedAgentBlindReceipt(
        gate_profile="CORE_AGENT_GATE",
        goal_id="TC-VNEXT-G01-TEXT-CARDS",
        candidate_commit=COMMIT,
        candidate_tree=TREE,
        prompt_sha256=SHA,
        schema_sha256=SHA,
        thresholds_sha256=SHA,
        config_sha256=SHA,
        provider_binding_sha256=SHA,
        scorer_sha256=SHA,
        input_bundle_sha256=SHA,
        prediction_bundle_sha256=SHA,
        scored_case_count=18,
        custodian_task_id="independent-core-blind",
        aggregate_metrics=_blind_metrics(),
        taxonomy_counts={name: 0 for name in BLIND_ERROR_CATEGORY_ORDER},
        error_taxonomy=[],
        required_gate_metrics_passed=True,
        verdict="PASS",
        completed_at=NOW,
    )
    assert receipt.authority_signature is None
    assert receipt.custody_registry_identity_sha256 is None
    with pytest.raises(ValueError, match="cannot claim HARDENED"):
        SealedAgentBlindReceipt.model_validate(
            {
                **receipt.model_dump(mode="json"),
                "authority_policy_sha256": SHA,
            }
        )


def test_core_final_receipt_binds_four_components_without_authority() -> None:
    binding_names = {
        "model",
        "prompt",
        "schema",
        "config",
        "provider",
        "thresholds",
        "dev_validation_scorer",
        "sealed_scorer",
        "review_schema",
        "adjudication_schema",
    }
    receipt = AgentGatePassReceipt(
        gate_profile="CORE_AGENT_GATE",
        goal_sequence=1,
        goal_id="TC-VNEXT-G01-TEXT-CARDS",
        predecessor_goal_id="TC-BP-G00-BLUEPRINT",
        predecessor_completion_commit=COMMIT,
        current_goal_binding_sha256=SHA,
        current_goal_document_sha256=SHA,
        automated_gate_contract_sha256=SHA,
        candidate_commit=COMMIT,
        candidate_tree=TREE,
        candidate_config_sha256=SHA,
        candidate_data_sha256=SHA,
        frozen_binding_sha256={name: SHA for name in binding_names},
        component_receipt_sha256={
            "AUTOMATED_PRODUCT_GATE": SHA,
            "LIVE_PROVIDER_GATE": SHA,
            "MULTI_AGENT_PANEL": SHA,
            "SEALED_AGENT_BLIND": SHA,
        },
        fresh_checkout_root_sha256=SHA,
        remote_name="origin",
        remote_ref="refs/heads/codex/trip-check-product-reset",
        remote_subject=COMMIT,
        remote_tree=TREE,
        verifier_sha256=SHA,
        evidence_levels=[
            "AUTOMATED_TEST",
            "LIVE_PROVIDER_EVIDENCE",
            "MULTI_AGENT_SIMULATED_REVIEW",
            "SEALED_AGENT_BLIND",
        ],
        completed_at=NOW,
    )
    assert receipt.authority_signature is None
    assert receipt.authority_policy_sha256 is None


def test_core_entrypoints_do_not_import_authority_or_custody() -> None:
    sources = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "backend/evals/agent_gate_v1/core_gate.py",
            "backend/scripts/build_agent_gate_pass.py",
        )
    )
    assert "agent_gate_v1.authority" not in sources
    assert "agent_gate_v1.custody" not in sources
