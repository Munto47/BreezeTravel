from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

from evals.agent_gate_v1.authority import (
    AnchoredAuthorityPolicy,
    load_current_goal_binding,
    require_scoped_goal,
)
from evals.agent_gate_v1.automation_isolation import (
    AutomationIsolationError,
    ensure_isolated_candidate_image,
    run_isolated_check,
)
from evals.agent_gate_v1.contracts import (
    AgentGateComponent,
    AutomatedProductExecutionManifest,
    AutomatedProductGateContract,
    AutomatedProductGateReceipt,
    AutomatedProductVerificationReceipt,
    LiveProviderGateReceipt,
    LiveProviderVerificationReceipt,
    MultiAgentPanelGateReceipt,
    MultiAgentPanelVerificationReceipt,
    SealedAgentBlindGateReceipt,
    SealedBlindVerificationReceipt,
    StrictComponentReceipt,
)
from evals.agent_gate_v1.host_tools import trusted_host_tool
from evals.agent_gate_v1.path_security import read_external_snapshot
from evals.agent_gate_v1.signing import unsigned_payload, verify_payload_signature
from evals.agent_gate_v1.validator import verify_review_panel, verify_sealed_agent_blind
from evals.trip_text_cards_agent_v2.annotations import (
    validate_inference_runtime_receipt_assets,
    validate_provider_receipt_assets,
)


class ComponentVerificationError(ValueError):
    pass


COMPONENT_SCHEMA_MODELS = {
    "automated-product-gate-receipt-v2": AutomatedProductGateReceipt,
    "live-provider-gate-receipt-v2": LiveProviderGateReceipt,
    "multi-agent-panel-gate-receipt-v2": MultiAgentPanelGateReceipt,
    "sealed-agent-blind-gate-receipt-v2": SealedAgentBlindGateReceipt,
}


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _repository_relative_path(value: str, label: str) -> PurePosixPath:
    if "\\" in value:
        raise ComponentVerificationError(f"{label} must use repository-relative POSIX form")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ComponentVerificationError(f"{label} is not a safe repository-relative path")
    return path


