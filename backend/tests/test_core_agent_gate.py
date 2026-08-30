from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evals.agent_gate_v1.contracts import (
    BLIND_ERROR_CATEGORY_ORDER,
    AgentGatePassReceipt,
    AutomatedCheckExecution,
    AutomatedProductExecutionManifest,
    CurrentGoalBinding,
    SealedAgentBlindReceipt,
)
from evals.agent_gate_v1.core_gate import (
    CoreAgentGateError,
    CoreCandidateContext,
    _thresholds_pass,
    verify_core_live_score,
    verify_core_sealed,
)


ROOT = Path(__file__).resolve().parents[2]
COMMIT = "1" * 40
TREE = "2" * 40
SHA = "3" * 64
NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _legacy_core_binding() -> CurrentGoalBinding:
    """Build the frozen G01 Agent Gate view, independent of the active Goal."""

    contract_path = "backend/eval_data/agent_gate_v1/g01_automated_product_gate.json"
    return CurrentGoalBinding(
        schema_version="current-goal-binding-v2",
        goal_sequence=1,
        goal_id="TC-VNEXT-G01-TEXT-CARDS",
        status="IN_PROGRESS",
        predecessor_goal_id="TC-BP-G00-BLUEPRINT",
        predecessor_completion_commit="f3b5f3e0c36ff3977f826bd82a83b3150a2e97ac",
        automated_gate_contract_path=contract_path,
        automated_gate_contract_sha256=hashlib.sha256(
            (ROOT / contract_path).read_bytes()
        ).hexdigest(),
        gate_profile="CORE_AGENT_GATE",
        mainline_phase="CORE_MVP",
        work_package_registry_path="docs/governance/current_work_packages.json",
    )


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
        "deep_city_auto_match.coverage": 1.0,
        "estimated_confirmation_required_count.median": 0,
        "estimated_confirmation_required_count.p90": 0,
        "estimated_confirmation_required_count.population_is_deep_city": True,
        "other_city.case_count": 3,
        "other_city.gold_executable_count": 18,
        "other_city.auto_match_count": 0,
        "other_city.confirmation_required_count.total": 18,
        "other_city.confirmation_required_count.median": 6,
        "other_city.confirmation_required_count.p90": 6,
        "other_city.confirmation_required_count.max": 6,
        "other_city.population_is_other_city": True,
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


def _core_sealed_receipt(**updates) -> SealedAgentBlindReceipt:
    values = {
        "gate_profile": "CORE_AGENT_GATE",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "candidate_commit": COMMIT,
        "candidate_tree": TREE,
        "prompt_sha256": SHA,
        "schema_sha256": SHA,
        "thresholds_sha256": SHA,
        "config_sha256": SHA,
        "provider_binding_sha256": SHA,
        "scorer_sha256": SHA,
        "input_bundle_sha256": SHA,
        "prediction_bundle_sha256": SHA,
        "scored_case_count": 18,
        "custodian_task_id": "independent-core-blind",
        "aggregate_metrics": _blind_metrics(),
        "taxonomy_counts": {name: 0 for name in BLIND_ERROR_CATEGORY_ORDER},
        "error_taxonomy": [],
        "required_gate_metrics_passed": True,
        "verdict": "PASS",
        "completed_at": NOW,
    }
    values.update(updates)
    return SealedAgentBlindReceipt.model_validate(values)


def test_core_sealed_metrics_require_deep_scope_and_other_city_zero_auto() -> None:
    metrics = _blind_metrics()
    assert _thresholds_pass(metrics) is True

    wrong_population = {**metrics}
    wrong_population[
        "estimated_confirmation_required_count.population_is_deep_city"
    ] = False
    assert _thresholds_pass(wrong_population) is False

    other_city_auto = {**metrics}
    other_city_auto["other_city.auto_match_count"] = 1
    other_city_auto["other_aggregated_error_count"] = 1
    assert _thresholds_pass(other_city_auto) is False

    hidden_burden = {**metrics}
    hidden_burden["other_city.confirmation_required_count.total"] = 0
    assert _thresholds_pass(hidden_burden) is False


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
    receipt = _core_sealed_receipt()
    assert receipt.authority_signature is None
    assert receipt.custody_registry_identity_sha256 is None
    with pytest.raises(ValueError, match="cannot claim HARDENED"):
        SealedAgentBlindReceipt.model_validate(
            {
                **receipt.model_dump(mode="json"),
                "authority_policy_sha256": SHA,
            }
        )


