from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from evals.agent_gate_v1.contracts import (
    AgentGateAdjudicationReceipt,
    AgentGateAuthorityManifest,
    AgentGatePassReceipt,
    AgentGateReviewReceipt,
    AuthorityAnchorReceipt,
    AuthorityActivationReadinessReceipt,
    AutomatedProductExecutionManifest,
    AutomatedProductGateContract,
    AutomatedProductGateReceipt,
    AutomatedProductVerificationReceipt,
    CurrentGoalBinding,
    CurrentGoalDocumentState,
    LiveProviderGateReceipt,
    LiveProviderVerificationReceipt,
    MultiAgentPanelGateReceipt,
    MultiAgentPanelVerificationReceipt,
    SealedAgentBlindGateReceipt,
    SealedAgentBlindReceipt,
    SealedAgentBlindMintReceipt,
    SealedAgentBlindScoreReceipt,
    SealedAgentBlindThresholds,
    SealedBlindVerificationReceipt,
    SealedScoreInputManifest,
)
from evals.trip_text_cards_agent_v2.contracts import (
    AgentAdjudicationBundle,
    AgentAnnotationBundle,
    AgentInferenceCaseOutputV2,
    AgentPredictionRunEnvelope,
    InferenceDatabaseExportReceipt,
    InferenceHttpReceiptBundle,
    InferenceRuntimeReceiptBundle,
    ProviderDatabaseExportReceipt,
    ProviderHttpReceiptBundle,
    ProviderReceiptIndex,
    ProviderRuntimeReceiptBundle,
    SealedAgentReferenceBundle,
)
from evals.agent_gate_v1.host_tools import trusted_host_tool
from evals.agent_gate_v1.external_authority import (
    ExternalSignerConformanceExpectedBindingsReceipt,
    ExternalSignerConformanceReceipt,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
GENERAL_SOURCE_ROOT = BACKEND_ROOT / "eval_data" / "agent_gate_v1"
G01_SOURCE_ROOT = BACKEND_ROOT / "eval_data" / "trip_text_cards_agent_v2"
LEGACY_G01_ROOT = BACKEND_ROOT / "eval_data" / "trip_text_cards_v1"
LEGACY_BASELINE_COMMIT = "7bdd1a6abd9c10c6076aca67f08de785027501a0"
LEGACY_BASELINE_MANIFEST_SHA256 = (
    "6638b34bda0f990b1412b1dc4c9607ab4ee98c14235879c2a4dfeced16a29310"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        handle.write("\n")


def _prompt_hashes(root: Path) -> dict[str, str]:
    return {
        path.name: _sha256(path)
        for path in sorted((root / "prompts").glob("*.md"))
    }


def _synchronize_authority_files(
    general_root: Path,
    *,
    current_binding_path: Path | None,
) -> None:
    policy_path = general_root / "authority_policy.json"
    if not policy_path.exists():
        return

    runner = {
        "mode": "OCI_EPHEMERAL_NO_HOST_MOUNTS",
        "runner_recipe_path": (
            "backend/eval_data/agent_gate_v1/automation_runner.Dockerfile"
        ),
        "runner_recipe_sha256": _sha256(
            GENERAL_SOURCE_ROOT / "automation_runner.Dockerfile"
        ),
        "runner_entrypoint_path": (
            "backend/eval_data/agent_gate_v1/automation_runner_entrypoint.sh"
        ),
        "runner_entrypoint_sha256": _sha256(
            GENERAL_SOURCE_ROOT / "automation_runner_entrypoint.sh"
        ),
        "runner_context_policy_path": (
            "backend/eval_data/agent_gate_v1/automation_runner.Dockerfile.dockerignore"
        ),
        "runner_context_policy_sha256": _sha256(
            GENERAL_SOURCE_ROOT / "automation_runner.Dockerfile.dockerignore"
        ),
        "network_access": False,
        "host_mount_count": 0,
        "host_pid_namespace": False,
        "synthetic_profile": True,
        "authority_secret_mount_count": 0,
    }
    goal_hashes: dict[str, str] = {}
    for sequence in range(1, 8):
        filename = f"g{sequence:02d}_automated_product_gate.json"
        path = general_root / filename
        value = json.loads(path.read_text(encoding="utf-8"))
        value["isolation"] = runner
        AutomatedProductGateContract.model_validate(value)
        _write_json(path, value)
        goal_hashes[filename] = _sha256(path)

    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    immutable_paths = set(policy["immutable_protocol_paths"])
    immutable_paths.update(
        {
            "backend/evals/__init__.py",
            (
                "backend/eval_data/agent_gate_v1/"
                "authority_activation_readiness.schema.json"
            ),
            "backend/evals/agent_gate_v1/automation_isolation.py",
            "backend/evals/agent_gate_v1/host_tools.py",
            "backend/evals/trip_text_cards_v1/__init__.py",
            "backend/evals/trip_text_cards_v1/contracts.py",
            "backend/evals/trip_text_cards_v1/scorer.py",
            (
                "backend/eval_data/agent_gate_v1/"
                "current_goal_document_state.schema.json"
            ),
            (
                "backend/eval_data/agent_gate_v1/"
                "automation_runner_requirements.lock"
            ),
            (
                "backend/eval_data/agent_gate_v1/"
                "automation_runner_browser_package.json"
            ),
            (
                "backend/eval_data/agent_gate_v1/"
                "automation_runner_browser_package-lock.json"
            ),
        }
    )
    policy["immutable_protocol_paths"] = sorted(immutable_paths)
    policy["program_core_paths"] = sorted(immutable_paths)
    policy.setdefault("authority_phase", "BOOTSTRAP")
    policy["bootstrap_core_paths"] = sorted(
        {
            (
                "backend/eval_data/agent_gate_v1/"
                "authority_activation_readiness.schema.json"
            ),
            "backend/eval_data/agent_gate_v1/authority_policy.schema.json",
            "backend/evals/__init__.py",
            "backend/evals/agent_gate_v1/__init__.py",
            "backend/evals/agent_gate_v1/authority.py",
            "backend/evals/agent_gate_v1/contracts.py",
            "backend/evals/agent_gate_v1/custody.py",
            "backend/evals/agent_gate_v1/host_tools.py",
            "backend/evals/agent_gate_v1/path_security.py",
            "backend/evals/agent_gate_v1/signing.py",
            "backend/evals/trip_text_cards_v1/__init__.py",
            "backend/evals/trip_text_cards_v1/contracts.py",
            "backend/evals/trip_text_cards_v1/scorer.py",
        }
    )
    policy["current_goal_document_path"] = "docs/governance/CURRENT_GOAL.md"
    policy["authority_activation_receipt_path"] = (
        "backend/eval_data/agent_gate_v1/authority_activation_readiness.json"
    )
    for binding in policy["goal_bindings"]:
        filename = Path(binding["automated_gate_contract_path"]).name
        binding["automated_gate_contract_sha256"] = goal_hashes[filename]
    AgentGateAuthorityManifest.model_validate(policy)
    _write_json(policy_path, policy)

    if current_binding_path is None:
        return
    current_binding = json.loads(current_binding_path.read_text(encoding="utf-8"))
    filename = Path(current_binding["automated_gate_contract_path"]).name
    current_binding["automated_gate_contract_sha256"] = goal_hashes[filename]
    CurrentGoalBinding.model_validate(current_binding)
    _write_json(current_binding_path, current_binding)


def generate(
    *,
    general_root: Path,
    g01_root: Path,
    current_binding_path: Path | None = None,
) -> dict[str, object]:
    general_schemas = {
        "authority_activation_readiness.schema.json": (
            AuthorityActivationReadinessReceipt.model_json_schema()
        ),
        "authority_policy.schema.json": AgentGateAuthorityManifest.model_json_schema(),
        "authority_anchor_receipt.schema.json": AuthorityAnchorReceipt.model_json_schema(),
        "current_goal_binding.schema.json": CurrentGoalBinding.model_json_schema(),
        "current_goal_document_state.schema.json": (
            CurrentGoalDocumentState.model_json_schema()
        ),
        "review.schema.json": AgentGateReviewReceipt.model_json_schema(),
        "adjudication.schema.json": AgentGateAdjudicationReceipt.model_json_schema(),
        "automated_product_gate_receipt.schema.json": (
            AutomatedProductGateReceipt.model_json_schema()
        ),
        "automated_product_gate_contract.schema.json": (
            AutomatedProductGateContract.model_json_schema()
        ),
        "automated_product_execution_manifest.schema.json": (
            AutomatedProductExecutionManifest.model_json_schema()
        ),
        "live_provider_gate_receipt.schema.json": LiveProviderGateReceipt.model_json_schema(),
        "multi_agent_panel_gate_receipt.schema.json": (
            MultiAgentPanelGateReceipt.model_json_schema()
        ),
        "sealed_agent_blind_gate_receipt.schema.json": (
            SealedAgentBlindGateReceipt.model_json_schema()
        ),
        "automated_product_verification_receipt.schema.json": (
            AutomatedProductVerificationReceipt.model_json_schema()
        ),
        "live_provider_verification_receipt.schema.json": (
            LiveProviderVerificationReceipt.model_json_schema()
        ),
        "multi_agent_panel_verification_receipt.schema.json": (
            MultiAgentPanelVerificationReceipt.model_json_schema()
        ),
        "sealed_blind_verification_receipt.schema.json": (
            SealedBlindVerificationReceipt.model_json_schema()
        ),
        "agent_gate_pass_receipt.schema.json": AgentGatePassReceipt.model_json_schema(),
        "sealed_agent_blind_receipt.schema.json": SealedAgentBlindReceipt.model_json_schema(),
        "sealed_agent_blind_mint_receipt.schema.json": (
            SealedAgentBlindMintReceipt.model_json_schema()
        ),
        "sealed_agent_blind_score_receipt.schema.json": (
            SealedAgentBlindScoreReceipt.model_json_schema()
        ),
        "sealed_score_input_manifest.schema.json": (
            SealedScoreInputManifest.model_json_schema()
        ),
        "sealed_agent_blind_thresholds.schema.json": (
            SealedAgentBlindThresholds.model_json_schema()
        ),
        "external_signer_conformance_receipt.schema.json": (
            ExternalSignerConformanceReceipt.model_json_schema()
        ),
        "external_signer_conformance_expected_bindings.schema.json": (
            ExternalSignerConformanceExpectedBindingsReceipt.model_json_schema()
        ),
    }
    for filename, schema in general_schemas.items():
        _write_json(general_root / filename, schema)
    general_contract = {
        "schema_version": "agent-gate-protocol-contract-v2",
        "model": "gpt-5.6-sol",
        "reviewer_reasoning_effort": "xhigh",
        "adjudicator_reasoning_effort": "ultra",
        "reviewer_roles": ["PRODUCT_UX", "SEMANTIC_DOMAIN", "RELIABILITY_SECURITY"],
        "evidence_levels": [
            "AUTOMATED_TEST",
            "LIVE_PROVIDER_EVIDENCE",
            "MULTI_AGENT_SIMULATED_REVIEW",
            "SEALED_AGENT_BLIND",
            "HUMAN_USABILITY",
            "PRODUCTION_EVIDENCE",
        ],
        "human_evidence": False,
        "process_isolation_not_organizational_independence": True,
        "raw_artifact_storage": "REPOSITORY_EXTERNAL",
        "candidate_change_invalidates_receipts": True,
        "goal_tree_config_data_binding_required": True,
        "scenario_evidence_hash_readback_required": True,
        "sealed_blind_nonce_registry": "REPOSITORY_EXTERNAL_SQLITE",
        "sealed_blind_replay_rejected": True,
        "authority_policy_anchor": "PRE_ANCHOR_BOOTSTRAP_THEN_GOAL_GENERATION_ACTIVE_ANCHOR",
        "authority_bootstrap_gate_evidence": "FORBIDDEN",
        "authority_activation_requires_complete_live_capture_chain": True,
        "authority_activation_readiness_schema": (
            "authority-activation-readiness-v1"
        ),
        "authority_activation_readiness_signature": "SEALED_CUSTODY",
        "authority_activation_external_signer": (
            "REPOSITORY_EXTERNAL_NO_CANDIDATE_IMPORT_NO_KEY_ENV"
        ),
        "external_signer_operation_surface": "PURPOSE_SPECIFIC_NO_CALLER_PAYLOAD",
        "external_signer_supervision": (
            "AUTHORITY_OWNED_SEPARATE_PROCESS_AND_DURABLE_REGISTRY"
        ),
        "external_signer_candidate_template": "FORBIDDEN_FOR_KEY_HOLDING_PROCESS",
        "external_signer_candidate_context": (
            "CANONICAL_REMOTE_GIT_BOOTSTRAP_DERIVED_NOT_CALLER_SUPPLIED"
        ),
        "external_signer_expected_bindings_signature": (
            "FINAL_GATE_DISTINCT_FROM_SEALED_CUSTODY"
        ),
        "external_signer_wire_validation": (
            "STRICT_TYPES_DUPLICATE_KEYS_REJECTED_SIZE_LIMITED"
        ),
        "external_signer_activation_consumption": (
            "PREPARE_ACTIVATION_BINDS_VERIFIED_CONFORMANCE_HASH"
        ),
        "immutable_code_binding_closure": (
            "DIRECT_PROGRAM_CORE_OR_TRANSITIVE_PROTOCOL_CONTRACT_SHA256"
        ),
        "authority_anchor_fact_source": "DERIVED_FROM_CANDIDATE_GIT_AND_REMOTE",
        "authority_canonical_candidate_ref": "POLICY_PINNED_NOT_CALLER_SUPPLIED",
        "authority_generation": "MATCHES_ACTIVE_GOAL_SEQUENCE",
        "immutable_protocol_bytes": "IMMUTABLE_WITHIN_EACH_GOAL_GENERATION",
        "program_trust_core_bytes": "IMMUTABLE_ACROSS_G01_G07",
        "next_generation_launcher": "PREVIOUS_ANCHORED_CLEAN_CHECKOUT",
        "full_candidate_tree_binding": True,
        "future_goal_binding": "IMMUTABLE_G01_G07_TRANSITION_AND_CONTRACT_TABLE",
        "goal_transition_authority": "EXTERNAL_APPEND_ONLY_PREDECESSOR_PASS_REGISTRY",
        "authority_signature_algorithm": "ED25519_DOMAIN_SEPARATED",
        "authority_private_keys": "REPOSITORY_EXTERNAL_ROLE_SEPARATED",
        "authority_policy_mutation_after_anchor": (
            "REJECT_WITHIN_GOAL_ALLOW_EXACT_NEXT_GENERATION_AFTER_PREDECESSOR_PASS"
        ),
        "live_provider_component_before_capture_chain": "FAIL_CLOSED_NOT_RUN",
        "strict_component_receipts": True,
        "strict_component_raw_revalidation": True,
        "automated_component_fresh_command_rerun": True,
        "automated_component_subprocess_environment": (
            "OCI_NO_NETWORK_NO_HOST_MOUNTS_NO_HOST_PID_SYNTHETIC_PROFILE"
        ),
        "automation_runner_recipe_sha256": _sha256(
            GENERAL_SOURCE_ROOT / "automation_runner.Dockerfile"
        ),
        "automation_runner_context_policy_sha256": _sha256(
            GENERAL_SOURCE_ROOT / "automation_runner.Dockerfile.dockerignore"
        ),
        "automation_runner_entrypoint_sha256": _sha256(
            GENERAL_SOURCE_ROOT / "automation_runner_entrypoint.sh"
        ),
        "caller_supplied_expected_identity_rejected": True,
        "external_artifact_hardlinks_rejected": True,
        "external_artifact_git_managed_locations_rejected": True,
        "external_artifact_outputs": "EXCLUSIVE_CREATE_AND_FSYNC",
        "sealed_score_source": "PURPOSE_SPECIFIC_RAW_INPUT_PREDICTION_TRUTH_SCORER",
        "sealed_score_caller_supplied_aggregate_metrics": False,
        "sealed_truth_commitment": "HMAC_SHA256_EXTERNAL_KEY",
        "live_provider_exporters": (
            "FAIL_CLOSED_PENDING_CUSTODY_REGISTRY_MINT_AND_SIGNED_HTTPS_CAPTURE"
        ),
        "live_provider_payload_source": (
            "CUSTODY_PINNED_REGISTRY_AND_PURPOSE_SIGNED_TYPED_EFFECTS"
        ),
        "live_provider_current_status": "NOT_RUN",
        "arbitrary_database_url_rejected": True,
        "sealed_tranche_commitment_formula": (
            "canonical_sha256(input_bundle_sha256,case_set_commitment_sha256,"
            "truth_bundle_commitment)"
        ),
        "final_agent_gate_pass_aggregator": "evals.agent_gate_v1.final_gate",
        "remote_and_fresh_checkout_readback_required": True,
        "immutable_candidate_ref_per_goal_commit": True,
        "blocking_findings": ["P0", "P1", "P2_IN_CURRENT_GOAL"],
        "required_clean_checkout_fresh_readback": True,
        "required_scenarios": [
            "normal",
            "ambiguous",
            "boundary",
            "adversarial",
            "provider_failure",
            "privacy",
            "concurrency",
        ],
        "prompt_sha256": _prompt_hashes(GENERAL_SOURCE_ROOT),
        "schema_sha256": {
            filename: _sha256(general_root / filename)
            for filename in sorted(general_schemas)
        },
        "contract_code_sha256": {
            "contracts.py": _sha256(BACKEND_ROOT / "evals" / "agent_gate_v1" / "contracts.py"),
            "automation_isolation.py": _sha256(
                BACKEND_ROOT / "evals" / "agent_gate_v1" / "automation_isolation.py"
            ),
            "custody.py": _sha256(BACKEND_ROOT / "evals" / "agent_gate_v1" / "custody.py"),
            "authority.py": _sha256(BACKEND_ROOT / "evals" / "agent_gate_v1" / "authority.py"),
            "component_verifiers.py": _sha256(
                BACKEND_ROOT / "evals" / "agent_gate_v1" / "component_verifiers.py"
            ),
            "component_builders.py": _sha256(
                BACKEND_ROOT / "evals" / "agent_gate_v1" / "component_builders.py"
            ),
            "final_gate.py": _sha256(
                BACKEND_ROOT / "evals" / "agent_gate_v1" / "final_gate.py"
            ),
            "host_tools.py": _sha256(
                BACKEND_ROOT / "evals" / "agent_gate_v1" / "host_tools.py"
            ),
            "external_authority.py": _sha256(
                BACKEND_ROOT / "evals" / "agent_gate_v1" / "external_authority.py"
            ),
            "live_export.py": _sha256(
                BACKEND_ROOT / "evals" / "agent_gate_v1" / "live_export.py"
            ),
            "path_security.py": _sha256(
                BACKEND_ROOT / "evals" / "agent_gate_v1" / "path_security.py"
            ),
            "sealed_score.py": _sha256(
                BACKEND_ROOT / "evals" / "agent_gate_v1" / "sealed_score.py"
            ),
            "signing.py": _sha256(
                BACKEND_ROOT / "evals" / "agent_gate_v1" / "signing.py"
            ),
            "validator.py": _sha256(BACKEND_ROOT / "evals" / "agent_gate_v1" / "validator.py"),
        },
    }
    _write_json(general_root / "protocol_contract.json", general_contract)

    g01_schemas = {
        "agent_annotation.schema.json": AgentAnnotationBundle.model_json_schema(),
        "agent_adjudication.schema.json": AgentAdjudicationBundle.model_json_schema(),
        "agent_prediction_run.schema.json": AgentPredictionRunEnvelope.model_json_schema(),
        "agent_inference_case_output.schema.json": (
            AgentInferenceCaseOutputV2.model_json_schema()
        ),
        "sealed_agent_reference.schema.json": (
            SealedAgentReferenceBundle.model_json_schema()
        ),
        "inference_runtime_receipt_bundle.schema.json": (
            InferenceRuntimeReceiptBundle.model_json_schema()
        ),
        "inference_database_export_receipt.schema.json": (
            InferenceDatabaseExportReceipt.model_json_schema()
        ),
        "inference_http_receipt_bundle.schema.json": (
            InferenceHttpReceiptBundle.model_json_schema()
        ),
        "provider_database_export_receipt.schema.json": (
            ProviderDatabaseExportReceipt.model_json_schema()
        ),
        "provider_http_receipt_bundle.schema.json": (
            ProviderHttpReceiptBundle.model_json_schema()
        ),
        "provider_receipt_index.schema.json": ProviderReceiptIndex.model_json_schema(),
        "provider_runtime_receipt_bundle.schema.json": (
            ProviderRuntimeReceiptBundle.model_json_schema()
        ),
    }
    for filename, schema in g01_schemas.items():
        _write_json(g01_root / filename, schema)

    sealed_thresholds = {
        "schema_version": "sealed-agent-blind-thresholds-v1",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "required_metric_names": [
            "forbidden_content_as_place_count",
            "severe_wrong_auto_match_count",
            "wrong_city_auto_match_count",
            "wrong_category_auto_match_count",
            "auto_match.precision",
            "auto_match.denominator",
            "executable_mentions.precision",
            "executable_mentions.recall",
            "executable_mentions.fp",
            "executable_mentions.fn",
            "day_assignment.f1",
            "day_assignment.error_count",
            "role_macro_f1",
            "role_classification.error_count",
            "provider_resolution.error_count",
            "evidence_span_validity",
            "destination.exact_name_accuracy",
            "destination.basis_accuracy",
            "destination.explicit_evidence_validity",
            "public_projection.forbidden_key_hits",
            "public_projection.full_source_leak_hits",
            "public_api_latency.cards_ready_p95_ms",
            "latency.violation_count",
            "other_aggregated_error_count"
        ],
        "conditions": [
            {"metric": "forbidden_content_as_place_count", "operator": "EQ", "value": 0},
            {"metric": "severe_wrong_auto_match_count", "operator": "EQ", "value": 0},
            {"metric": "wrong_city_auto_match_count", "operator": "EQ", "value": 0},
            {"metric": "wrong_category_auto_match_count", "operator": "EQ", "value": 0},
            {"metric": "auto_match.precision", "operator": "GE", "value": 0.99},
            {"metric": "auto_match.denominator", "operator": "GE", "value": 50},
            {"metric": "executable_mentions.precision", "operator": "GE", "value": 0.98},
            {"metric": "executable_mentions.recall", "operator": "GE", "value": 0.95},
            {"metric": "day_assignment.f1", "operator": "GE", "value": 0.97},
            {"metric": "role_macro_f1", "operator": "GE", "value": 0.94},
            {"metric": "evidence_span_validity", "operator": "EQ", "value": 1.0},
            {"metric": "destination.exact_name_accuracy", "operator": "GE", "value": 0.99},
            {"metric": "destination.basis_accuracy", "operator": "GE", "value": 0.99},
            {
                "metric": "destination.explicit_evidence_validity",
                "operator": "EQ",
                "value": 1.0,
            },
            {"metric": "public_projection.forbidden_key_hits", "operator": "EQ", "value": 0},
            {"metric": "public_projection.full_source_leak_hits", "operator": "EQ", "value": 0},
            {
                "metric": "public_api_latency.cards_ready_p95_ms",
                "operator": "LE",
                "value": 8000,
            },
        ],
    }
    SealedAgentBlindThresholds.model_validate(sealed_thresholds)
    _write_json(g01_root / "sealed_blind_thresholds.json", sealed_thresholds)

    baseline_manifest_path = G01_SOURCE_ROOT / "legacy_human_v1_baseline_manifest.json"
    if _sha256(baseline_manifest_path) != LEGACY_BASELINE_MANIFEST_SHA256:
        raise ValueError("legacy baseline manifest bytes are not the frozen archive")
    legacy_manifest = json.loads(baseline_manifest_path.read_text(encoding="utf-8"))
    baseline_commit = legacy_manifest["baseline_commit"]
    if baseline_commit != LEGACY_BASELINE_COMMIT:
        raise ValueError("legacy baseline commit is not the governed checkpoint")
    listed = subprocess.run(
        [
            trusted_host_tool("git"),
            "-C",
            str(BACKEND_ROOT.parent),
            "ls-tree",
            "-r",
            "--name-only",
            baseline_commit,
            "--",
            "backend/eval_data/trip_text_cards_v1",
            "backend/evidence/trip_text_cards_v1",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if set(listed) != set(legacy_manifest["files"]):
        raise ValueError("legacy baseline manifest does not cover every frozen file")
    for relative, expected_sha256 in legacy_manifest["files"].items():
        baseline_bytes = subprocess.run(
            [
                trusted_host_tool("git"),
                "-C",
                str(BACKEND_ROOT.parent),
                "show",
                f"{baseline_commit}:{relative}",
            ],
            check=True,
            capture_output=True,
        ).stdout
        if hashlib.sha256(baseline_bytes).hexdigest() != expected_sha256:
            raise ValueError(f"legacy baseline manifest hash mismatch: {relative}")
        if _sha256(BACKEND_ROOT.parent / relative) != expected_sha256:
            raise ValueError(f"legacy frozen worktree bytes changed: {relative}")
    _write_json(g01_root / "legacy_human_v1_baseline_manifest.json", legacy_manifest)
    _write_json(g01_root / "legacy_human_v1_manifest.json", legacy_manifest)

    g01_contract = {
        "schema_version": "g01-text-card-agent-evaluation-contract-v2",
        "dataset_version": "g01-text-card-dataset-v1",
        "input_dataset_contract_sha256": _sha256(LEGACY_G01_ROOT / "dataset_contract.json"),
        "input_file_sha256": {
            split: _sha256(LEGACY_G01_ROOT / f"{split}.inputs.jsonl")
            for split in ("dev", "validation", "frozen_blind")
        },
        "ordinary_agent_splits": ["dev", "validation"],
        "ordinary_agent_blind_access": False,
        "sealed_agent_reference_schema": "sealed_agent_reference.schema.json",
        "sealed_agent_reference_storage": "REPOSITORY_EXTERNAL",
        "sealed_agent_reference_candidate_output_visibility": "NONE",
        "sealed_runtime_split": "frozen_blind",
        "sealed_scorer_path": "backend/scripts/score_g01_sealed_agent_blind.py",
        "sealed_scorer_accepts_aggregate_metrics": False,
        "reference_tasks": {
            "count": 2,
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "candidate_output_visibility": "NONE",
            "peer_output_visibility": "NONE",
        },
        "adjudication_task": {
            "count": 1,
            "model": "gpt-5.6-sol",
            "reasoning_effort": "ultra",
            "starts_after_references_frozen": True,
            "candidate_output_visibility": "NONE",
        },
        "canonical_place_authority": "PROVIDER_BOUND_AGENT_REFERENCE",
        "executable_source_span_must_be_atomic": True,
        "destination_basis": ["EXPLICIT", "SOFT_ASSUMPTION"],
        "provider_resolution_status": ["MATCHED", "UNRESOLVED", "AMBIGUOUS"],
        "provider_receipt_authorization_basis": "OWNER_ATTESTED_EXISTING_AUTHORIZATION",
        "provider_receipt_source_runtime": "PERSISTED_PROVIDER_EFFECT_REGISTRY",
        "provider_index_frozen_before_reference_tasks": True,
        "prediction_run_requires_candidate_and_provider_bindings": True,
        "raw_artifact_storage": "REPOSITORY_EXTERNAL",
        "repository_truth_payloads": 0,
        "evidence_levels": ["MULTI_AGENT_SIMULATED_REVIEW", "LIVE_PROVIDER_EVIDENCE"],
        "human_usability_status": "NOT_RUN",
        "production_status": "NOT_RUN",
        "limitations": ["PROCESS_ISOLATION_NOT_ORGANIZATIONAL_INDEPENDENCE"],
        "sealed_blind_protocol": "agent-gate-protocol-contract-v2",
        "sealed_blind_thresholds_sha256": _sha256(
            g01_root / "sealed_blind_thresholds.json"
        ),
        "prompt_sha256": _prompt_hashes(G01_SOURCE_ROOT),
        "schema_sha256": {
            filename: _sha256(g01_root / filename)
            for filename in sorted(g01_schemas)
        },
        "legacy_human_v1_manifest_sha256": _sha256(g01_root / "legacy_human_v1_manifest.json"),
        "contract_code_sha256": {
            "contracts.py": _sha256(
                BACKEND_ROOT / "evals" / "trip_text_cards_agent_v2" / "contracts.py"
            ),
            "annotations.py": _sha256(
                BACKEND_ROOT / "evals" / "trip_text_cards_agent_v2" / "annotations.py"
            ),
            "split_loader.py": _sha256(
                BACKEND_ROOT / "evals" / "trip_text_cards_agent_v2" / "split_loader.py"
            ),
        },
    }
    _write_json(g01_root / "agent_evaluation_contract.json", g01_contract)
    canonical_general_root = GENERAL_SOURCE_ROOT.resolve()
    resolved_general_root = general_root.resolve()
    binding_output = current_binding_path
    if binding_output is None and resolved_general_root == canonical_general_root:
        binding_output = (
            BACKEND_ROOT.parent
            / "docs"
            / "governance"
            / "current_goal_binding.json"
        )
    _synchronize_authority_files(
        general_root,
        current_binding_path=binding_output,
    )
    return {"general": general_contract, "g01": g01_contract}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--general-root",
        type=Path,
        default=GENERAL_SOURCE_ROOT,
    )
    parser.add_argument(
        "--g01-root",
        type=Path,
        default=G01_SOURCE_ROOT,
    )
    parser.add_argument("--current-goal-binding", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            generate(
                general_root=args.general_root,
                g01_root=args.g01_root,
                current_binding_path=args.current_goal_binding,
            ),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
