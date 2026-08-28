from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.agent_gate_v1.authority import (
    compute_git_blob_bundle_hash,
    load_anchored_authority_policy,
    load_current_goal_binding,
)
from evals.agent_gate_v1.automation_isolation import (
    AutomationIsolationError,
    build_isolated_candidate_image,
    run_isolated_check,
    save_isolated_candidate_image,
)
from evals.agent_gate_v1.component_verifiers import (
    _canonical_sha256,
    _repository_relative_path,
    verify_automated_product_evidence,
)
from evals.agent_gate_v1.contracts import (
    AgentGateComponent,
    AutomatedProductExecutionManifest,
    AutomatedProductGateContract,
    AutomatedProductGateReceipt,
    AutomatedProductVerificationReceipt,
    DetachedAuthoritySignature,
    LiveProviderGateReceipt,
    LiveProviderVerificationReceipt,
    MultiAgentPanelGateReceipt,
    MultiAgentPanelVerificationReceipt,
    ReviewerRole,
    SealedAgentBlindGateReceipt,
    SealedBlindVerificationReceipt,
    StrictComponentReceipt,
)
from evals.agent_gate_v1.host_tools import trusted_host_tool
from evals.agent_gate_v1.path_security import (
    read_external_snapshot,
    write_external_bytes_exclusive,
)
from evals.agent_gate_v1.signing import verify_payload_signature
from evals.agent_gate_v1.validator import verify_review_panel, verify_sealed_agent_blind
from evals.trip_text_cards_agent_v2.annotations import (
    validate_inference_runtime_receipt_assets,
    validate_provider_receipt_assets,
)
from evals.trip_text_cards_agent_v2.contracts import (
    InferenceRuntimeReceiptBundle,
    ProviderReceiptIndex,
    ProviderRuntimeReceiptBundle,
)


class ComponentBuildError(ValueError):
    pass