def _git_blob(root: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(
        [trusted_host_tool("git"), "-C", str(root), "show", f"{commit}:{path}"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ComponentVerificationError(f"candidate artifact is absent: {path}")
    return result.stdout


def load_strict_component_receipt(
    path: Path,
    repository_root: Path,
) -> tuple[object, StrictComponentReceipt]:
    snapshot = read_external_snapshot(path, repository_root)
    try:
        raw = json.loads(snapshot.content)
        model = COMPONENT_SCHEMA_MODELS.get(raw.get("schema_version"))
        if model is None:
            raise ComponentVerificationError("unknown or historical component receipt schema")
        receipt = model.model_validate(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, ComponentVerificationError):
            raise
        raise ComponentVerificationError(f"invalid strict component receipt: {exc}") from exc
    return snapshot, receipt


def _load_upstream(receipt, model_type, repository_root: Path):
    snapshot = read_external_snapshot(
        Path(receipt.verification_receipt_path),
        repository_root,
    )
    if snapshot.sha256 != receipt.verification_receipt_sha256:
        raise ComponentVerificationError("component upstream verification hash mismatch")
    try:
        value = model_type.model_validate_json(snapshot.content)
    except ValueError as exc:
        raise ComponentVerificationError(f"invalid component upstream receipt: {exc}") from exc
    return snapshot, value


def verify_automated_product_evidence(
    *,
    repository_root: Path,
    candidate_commit: str,
    candidate_tree: str,
    candidate_config_sha256: str,
    candidate_data_sha256: str,
    expected_goal_id: str,
    gate_contract_path: str,
    execution_manifest_path: Path,
) -> dict[str, object]:
    contract_relative = _repository_relative_path(
        gate_contract_path,
        "automated Gate contract",
    ).as_posix()
    contract_bytes = _git_blob(repository_root, candidate_commit, contract_relative)
    try:
        contract = AutomatedProductGateContract.model_validate_json(contract_bytes)
    except ValueError as exc:
        raise ComponentVerificationError(f"invalid automated Gate contract: {exc}") from exc
    manifest_snapshot = read_external_snapshot(execution_manifest_path, repository_root)
    try:
        manifest = AutomatedProductExecutionManifest.model_validate_json(
            manifest_snapshot.content
        )
    except ValueError as exc:
        raise ComponentVerificationError(
            f"invalid automated execution manifest: {exc}"
        ) from exc
    contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
    common = (
        expected_goal_id,
        candidate_commit,
        candidate_tree,
        candidate_config_sha256,
        candidate_data_sha256,
        contract_sha256,
    )
    observed = (
        manifest.goal_id,
        manifest.candidate_commit,
        manifest.candidate_tree,
        manifest.candidate_config_sha256,
        manifest.candidate_data_sha256,
        manifest.gate_contract_sha256,
    )
    if contract.goal_id != expected_goal_id or observed != common:
        raise ComponentVerificationError("automated evidence candidate or contract mismatch")
    isolation = contract.isolation
    runner_bindings = (
        (
            isolation.runner_recipe_path,
            isolation.runner_recipe_sha256,
        ),
        (
            isolation.runner_entrypoint_path,
            isolation.runner_entrypoint_sha256,
        ),
        (
            isolation.runner_context_policy_path,
            isolation.runner_context_policy_sha256,
        ),
    )
    if any(
        hashlib.sha256(
            _git_blob(repository_root, candidate_commit, path)
        ).hexdigest()
        != expected_sha256
        for path, expected_sha256 in runner_bindings
    ):
        raise ComponentVerificationError("automated runner bundle hash mismatch")
    if (
        manifest.verdict != "PASS"
        or manifest.isolation_mode != isolation.mode
        or manifest.runner_recipe_sha256 != isolation.runner_recipe_sha256
        or manifest.runner_entrypoint_sha256
        != isolation.runner_entrypoint_sha256
        or manifest.runner_context_policy_sha256
        != isolation.runner_context_policy_sha256
        or manifest.network_access
        or manifest.host_mount_count != 0
        or manifest.host_pid_namespace
        or not manifest.synthetic_profile
        or manifest.authority_secret_mount_count != 0
    ):
        raise ComponentVerificationError("automated execution isolation contract mismatch")
    if [item.check_id for item in manifest.checks] != [
        item.check_id for item in contract.checks
    ]:
        raise ComponentVerificationError("automated execution does not cover the contract exactly")
    try:
        ensure_isolated_candidate_image(
            repository_root=repository_root,
            expected_image_id=manifest.runner_image_id,
            image_archive_path=Path(manifest.runner_image_archive_path),
            image_archive_sha256=manifest.runner_image_archive_sha256,
            image_archive_size=manifest.runner_image_archive_size,
        )
    except AutomationIsolationError as exc:
        raise ComponentVerificationError(
            "isolated automated image recovery failed"
        ) from exc
    for check, execution in zip(contract.checks, manifest.checks, strict=True):
        if (
            execution.argv_sha256 != _canonical_sha256(check.argv)
            or execution.workdir != check.workdir
        ):
            raise ComponentVerificationError("automated execution command binding mismatch")
        try:
            rerun = run_isolated_check(
                repository_root=repository_root,
                expected_image_id=manifest.runner_image_id,
                workdir=check.workdir,
                argv=check.argv,
                timeout_seconds=check.timeout_seconds,
            )
        except AutomationIsolationError as exc:
            raise ComponentVerificationError(
                f"isolated automated readback failed: {check.check_id}"
            ) from exc
        if rerun.exit_code != 0:
            raise ComponentVerificationError(
                f"automated check failed during fresh readback: {check.check_id}"
            )
    return {
        "goal_id": expected_goal_id,
        "candidate_commit": candidate_commit,
        "candidate_tree": candidate_tree,
        "candidate_config_sha256": candidate_config_sha256,
        "candidate_data_sha256": candidate_data_sha256,
        "gate_contract_path": contract_relative,
        "gate_contract_sha256": contract_sha256,
        "isolation_mode": isolation.mode,
        "runner_recipe_sha256": isolation.runner_recipe_sha256,
        "runner_entrypoint_sha256": isolation.runner_entrypoint_sha256,
        "runner_context_policy_sha256": isolation.runner_context_policy_sha256,
        "runner_image_id": manifest.runner_image_id,
        "runner_image_archive_format": manifest.runner_image_archive_format,
        "runner_image_archive_path": manifest.runner_image_archive_path,
        "runner_image_archive_sha256": manifest.runner_image_archive_sha256,
        "runner_image_archive_size": manifest.runner_image_archive_size,
        "network_access": False,
        "host_mount_count": 0,
        "host_pid_namespace": False,
        "execution_manifest_path": str(execution_manifest_path),
        "execution_manifest_sha256": manifest_snapshot.sha256,
        "executed_check_count": len(contract.checks),
        "failed_check_count": 0,
        "checks_not_run": [],
        "verdict": "PASS",
    }


def _verify_common(
    *,
    receipt: StrictComponentReceipt,
    repository_root: Path,
    anchored_policy: AnchoredAuthorityPolicy,
    expected_goal_id: str,
    expected_candidate_commit: str,
    expected_candidate_tree: str,
    expected_config_sha256: str,
    expected_data_sha256: str,
) -> None:
    require_scoped_goal(anchored_policy.manifest, expected_goal_id)
    component: AgentGateComponent = receipt.component
    expected_verifier_path = anchored_policy.manifest.component_verifier_paths[component]
    verifier_bytes = _git_blob(
        repository_root,
        expected_candidate_commit,
        expected_verifier_path,
    )
    expected_common = (
        expected_goal_id,
        expected_candidate_commit,
        expected_candidate_tree,
        expected_config_sha256,
        expected_data_sha256,
        anchored_policy.sha256,
        expected_verifier_path,
        hashlib.sha256(verifier_bytes).hexdigest(),
    )
    actual_common = (
        receipt.goal_id,
        receipt.candidate_commit,
        receipt.candidate_tree,
        receipt.candidate_config_sha256,
        receipt.candidate_data_sha256,
        receipt.authority_policy_sha256,
        receipt.verifier_path,
        receipt.verifier_sha256,
    )
    if actual_common != expected_common:
        raise ComponentVerificationError("component candidate, policy, or verifier binding mismatch")
    verify_payload_signature(
        payload=unsigned_payload(receipt),
        signature=receipt.authority_signature,
        manifest=anchored_policy.manifest,
        expected_role=component,
    )


def verify_strict_component_receipt(
    *,
    receipt: StrictComponentReceipt,
    repository_root: Path,
    anchored_policy: AnchoredAuthorityPolicy,
    expected_goal_id: str,
    expected_candidate_commit: str,
    expected_candidate_tree: str,
    expected_config_sha256: str,
    expected_data_sha256: str,
    custody_registry_path: Path | None = None,
) -> None:
    _verify_common(
        receipt=receipt,
        repository_root=repository_root,
        anchored_policy=anchored_policy,
        expected_goal_id=expected_goal_id,
        expected_candidate_commit=expected_candidate_commit,
        expected_candidate_tree=expected_candidate_tree,
        expected_config_sha256=expected_config_sha256,
        expected_data_sha256=expected_data_sha256,
    )

    if isinstance(receipt, AutomatedProductGateReceipt):
        _snapshot, upstream = _load_upstream(
            receipt,
            AutomatedProductVerificationReceipt,
            repository_root,
        )
        if (
            receipt.gate_contract_path != upstream.gate_contract_path
            or receipt.execution_manifest_path != upstream.execution_manifest_path
        ):
            raise ComponentVerificationError("automated raw artifact paths disagree")
        goal_binding = load_current_goal_binding(
            repository_root,
            expected_candidate_commit,
            anchored_policy.manifest,
        )
        if (
            upstream.gate_contract_path
            != goal_binding.automated_gate_contract_path
            or upstream.gate_contract_sha256
            != goal_binding.automated_gate_contract_sha256
        ):
            raise ComponentVerificationError(
                "automated Gate contract is not the current Goal binding"
            )
        verified = verify_automated_product_evidence(
            repository_root=repository_root,
            candidate_commit=expected_candidate_commit,
            candidate_tree=expected_candidate_tree,
            candidate_config_sha256=expected_config_sha256,
            candidate_data_sha256=expected_data_sha256,
            expected_goal_id=expected_goal_id,
            gate_contract_path=upstream.gate_contract_path,
            execution_manifest_path=Path(upstream.execution_manifest_path),
        )
        expected = (
            receipt.gate_contract_sha256,
            receipt.execution_manifest_sha256,
            receipt.executed_check_count,
            receipt.failed_check_count,
            receipt.isolation_mode,
            receipt.runner_recipe_sha256,
            receipt.runner_entrypoint_sha256,
            receipt.runner_context_policy_sha256,
            receipt.runner_image_id,
            receipt.runner_image_archive_format,
            receipt.runner_image_archive_path,
            receipt.runner_image_archive_sha256,
            receipt.runner_image_archive_size,
            receipt.network_access,
            receipt.host_mount_count,
            receipt.host_pid_namespace,
        )
        actual = (
            verified["gate_contract_sha256"],
            verified["execution_manifest_sha256"],
            verified["executed_check_count"],
            verified["failed_check_count"],
            verified["isolation_mode"],
            verified["runner_recipe_sha256"],
            verified["runner_entrypoint_sha256"],
            verified["runner_context_policy_sha256"],
            verified["runner_image_id"],
            verified["runner_image_archive_format"],
            verified["runner_image_archive_path"],
            verified["runner_image_archive_sha256"],
            verified["runner_image_archive_size"],
            verified["network_access"],
            verified["host_mount_count"],
            verified["host_pid_namespace"],
        )
        upstream_actual = (
            upstream.gate_contract_sha256,
            upstream.execution_manifest_sha256,
            upstream.executed_check_count,
            upstream.failed_check_count,
            upstream.isolation_mode,
            upstream.runner_recipe_sha256,
            upstream.runner_entrypoint_sha256,
            upstream.runner_context_policy_sha256,
            upstream.runner_image_id,
            upstream.runner_image_archive_format,
            upstream.runner_image_archive_path,
            upstream.runner_image_archive_sha256,
            upstream.runner_image_archive_size,
            upstream.network_access,
            upstream.host_mount_count,
            upstream.host_pid_namespace,
        )
        if actual != expected or actual != upstream_actual:
            raise ComponentVerificationError("automated component contradicts raw execution")
        return

    if isinstance(receipt, LiveProviderGateReceipt):
        raise ComponentVerificationError(
            "formal live Provider Gate is NOT_RUN until the custody-minted "
            "direct-HTTPS capture execution receipt is implemented"
        )
        _snapshot, upstream = _load_upstream(
            receipt,
            LiveProviderVerificationReceipt,
            repository_root,
        )
        raw_paths = (
            receipt.amap_provider_receipt_index_path,
            receipt.amap_runtime_receipt_path,
            receipt.qwen_runtime_receipt_path,
        )
        if raw_paths != (
            upstream.amap_provider_receipt_index_path,
            upstream.amap_runtime_receipt_path,
            upstream.qwen_runtime_receipt_path,
        ):
            raise ComponentVerificationError("live Provider raw artifact paths disagree")
        try:
            _index, amap_runtime, amap_verified = validate_provider_receipt_assets(
                split=upstream.split,
                provider_receipt_index_path=Path(
                    upstream.amap_provider_receipt_index_path
                ),
                provider_runtime_receipt_bundle_path=Path(
                    upstream.amap_runtime_receipt_path
                ),
                repository_root=repository_root,
                expected_candidate_commit=expected_candidate_commit,
                expected_candidate_tree=expected_candidate_tree,
                expected_goal_id=expected_goal_id,
                expected_provider_binding_sha256=(
                    upstream.amap_provider_binding_sha256
                ),
                expected_runtime_receipt_bundle_sha256=(
                    upstream.amap_runtime_receipt_sha256
                ),
                expected_database_export_receipt_sha256=(
                    upstream.amap_database_export_receipt_sha256
                ),
                expected_provider_http_receipt_bundle_sha256=(
                    upstream.amap_http_receipt_bundle_sha256
                ),
                require_live_provider_evidence=True,
            )
            qwen_runtime, qwen_verified = validate_inference_runtime_receipt_assets(
                inference_runtime_receipt_bundle_path=Path(
                    upstream.qwen_runtime_receipt_path
                ),
                repository_root=repository_root,
                expected_candidate_commit=expected_candidate_commit,
                expected_candidate_tree=expected_candidate_tree,
                expected_goal_id=expected_goal_id,
                require_live_provider_evidence=True,
            )
        except ValueError as exc:
            raise ComponentVerificationError(str(exc)) from exc
        verified = (
            amap_verified["provider_receipt_index_sha256"],
            amap_verified["provider_runtime_receipt_bundle_sha256"],
            qwen_verified["inference_runtime_receipt_bundle_sha256"],
            len(amap_runtime.effects),
            len(qwen_runtime.effects),
            0,
        )
        strict_values = (
            receipt.amap_provider_receipt_index_sha256,
            receipt.amap_runtime_receipt_sha256,
            receipt.qwen_runtime_receipt_sha256,
            receipt.amap_live_effect_count,
            receipt.qwen_live_effect_count,
            receipt.fixture_effect_count,
        )
        upstream_values = (
            upstream.amap_provider_receipt_index_sha256,
            upstream.amap_runtime_receipt_sha256,
            upstream.qwen_runtime_receipt_sha256,
            upstream.amap_live_effect_count,
            upstream.qwen_live_effect_count,
            upstream.fixture_effect_count,
        )
        if (
            upstream.goal_id != expected_goal_id
            or upstream.candidate_commit != expected_candidate_commit
            or upstream.candidate_tree != expected_candidate_tree
            or upstream.authority_policy_sha256 != anchored_policy.sha256
            or upstream.split != receipt.split
            or upstream.amap_provider_binding_sha256
            != receipt.amap_provider_binding_sha256
            or upstream.amap_database_export_receipt_sha256
            != receipt.amap_database_export_receipt_sha256
            or upstream.amap_http_receipt_bundle_sha256
            != receipt.amap_http_receipt_bundle_sha256
            or verified != strict_values
            or verified != upstream_values
        ):
            raise ComponentVerificationError("live Provider component contradicts raw evidence")
        return

    if isinstance(receipt, MultiAgentPanelGateReceipt):
        _snapshot, upstream = _load_upstream(
            receipt,
            MultiAgentPanelVerificationReceipt,
            repository_root,
        )
        if (
            receipt.review_paths != upstream.review_paths
            or receipt.adjudication_path != upstream.adjudication_path
            or receipt.expected_input_bundle_sha256
            != upstream.expected_input_bundle_sha256
        ):
            raise ComponentVerificationError("panel raw artifact bindings disagree")
        try:
            verified = verify_review_panel(
                review_paths=[Path(value) for value in upstream.review_paths],
                adjudication_path=Path(upstream.adjudication_path),
                repository_root=repository_root,
                expected_goal_id=expected_goal_id,
                expected_candidate_commit=expected_candidate_commit,
                expected_candidate_tree=expected_candidate_tree,
                expected_candidate_config_sha256=expected_config_sha256,
                expected_candidate_data_sha256=expected_data_sha256,
                expected_input_bundle_sha256=upstream.expected_input_bundle_sha256,
            )
        except ValueError as exc:
            raise ComponentVerificationError(str(exc)) from exc
        verified_values = (
            verified["review_sha256"],
            verified["adjudication_sha256"],
            verified["accepted_p0_count"],
            verified["accepted_p1_count"],
            verified["accepted_in_scope_p2_count"],
            verified["roles_complete"],
            verified["verdict"],
        )
        strict_values = (
            sorted(receipt.review_receipt_sha256),
            receipt.adjudication_receipt_sha256,
            receipt.accepted_p0_count,
            receipt.accepted_p1_count,
            receipt.accepted_in_scope_p2_count,
            receipt.required_scenario_union_complete,
            receipt.verdict,
        )
        upstream_values = (
            sorted(upstream.review_sha256),
            upstream.adjudication_sha256,
            upstream.accepted_p0_count,
            upstream.accepted_p1_count,
            upstream.accepted_in_scope_p2_count,
            upstream.required_scenario_union_complete,
            upstream.verdict,
        )
        if verified_values != strict_values or verified_values != upstream_values:
            raise ComponentVerificationError("panel component contradicts raw reviews")
        return

    if custody_registry_path is None:
        raise ComponentVerificationError("sealed component requires the pinned custody registry")
    _snapshot, upstream = _load_upstream(
        receipt,
        SealedBlindVerificationReceipt,
        repository_root,
    )
    raw_paths = (
        receipt.receipt_path,
        receipt.score_input_manifest_path,
        receipt.deterministic_score_receipt_path,
        receipt.mint_receipt_path,
        receipt.thresholds_repository_path,
        receipt.scorer_repository_path,
    )
    if raw_paths != (
        upstream.receipt_path,
        upstream.score_input_manifest_path,
        upstream.deterministic_score_receipt_path,
        upstream.mint_receipt_path,
        upstream.thresholds_repository_path,
        upstream.scorer_repository_path,
    ):
        raise ComponentVerificationError("sealed raw artifact bindings disagree")
    thresholds_relative = _repository_relative_path(
        upstream.thresholds_repository_path,
        "sealed thresholds",
    )
    scorer_relative = _repository_relative_path(
        upstream.scorer_repository_path,
        "sealed scorer",
    )
    try:
        verified = verify_sealed_agent_blind(
            receipt_path=Path(upstream.receipt_path),
            repository_root=repository_root,
            thresholds_path=repository_root / Path(thresholds_relative.as_posix()),
            score_input_manifest_path=Path(upstream.score_input_manifest_path),
            deterministic_score_receipt_path=Path(
                upstream.deterministic_score_receipt_path
            ),
            scorer_path=repository_root / Path(scorer_relative.as_posix()),
            custody_registry_path=custody_registry_path,
            mint_receipt_path=Path(upstream.mint_receipt_path),
        )
    except ValueError as exc:
        raise ComponentVerificationError(str(exc)) from exc
    verified_values = (
        verified["candidate_commit"],
        verified["candidate_tree"],
        verified["authority_anchor_commit"],
        verified["authority_policy_sha256"],
        verified["receipt_sha256"],
        verified["score_input_manifest_sha256"],
        verified["score_receipt_sha256"],
        verified["mint_receipt_sha256"],
        verified["custody_registry_identity_sha256"],
        verified["registry_state"],
        verified["tranche_commitment_sha256"],
        verified["one_shot_nonce_sha256"],
        verified["attempt_commitment_sha256"],
        verified["verdict"],
    )
    upstream_values = (
        upstream.candidate_commit,
        upstream.candidate_tree,
        upstream.authority_anchor_commit,
        upstream.authority_policy_sha256,
        upstream.receipt_sha256,
        upstream.score_input_manifest_sha256,
        upstream.score_receipt_sha256,
        upstream.mint_receipt_sha256,
        upstream.custody_registry_identity_sha256,
        upstream.registry_state,
        upstream.tranche_commitment_sha256,
        upstream.one_shot_nonce_sha256,
        upstream.attempt_commitment_sha256,
        upstream.verdict,
    )
    strict_values = (
        receipt.candidate_commit,
        receipt.candidate_tree,
        upstream.authority_anchor_commit,
        receipt.authority_policy_sha256,
        receipt.attempt_receipt_sha256,
        receipt.score_input_manifest_sha256,
        receipt.score_receipt_sha256,
        receipt.mint_receipt_sha256,
        receipt.custody_registry_identity_sha256,
        receipt.registry_state,
        receipt.tranche_commitment_sha256,
        receipt.one_shot_nonce_sha256,
        receipt.attempt_commitment_sha256,
        receipt.verdict,
    )
    if verified_values != upstream_values or verified_values != strict_values:
        raise ComponentVerificationError("sealed component contradicts raw custody evidence")