def test_core_sealed_summary_must_derive_from_deterministic_score(
    tmp_path: Path,
) -> None:
    binding = _legacy_core_binding()
    frozen = {
        name: SHA
        for name in (
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
            "work_packages",
        )
    }
    context = CoreCandidateContext(
        repository_root=ROOT,
        candidate_commit=COMMIT,
        candidate_tree=TREE,
        binding=binding,
        config_sha256=SHA,
        data_sha256=SHA,
        frozen_binding_sha256=frozen,
        current_goal_binding_sha256=SHA,
        current_goal_document_sha256=SHA,
    )
    score = _core_sealed_receipt()
    score_path = tmp_path / "deterministic-score.json"
    score_bytes = (
        json.dumps(
            score.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    score_path.write_bytes(score_bytes)
    summary = _core_sealed_receipt(
        deterministic_score_receipt_sha256=hashlib.sha256(score_bytes).hexdigest()
    )
    summary_path = tmp_path / "sealed-summary.json"
    summary_bytes = (
        json.dumps(
            summary.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    summary_path.write_bytes(summary_bytes)

    assert verify_core_sealed(summary_path, score_path, context) == hashlib.sha256(
        summary_bytes
    ).hexdigest()
    self_report_path = tmp_path / "sealed-self-report.json"
    self_report_path.write_bytes(score_bytes)
    with pytest.raises(CoreAgentGateError, match="frozen thresholds"):
        verify_core_sealed(self_report_path, score_path, context)


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
        "work_packages",
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


def test_core_live_score_rejects_unreproducible_self_report(tmp_path: Path) -> None:
    binding = _legacy_core_binding()
    frozen = {
        name: SHA
        for name in (
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
            "work_packages",
        )
    }
    context = CoreCandidateContext(
        repository_root=ROOT,
        candidate_commit=COMMIT,
        candidate_tree=TREE,
        binding=binding,
        config_sha256=SHA,
        data_sha256=SHA,
        frozen_binding_sha256=frozen,
        current_goal_binding_sha256=SHA,
        current_goal_document_sha256=SHA,
    )
    fake = {
        "schema_version": "g01-text-card-agent-scored-receipt-v2",
        "goal_id": binding.goal_id,
        "split": "validation",
        "candidate_commit": COMMIT,
        "candidate_tree": TREE,
        "prediction_bindings": {
            "model_binding_sha256": SHA,
            "prompt_sha256": SHA,
            "schema_sha256": SHA,
            "config_sha256": SHA,
            "provider_binding_sha256": SHA,
        },
        "inference_effect_count": 18,
        "agent_adjudication": {
            "human_evidence": False,
            "live_provider_evidence_verified": True,
            "canonical_provider_bound_mentions": 50,
            "provider_binding_sha256": SHA,
        },
        "score": {
            "case_count": 18,
            "forbidden_content_as_place_count": 0,
            "severe_wrong_auto_match_count": 0,
            "wrong_city_auto_match_count": 0,
            "wrong_category_auto_match_count": 0,
            "auto_match": {"denominator": 50, "precision": 1.0},
            "executable_mentions": {"precision": 1.0, "recall": 1.0},
            "day_assignment": {"f1": 1.0},
            "role_macro_f1": 1.0,
            "deep_city_auto_match": {"coverage": 1.0},
            "estimated_confirmation_required_count": {
                "population": "DEEP_CITY",
                "median": 0,
                "p90": 0,
            },
            "other_city_confirmation_required_count": {
                "population": "OTHER_CITY",
                "case_count": 3,
                "gold_executable_count": 18,
                "auto_match_count": 0,
                "correct_auto_match_count": 0,
                "total": 18,
            },
            "evidence_span_validity": 1.0,
            "scoring_coverage": 1.0,
            "candidate_auto_selected_minimum_met": True,
        },
        "blind_inputs_read": 0,
        "blind_truth_read": 0,
        "human_usability_status": "NOT_RUN",
        "production_status": "NOT_RUN",
        "gate_claim": "VALIDATION_ONLY",
    }
    path = tmp_path / "self-reported-live-score.json"
    path.write_text(json.dumps(fake), encoding="utf-8")

    with pytest.raises(CoreAgentGateError, match="reproduction binding"):
        verify_core_live_score(path, context)