def _git(root: Path, *args: str, text: bool = True):
    result = subprocess.run(
        [trusted_host_tool("git"), "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=text,
    )
    if result.returncode != 0:
        raise ComponentBuildError(f"Git component build failed: {' '.join(args)}")
    return result.stdout.strip() if text else result.stdout


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_json(path: Path, value: object, repository_root: Path):
    write_external_bytes_exclusive(path, _json_bytes(value), repository_root)
    return read_external_snapshot(path, repository_root)


class _BuildContext:
    def __init__(self, repository_root: Path):
        self.repository_root = repository_root.resolve(strict=True)
        if _git(self.repository_root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise ComponentBuildError("component builders require a clean candidate checkout")
        self.candidate_commit = _git(self.repository_root, "rev-parse", "HEAD")
        self.candidate_tree = _git(
            self.repository_root,
            "show",
            "-s",
            "--format=%T",
            self.candidate_commit,
        )
        self.anchored = load_anchored_authority_policy(
            self.repository_root,
            self.candidate_commit,
        )
        self.goal_binding = load_current_goal_binding(
            self.repository_root,
            self.candidate_commit,
            self.anchored.manifest,
        )
        self.goal_id = self.goal_binding.goal_id
        self.config_sha256 = compute_git_blob_bundle_hash(
            self.repository_root,
            self.candidate_commit,
            self.anchored.manifest.config_roots,
        )
        self.data_sha256 = compute_git_blob_bundle_hash(
            self.repository_root,
            self.candidate_commit,
            self.anchored.manifest.data_roots,
        )

    def common_unsigned(
        self,
        *,
        component: AgentGateComponent,
        verification_path: Path,
        verification_sha256: str,
    ) -> dict[str, Any]:
        verifier_path = self.anchored.manifest.component_verifier_paths[component]
        verifier_sha256 = hashlib.sha256(
            _git(
                self.repository_root,
                "show",
                f"{self.candidate_commit}:{verifier_path}",
                text=False,
            )
        ).hexdigest()
        return {
            "goal_id": self.goal_id,
            "candidate_commit": self.candidate_commit,
            "candidate_tree": self.candidate_tree,
            "candidate_config_sha256": self.config_sha256,
            "candidate_data_sha256": self.data_sha256,
            "component": component,
            "authority_policy_sha256": self.anchored.sha256,
            "verifier_path": verifier_path,
            "verifier_sha256": verifier_sha256,
            "verification_receipt_path": str(verification_path),
            "verification_receipt_sha256": verification_sha256,
            "checks_not_run": [],
            "human_evidence": False,
            "verdict": "PASS",
            "completed_at": _now(),
        }


def _attach_external_signature(
    *,
    context: _BuildContext,
    component: AgentGateComponent,
    unsigned: dict[str, Any],
    authority_signature: DetachedAuthoritySignature,
) -> StrictComponentReceipt:
    try:
        verify_payload_signature(
            payload=unsigned,
            signature=authority_signature,
            manifest=context.anchored.manifest,
            expected_role=component,
        )
    except ValueError as exc:
        raise ComponentBuildError("external component signature is invalid") from exc
    value = {
        **unsigned,
        "authority_signature": authority_signature.model_dump(mode="json"),
    }
    models = {
        "AUTOMATED_PRODUCT_GATE": AutomatedProductGateReceipt,
        "LIVE_PROVIDER_GATE": LiveProviderGateReceipt,
        "MULTI_AGENT_PANEL": MultiAgentPanelGateReceipt,
        "SEALED_AGENT_BLIND": SealedAgentBlindGateReceipt,
    }
    return models[component].model_validate(value)


def _write_component(
    *,
    receipt: StrictComponentReceipt,
    output_path: Path,
    repository_root: Path,
) -> None:
    _write_json(output_path, receipt.model_dump(mode="json"), repository_root)


def build_automated_product_component(
    *,
    repository_root: Path,
    execution_manifest_output: Path,
    runner_image_archive_output: Path,
    verification_output: Path,
    component_output: Path,
    authority_signature: DetachedAuthoritySignature,
) -> AutomatedProductGateReceipt:
    context = _BuildContext(repository_root)
    relative = _repository_relative_path(
        context.goal_binding.automated_gate_contract_path,
        "automated Gate contract",
    ).as_posix()
    contract_bytes = _git(
        context.repository_root,
        "show",
        f"{context.candidate_commit}:{relative}",
        text=False,
    )
    contract = AutomatedProductGateContract.model_validate_json(contract_bytes)
    if (
        contract.goal_id != context.goal_id
        or hashlib.sha256(contract_bytes).hexdigest()
        != context.goal_binding.automated_gate_contract_sha256
    ):
        raise ComponentBuildError("automated Gate contract goal does not match current Goal")
    try:
        image_tag, image_id = build_isolated_candidate_image(
            repository_root=context.repository_root,
            candidate_commit=context.candidate_commit,
            candidate_tree=context.candidate_tree,
            recipe_path=contract.isolation.runner_recipe_path,
            recipe_sha256=contract.isolation.runner_recipe_sha256,
            entrypoint_path=contract.isolation.runner_entrypoint_path,
            entrypoint_sha256=contract.isolation.runner_entrypoint_sha256,
            context_policy_path=contract.isolation.runner_context_policy_path,
            context_policy_sha256=(
                contract.isolation.runner_context_policy_sha256
            ),
        )
        archive = save_isolated_candidate_image(
            repository_root=context.repository_root,
            image_tag=image_tag,
            expected_image_id=image_id,
            archive_output=runner_image_archive_output,
        )
    except AutomationIsolationError as exc:
        not_run_manifest = AutomatedProductExecutionManifest.model_validate(
            {
                "schema_version": "automated-product-execution-manifest-v1",
                "goal_id": context.goal_id,
                "candidate_commit": context.candidate_commit,
                "candidate_tree": context.candidate_tree,
                "candidate_config_sha256": context.config_sha256,
                "candidate_data_sha256": context.data_sha256,
                "gate_contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
                "isolation_mode": contract.isolation.mode,
                "runner_recipe_sha256": contract.isolation.runner_recipe_sha256,
                "runner_entrypoint_sha256": (
                    contract.isolation.runner_entrypoint_sha256
                ),
                "runner_context_policy_sha256": (
                    contract.isolation.runner_context_policy_sha256
                ),
                "runner_image_id": None,
                "runner_image_archive_format": None,
                "runner_image_archive_path": None,
                "runner_image_archive_sha256": None,
                "runner_image_archive_size": None,
                "network_access": False,
                "host_mount_count": 0,
                "host_pid_namespace": False,
                "synthetic_profile": True,
                "authority_secret_mount_count": 0,
                "checks": [],
                "checks_not_run": [item.check_id for item in contract.checks],
                "failure_stage": (
                    "OCI_RUNNER_UNAVAILABLE"
                    if "requires an available OCI/Docker runner" in str(exc)
                    else (
                        "OCI_ARCHIVE_FAILED"
                        if "archive" in str(exc)
                        else "OCI_BUILD_FAILED"
                    )
                ),
                "verdict": "NOT_RUN",
            }
        )
        _write_json(
            execution_manifest_output,
            not_run_manifest.model_dump(mode="json"),
            context.repository_root,
        )
        raise ComponentBuildError(str(exc)) from exc
    executions = []
    for check in contract.checks:
        started_at = _now()
        try:
            result = run_isolated_check(
                repository_root=context.repository_root,
                expected_image_id=image_id,
                workdir=check.workdir,
                argv=check.argv,
                timeout_seconds=check.timeout_seconds,
            )
        except AutomationIsolationError as exc:
            raise ComponentBuildError(str(exc)) from exc
        completed_at = _now()
        if result.exit_code != 0:
            raise ComponentBuildError(f"automated check failed: {check.check_id}")
        executions.append(
            {
                "check_id": check.check_id,
                "argv_sha256": _canonical_sha256(check.argv),
                "workdir": check.workdir,
                "exit_code": 0,
                "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
                "started_at": started_at,
                "completed_at": completed_at,
                "verdict": "PASS",
            }
        )
    manifest = AutomatedProductExecutionManifest.model_validate(
        {
            "schema_version": "automated-product-execution-manifest-v1",
            "goal_id": context.goal_id,
            "candidate_commit": context.candidate_commit,
            "candidate_tree": context.candidate_tree,
            "candidate_config_sha256": context.config_sha256,
            "candidate_data_sha256": context.data_sha256,
            "gate_contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
            "isolation_mode": contract.isolation.mode,
            "runner_recipe_sha256": contract.isolation.runner_recipe_sha256,
            "runner_entrypoint_sha256": (
                contract.isolation.runner_entrypoint_sha256
            ),
            "runner_context_policy_sha256": (
                contract.isolation.runner_context_policy_sha256
            ),
            "runner_image_id": image_id,
            "runner_image_archive_format": "DOCKER_IMAGE_ARCHIVE_V1",
            "runner_image_archive_path": str(archive.path),
            "runner_image_archive_sha256": archive.sha256,
            "runner_image_archive_size": archive.size,
            "network_access": False,
            "host_mount_count": 0,
            "host_pid_namespace": False,
            "synthetic_profile": True,
            "authority_secret_mount_count": 0,
            "checks": executions,
            "checks_not_run": [],
            "failure_stage": None,
            "verdict": "PASS",
        }
    )
    _write_json(
        execution_manifest_output,
        manifest.model_dump(mode="json"),
        context.repository_root,
    )
    verified = verify_automated_product_evidence(
        repository_root=context.repository_root,
        candidate_commit=context.candidate_commit,
        candidate_tree=context.candidate_tree,
        candidate_config_sha256=context.config_sha256,
        candidate_data_sha256=context.data_sha256,
        expected_goal_id=context.goal_id,
        gate_contract_path=relative,
        execution_manifest_path=execution_manifest_output,
    )
    verification = AutomatedProductVerificationReceipt.model_validate(verified)
    verification_snapshot = _write_json(
        verification_output,
        verification.model_dump(mode="json"),
        context.repository_root,
    )
    unsigned = {
        "schema_version": "automated-product-gate-receipt-v2",
        **context.common_unsigned(
            component="AUTOMATED_PRODUCT_GATE",
            verification_path=verification_output,
            verification_sha256=verification_snapshot.sha256,
        ),
        "gate_contract_path": verification.gate_contract_path,
        "gate_contract_sha256": verification.gate_contract_sha256,
        "execution_manifest_path": verification.execution_manifest_path,
        "execution_manifest_sha256": verification.execution_manifest_sha256,
        "executed_check_count": verification.executed_check_count,
        "failed_check_count": 0,
        "isolation_mode": verification.isolation_mode,
        "runner_recipe_sha256": verification.runner_recipe_sha256,
        "runner_entrypoint_sha256": verification.runner_entrypoint_sha256,
        "runner_context_policy_sha256": (
            verification.runner_context_policy_sha256
        ),
        "runner_image_id": verification.runner_image_id,
        "runner_image_archive_format": verification.runner_image_archive_format,
        "runner_image_archive_path": verification.runner_image_archive_path,
        "runner_image_archive_sha256": verification.runner_image_archive_sha256,
        "runner_image_archive_size": verification.runner_image_archive_size,
        "network_access": False,
        "host_mount_count": 0,
        "host_pid_namespace": False,
        "evidence_level": "AUTOMATED_TEST",
    }
    receipt = _attach_external_signature(
        context=context,
        component="AUTOMATED_PRODUCT_GATE",
        unsigned=unsigned,
        authority_signature=authority_signature,
    )
    _write_component(
        receipt=receipt,
        output_path=component_output,
        repository_root=context.repository_root,
    )
    return receipt


def build_live_provider_component(
    *,
    repository_root: Path,
    amap_provider_index_path: Path,
    amap_runtime_path: Path,
    qwen_runtime_path: Path,
    verification_output: Path,
    component_output: Path,
    authority_signature: DetachedAuthoritySignature,
) -> LiveProviderGateReceipt:
    raise ComponentBuildError(
        "formal live Provider Gate is NOT_RUN until the custody-minted direct-HTTPS "
        "capture execution receipt is implemented"
    )
    context = _BuildContext(repository_root)
    amap_index_snapshot = read_external_snapshot(
        amap_provider_index_path,
        context.repository_root,
    )
    amap_runtime_snapshot = read_external_snapshot(
        amap_runtime_path,
        context.repository_root,
    )
    qwen_runtime_snapshot = read_external_snapshot(
        qwen_runtime_path,
        context.repository_root,
    )
    amap_index = ProviderReceiptIndex.model_validate_json(amap_index_snapshot.content)
    amap_runtime = ProviderRuntimeReceiptBundle.model_validate_json(
        amap_runtime_snapshot.content
    )
    qwen_runtime = InferenceRuntimeReceiptBundle.model_validate_json(
        qwen_runtime_snapshot.content
    )
    artifact_snapshots = {
        snapshot.path.resolve(strict=True): snapshot
        for snapshot in (
            amap_index_snapshot,
            amap_runtime_snapshot,
            qwen_runtime_snapshot,
        )
    }
    child_path_values = (
        amap_runtime.database_export_receipt_path,
        amap_runtime.provider_http_receipt_bundle_path,
        qwen_runtime.predictions_path,
        qwen_runtime.inference_outputs_path,
        qwen_runtime.database_export_receipt_path,
        qwen_runtime.provider_http_receipt_bundle_path,
    )
    if any(not isinstance(value, str) or not value for value in child_path_values):
        raise ComponentBuildError("live Provider evidence child paths are incomplete")
    child_paths = tuple(Path(value) for value in child_path_values)
    for child_path in child_paths:
        resolved = child_path.resolve(strict=True)
        if resolved in artifact_snapshots:
            raise ComponentBuildError(
                "live Provider evidence paths must identify distinct immutable artifacts"
            )
        snapshot = read_external_snapshot(child_path, context.repository_root)
        artifact_snapshots[snapshot.path.resolve(strict=True)] = snapshot
    if amap_index.split != qwen_runtime.split:
        raise ComponentBuildError("AMap and Qwen live evidence splits disagree")
    _index, amap_runtime, amap_verified = validate_provider_receipt_assets(
        split=amap_index.split,
        provider_receipt_index_path=amap_provider_index_path,
        provider_runtime_receipt_bundle_path=amap_runtime_path,
        repository_root=context.repository_root,
        expected_candidate_commit=context.candidate_commit,
        expected_candidate_tree=context.candidate_tree,
        expected_goal_id=context.goal_id,
        expected_provider_binding_sha256=amap_index.provider_binding_sha256,
        expected_runtime_receipt_bundle_sha256=amap_runtime_snapshot.sha256,
        expected_database_export_receipt_sha256=(
            amap_runtime.database_export_receipt_sha256
        ),
        expected_provider_http_receipt_bundle_sha256=(
            amap_runtime.provider_http_receipt_bundle_sha256
        ),
        require_live_provider_evidence=True,
        artifact_snapshots=artifact_snapshots,
    )
    qwen_runtime, qwen_verified = validate_inference_runtime_receipt_assets(
        inference_runtime_receipt_bundle_path=qwen_runtime_path,
        repository_root=context.repository_root,
        expected_candidate_commit=context.candidate_commit,
        expected_candidate_tree=context.candidate_tree,
        expected_goal_id=context.goal_id,
        require_live_provider_evidence=True,
        artifact_snapshots=artifact_snapshots,
    )
    verification = LiveProviderVerificationReceipt.model_validate(
        {
            "schema_version": "live-provider-verification-receipt-v2",
            "goal_id": context.goal_id,
            "candidate_commit": context.candidate_commit,
            "candidate_tree": context.candidate_tree,
            "authority_policy_sha256": context.anchored.sha256,
            "amap_provider_receipt_index_path": str(amap_provider_index_path),
            "amap_provider_receipt_index_sha256": amap_index_snapshot.sha256,
            "amap_runtime_receipt_path": str(amap_runtime_path),
            "amap_runtime_receipt_sha256": amap_runtime_snapshot.sha256,
            "qwen_runtime_receipt_path": str(qwen_runtime_path),
            "qwen_runtime_receipt_sha256": qwen_runtime_snapshot.sha256,
            "split": amap_index.split,
            "amap_provider_binding_sha256": amap_index.provider_binding_sha256,
            "amap_database_export_receipt_sha256": (
                amap_verified["database_export_receipt_sha256"]
            ),
            "amap_http_receipt_bundle_sha256": (
                amap_verified["provider_http_receipt_bundle_sha256"]
            ),
            "amap_live_effect_count": len(amap_runtime.effects),
            "qwen_live_effect_count": qwen_verified["qwen_live_effect_count"],
            "amap_execution_mode": "LIVE",
            "qwen_execution_mode": "LIVE",
            "fixture_effect_count": 0,
            "verdict": "PASS",
        }
    )
    verification_snapshot = _write_json(
        verification_output,
        verification.model_dump(mode="json"),
        context.repository_root,
    )
    unsigned = {
        "schema_version": "live-provider-gate-receipt-v2",
        **context.common_unsigned(
            component="LIVE_PROVIDER_GATE",
            verification_path=verification_output,
            verification_sha256=verification_snapshot.sha256,
        ),
        "amap_provider_receipt_index_path": str(amap_provider_index_path),
        "amap_provider_receipt_index_sha256": amap_index_snapshot.sha256,
        "amap_runtime_receipt_path": str(amap_runtime_path),
        "amap_runtime_receipt_sha256": amap_runtime_snapshot.sha256,
        "qwen_runtime_receipt_path": str(qwen_runtime_path),
        "qwen_runtime_receipt_sha256": qwen_runtime_snapshot.sha256,
        "split": verification.split,
        "amap_provider_binding_sha256": verification.amap_provider_binding_sha256,
        "amap_database_export_receipt_sha256": (
            verification.amap_database_export_receipt_sha256
        ),
        "amap_http_receipt_bundle_sha256": (
            verification.amap_http_receipt_bundle_sha256
        ),
        "amap_live_effect_count": verification.amap_live_effect_count,
        "qwen_live_effect_count": verification.qwen_live_effect_count,
        "fixture_effect_count": 0,
        "evidence_level": "LIVE_PROVIDER_EVIDENCE",
    }
    receipt = _attach_external_signature(
        context=context,
        component="LIVE_PROVIDER_GATE",
        unsigned=unsigned,
        authority_signature=authority_signature,
    )
    _write_component(
        receipt=receipt,
        output_path=component_output,
        repository_root=context.repository_root,
    )
    return receipt


def build_multi_agent_panel_component(
    *,
    repository_root: Path,
    review_paths: list[Path],
    adjudication_path: Path,
    input_bundle_paths: dict[ReviewerRole, Path],
    verification_output: Path,
    component_output: Path,
    authority_signature: DetachedAuthoritySignature,
) -> MultiAgentPanelGateReceipt:
    context = _BuildContext(repository_root)
    expected_input_hashes = {
        role: read_external_snapshot(path, context.repository_root).sha256
        for role, path in input_bundle_paths.items()
    }
    verified = verify_review_panel(
        review_paths=review_paths,
        adjudication_path=adjudication_path,
        repository_root=context.repository_root,
        expected_goal_id=context.goal_id,
        expected_candidate_commit=context.candidate_commit,
        expected_candidate_tree=context.candidate_tree,
        expected_candidate_config_sha256=context.config_sha256,
        expected_candidate_data_sha256=context.data_sha256,
        expected_input_bundle_sha256=expected_input_hashes,
    )
    verification = MultiAgentPanelVerificationReceipt.model_validate(
        {
            "schema_version": "multi-agent-panel-verification-receipt-v2",
            "goal_id": context.goal_id,
            "candidate_commit": context.candidate_commit,
            "candidate_tree": context.candidate_tree,
            "candidate_config_sha256": context.config_sha256,
            "candidate_data_sha256": context.data_sha256,
            "review_paths": [str(path) for path in review_paths],
            "adjudication_path": str(adjudication_path),
            "expected_input_bundle_sha256": expected_input_hashes,
            "review_count": 3,
            "review_sha256": verified["review_sha256"],
            "adjudication_sha256": verified["adjudication_sha256"],
            "accepted_p0_count": verified["accepted_p0_count"],
            "accepted_p1_count": verified["accepted_p1_count"],
            "accepted_in_scope_p2_count": verified["accepted_in_scope_p2_count"],
            "required_scenario_union_complete": verified["roles_complete"],
            "verdict": verified["verdict"],
        }
    )
    verification_snapshot = _write_json(
        verification_output,
        verification.model_dump(mode="json"),
        context.repository_root,
    )
    unsigned = {
        "schema_version": "multi-agent-panel-gate-receipt-v2",
        **context.common_unsigned(
            component="MULTI_AGENT_PANEL",
            verification_path=verification_output,
            verification_sha256=verification_snapshot.sha256,
        ),
        "review_paths": verification.review_paths,
        "adjudication_path": verification.adjudication_path,
        "expected_input_bundle_sha256": verification.expected_input_bundle_sha256,
        "review_receipt_sha256": verification.review_sha256,
        "adjudication_receipt_sha256": verification.adjudication_sha256,
        "reviewer_task_count": 3,
        "adjudicator_task_count": 1,
        "accepted_p0_count": 0,
        "accepted_p1_count": 0,
        "accepted_in_scope_p2_count": 0,
        "required_scenario_union_complete": True,
        "evidence_level": "MULTI_AGENT_SIMULATED_REVIEW",
    }
    receipt = _attach_external_signature(
        context=context,
        component="MULTI_AGENT_PANEL",
        unsigned=unsigned,
        authority_signature=authority_signature,
    )
    _write_component(
        receipt=receipt,
        output_path=component_output,
        repository_root=context.repository_root,
    )
    return receipt


def build_sealed_agent_blind_component(
    *,
    repository_root: Path,
    receipt_path: Path,
    score_input_manifest_path: Path,
    deterministic_score_receipt_path: Path,
    mint_receipt_path: Path,
    thresholds_repository_path: str,
    scorer_repository_path: str,
    custody_registry_path: Path,
    verification_output: Path,
    component_output: Path,
    authority_signature: DetachedAuthoritySignature,
) -> SealedAgentBlindGateReceipt:
    context = _BuildContext(repository_root)
    thresholds = _repository_relative_path(
        thresholds_repository_path,
        "sealed thresholds",
    ).as_posix()
    scorer = _repository_relative_path(
        scorer_repository_path,
        "sealed scorer",
    ).as_posix()
    verified = verify_sealed_agent_blind(
        receipt_path=receipt_path,
        repository_root=context.repository_root,
        thresholds_path=context.repository_root / thresholds,
        score_input_manifest_path=score_input_manifest_path,
        deterministic_score_receipt_path=deterministic_score_receipt_path,
        scorer_path=context.repository_root / scorer,
        custody_registry_path=custody_registry_path,
        mint_receipt_path=mint_receipt_path,
    )
    verification = SealedBlindVerificationReceipt.model_validate(
        {
            **verified,
            "receipt_path": str(receipt_path),
            "thresholds_repository_path": thresholds,
            "score_input_manifest_path": str(score_input_manifest_path),
            "deterministic_score_receipt_path": str(
                deterministic_score_receipt_path
            ),
            "scorer_repository_path": scorer,
            "mint_receipt_path": str(mint_receipt_path),
        }
    )
    verification_snapshot = _write_json(
        verification_output,
        verification.model_dump(mode="json"),
        context.repository_root,
    )
    unsigned = {
        "schema_version": "sealed-agent-blind-gate-receipt-v2",
        **context.common_unsigned(
            component="SEALED_AGENT_BLIND",
            verification_path=verification_output,
            verification_sha256=verification_snapshot.sha256,
        ),
        "receipt_path": verification.receipt_path,
        "score_input_manifest_path": verification.score_input_manifest_path,
        "deterministic_score_receipt_path": (
            verification.deterministic_score_receipt_path
        ),
        "mint_receipt_path": verification.mint_receipt_path,
        "thresholds_repository_path": verification.thresholds_repository_path,
        "scorer_repository_path": verification.scorer_repository_path,
        "custody_registry_identity_sha256": (
            verification.custody_registry_identity_sha256
        ),
        "mint_receipt_sha256": verification.mint_receipt_sha256,
        "attempt_receipt_sha256": verification.receipt_sha256,
        "score_input_manifest_sha256": verification.score_input_manifest_sha256,
        "score_receipt_sha256": verification.score_receipt_sha256,
        "one_shot_nonce_sha256": verification.one_shot_nonce_sha256,
        "attempt_commitment_sha256": verification.attempt_commitment_sha256,
        "tranche_commitment_sha256": verification.tranche_commitment_sha256,
        "registry_state": "COMPLETED",
        "evidence_level": "SEALED_AGENT_BLIND",
    }
    receipt = _attach_external_signature(
        context=context,
        component="SEALED_AGENT_BLIND",
        unsigned=unsigned,
        authority_signature=authority_signature,
    )
    _write_component(
        receipt=receipt,
        output_path=component_output,
        repository_root=context.repository_root,
    )
    return receipt
