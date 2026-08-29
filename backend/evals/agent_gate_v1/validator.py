from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from evals.agent_gate_v1.contracts import (
    AgentGateAdjudicationReceipt,
    AgentGateReviewReceipt,
    EvidenceRef,
    ReviewerRole,
    SealedScoreInputManifest,
    SealedAgentBlindReceipt,
    SealedAgentBlindScoreReceipt,
    SealedAgentBlindThresholds,
)
from evals.agent_gate_v1.host_tools import trusted_host_tool
from evals.agent_gate_v1.path_security import ArtifactSnapshot, read_external_snapshot
from evals.agent_gate_v1.sealed_score import evaluate_frozen_thresholds


class AgentGateValidationError(ValueError):
    pass


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_snapshot(snapshot: ArtifactSnapshot, model_type):
    try:
        return model_type.model_validate_json(snapshot.content)
    except ValueError as exc:
        raise AgentGateValidationError(
            f"invalid agent gate artifact {snapshot.path.name}: {exc}"
        ) from exc


def _git_tree(repository_root: Path, commit: str) -> str:
    result = subprocess.run(
        [trusted_host_tool("git"), "-C", str(repository_root), "show", "-s", "--format=%T", commit],
        check=False,
        capture_output=True,
        text=True,
    )
    tree = result.stdout.strip()
    if result.returncode != 0 or len(tree) != 40:
        raise AgentGateValidationError("candidate commit does not exist in the repository")
    return tree


def _git_blob(repository_root: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(
        [trusted_host_tool("git"), "-C", str(repository_root), "show", f"{commit}:{path}"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AgentGateValidationError(f"candidate evidence blob is absent: {path}")
    return result.stdout


def _git_blob_sha256(repository_root: Path, commit: str, path: str) -> str:
    return hashlib.sha256(_git_blob(repository_root, commit, path)).hexdigest()


def _verify_evidence_ref(
    reference: EvidenceRef,
    repository_root: Path,
    candidate_commit: str,
    external_cache: dict[str, ArtifactSnapshot],
) -> None:
    raw = Path(reference.artifact_path)
    if reference.storage == "REPOSITORY":
        if raw.is_absolute() or ".." in raw.parts or "\\" in reference.artifact_path:
            raise AgentGateValidationError("repository evidence path is not safe and relative")
        observed_sha256 = _git_blob_sha256(
            repository_root,
            candidate_commit,
            raw.as_posix(),
        )
    else:
        if not raw.is_absolute():
            raise AgentGateValidationError("external evidence path must be absolute")
        cache_key = str(raw)
        snapshot = external_cache.get(cache_key)
        if snapshot is None:
            snapshot = read_external_snapshot(raw, repository_root)
            external_cache[cache_key] = snapshot
        observed_sha256 = snapshot.sha256
    if observed_sha256 != reference.sha256:
        raise AgentGateValidationError("evidence artifact hash mismatch")


def verify_review_panel(
    *,
    review_paths: list[Path],
    adjudication_path: Path,
    repository_root: Path,
    expected_goal_id: str,
    expected_candidate_commit: str,
    expected_candidate_tree: str,
    expected_candidate_config_sha256: str,
    expected_candidate_data_sha256: str,
    expected_input_bundle_sha256: dict[ReviewerRole, str],
) -> dict[str, Any]:
    if len(review_paths) != 3:
        raise AgentGateValidationError("exactly three reviewer artifacts are required")
    review_snapshots = [
        read_external_snapshot(path, repository_root) for path in review_paths
    ]
    reviews = [
        _load_snapshot(snapshot, AgentGateReviewReceipt)
        for snapshot in review_snapshots
    ]
    if _git_tree(repository_root, expected_candidate_commit) != expected_candidate_tree:
        raise AgentGateValidationError("expected candidate tree does not match the Git commit")
    contract_root = "backend/eval_data/agent_gate_v1"
    role_prompt = {
        "PRODUCT_UX": "product_ux.md",
        "SEMANTIC_DOMAIN": "semantic_domain.md",
        "RELIABILITY_SECURITY": "reliability_security.md",
    }
    review_schema_sha256 = _git_blob_sha256(
        repository_root,
        expected_candidate_commit,
        f"{contract_root}/review.schema.json",
    )
    evidence_cache: dict[str, ArtifactSnapshot] = {}
    for review in reviews:
        if review.goal_id != expected_goal_id:
            raise AgentGateValidationError("review goal binding mismatch")
        if review.candidate_commit != expected_candidate_commit:
            raise AgentGateValidationError("review candidate commit binding mismatch")
        if review.candidate_tree != expected_candidate_tree:
            raise AgentGateValidationError("review candidate tree binding mismatch")
        if review.candidate_config_sha256 != expected_candidate_config_sha256:
            raise AgentGateValidationError("review candidate config binding mismatch")
        if review.candidate_data_sha256 != expected_candidate_data_sha256:
            raise AgentGateValidationError("review candidate data binding mismatch")
        if review.attestation.input_bundle_sha256 != expected_input_bundle_sha256[review.reviewer_role]:
            raise AgentGateValidationError("review input bundle hash mismatch")
        expected_prompt_sha256 = _git_blob_sha256(
            repository_root,
            expected_candidate_commit,
            f"{contract_root}/prompts/{role_prompt[review.reviewer_role]}",
        )
        if review.attestation.prompt_sha256 != expected_prompt_sha256:
            raise AgentGateValidationError("review prompt hash binding mismatch")
        if review.attestation.output_schema_sha256 != review_schema_sha256:
            raise AgentGateValidationError("review output schema hash binding mismatch")
        for scenario in review.scenario_coverage.__dict__.values():
            for evidence in scenario.evidence:
                _verify_evidence_ref(
                    evidence,
                    repository_root,
                    expected_candidate_commit,
                    evidence_cache,
                )
        for finding in review.findings:
            for evidence in finding.evidence:
                _verify_evidence_ref(
                    evidence,
                    repository_root,
                    expected_candidate_commit,
                    evidence_cache,
                )
    expected_roles: set[ReviewerRole] = {
        "PRODUCT_UX",
        "SEMANTIC_DOMAIN",
        "RELIABILITY_SECURITY",
    }
    if {item.reviewer_role for item in reviews} != expected_roles:
        raise AgentGateValidationError("review panel must contain the three frozen roles")
    if len({item.attestation.task_id for item in reviews}) != 3:
        raise AgentGateValidationError("reviewers must use distinct isolated tasks")
    if len({item.candidate_commit for item in reviews}) != 1:
        raise AgentGateValidationError("reviewers must bind the same candidate commit")
    if any(item.verdict != "PASS" for item in reviews):
        raise AgentGateValidationError("every reviewer must pass before adjudication can pass")

    adjudication_snapshot = read_external_snapshot(adjudication_path, repository_root)
    adjudication = _load_snapshot(
        adjudication_snapshot,
        AgentGateAdjudicationReceipt,
    )
    review_hashes = {snapshot.sha256 for snapshot in review_snapshots}
    if set(adjudication.source_review_sha256) != review_hashes:
        raise AgentGateValidationError("adjudication review hash binding mismatch")
    if adjudication.candidate_commit != reviews[0].candidate_commit:
        raise AgentGateValidationError("adjudication candidate binding mismatch")
    if adjudication.goal_id != expected_goal_id:
        raise AgentGateValidationError("adjudication goal binding mismatch")
    if adjudication.candidate_tree != expected_candidate_tree:
        raise AgentGateValidationError("adjudication candidate tree binding mismatch")
    if adjudication.candidate_config_sha256 != expected_candidate_config_sha256:
        raise AgentGateValidationError("adjudication candidate config binding mismatch")
    if adjudication.candidate_data_sha256 != expected_candidate_data_sha256:
        raise AgentGateValidationError("adjudication candidate data binding mismatch")
    if not adjudication.required_scenario_union_complete:
        raise AgentGateValidationError("adjudication scenario union is incomplete")
    if adjudication.attestation.task_id in {item.attestation.task_id for item in reviews}:
        raise AgentGateValidationError("adjudicator must use a fresh task")
    if adjudication.attestation.started_at < max(item.attestation.frozen_at for item in reviews):
        raise AgentGateValidationError("adjudication cannot start before all reviews are frozen")
    if adjudication.attestation.prompt_sha256 != _git_blob_sha256(
        repository_root,
        expected_candidate_commit,
        f"{contract_root}/prompts/gate_adjudicator.md",
    ):
        raise AgentGateValidationError("adjudication prompt hash binding mismatch")
    if adjudication.attestation.output_schema_sha256 != _git_blob_sha256(
        repository_root,
        expected_candidate_commit,
        f"{contract_root}/adjudication.schema.json",
    ):
        raise AgentGateValidationError("adjudication output schema hash binding mismatch")
    if adjudication.attestation.input_bundle_sha256 != _canonical_sha256(
        {
            "goal_id": expected_goal_id,
            "candidate_commit": expected_candidate_commit,
            "candidate_tree": expected_candidate_tree,
            "candidate_config_sha256": expected_candidate_config_sha256,
            "candidate_data_sha256": expected_candidate_data_sha256,
            "source_review_sha256": sorted(review_hashes),
        }
    ):
        raise AgentGateValidationError("adjudication input bundle hash mismatch")

    review_by_hash = {
        snapshot.sha256: review
        for snapshot, review in zip(review_snapshots, reviews, strict=True)
    }
    expected_findings = {
        (review_hash, finding.finding_id): finding
        for review_hash, review in review_by_hash.items()
        for finding in review.findings
    }
    actual_findings = {
        (finding.source_review_sha256, finding.finding_id): finding
        for finding in adjudication.findings
    }
    if set(actual_findings) != set(expected_findings):
        raise AgentGateValidationError("adjudication must resolve every review finding exactly once")
    for key, finding in actual_findings.items():
        if finding.severity != expected_findings[key].severity:
            raise AgentGateValidationError("adjudication finding severity mismatch")
    return {
        "schema_version": "agent-gate-panel-verification-receipt-v1",
        "goal_id": expected_goal_id,
        "candidate_commit": adjudication.candidate_commit,
        "candidate_tree": adjudication.candidate_tree,
        "candidate_config_sha256": expected_candidate_config_sha256,
        "candidate_data_sha256": expected_candidate_data_sha256,
        "review_count": 3,
        "roles_complete": True,
        "review_sha256": sorted(review_hashes),
        "adjudication_sha256": adjudication_snapshot.sha256,
        "accepted_p0_count": adjudication.accepted_p0_count,
        "accepted_p1_count": adjudication.accepted_p1_count,
        "accepted_in_scope_p2_count": adjudication.accepted_in_scope_p2_count,
        "evidence_level": "MULTI_AGENT_SIMULATED_REVIEW",
        "human_evidence": False,
        "verdict": adjudication.verdict,
    }


def verify_sealed_agent_blind(
    *,
    receipt_path: Path,
    repository_root: Path,
    thresholds_path: Path,
    score_input_manifest_path: Path,
    deterministic_score_receipt_path: Path,
    scorer_path: Path,
    custody_registry_path: Path,
    mint_receipt_path: Path,
) -> dict[str, Any]:
    from evals.agent_gate_v1.authority import load_anchored_authority_policy
    from evals.agent_gate_v1.custody import claim_attempt_receipt, read_run_state
    from evals.agent_gate_v1.signing import unsigned_payload, verify_payload_signature

    mint_preview = read_external_snapshot(mint_receipt_path, repository_root)
    try:
        from evals.agent_gate_v1.contracts import SealedAgentBlindMintReceipt

        preview = SealedAgentBlindMintReceipt.model_validate_json(mint_preview.content)
    except ValueError as exc:
        raise AgentGateValidationError(f"invalid sealed blind mint receipt: {exc}") from exc
    anchored = load_anchored_authority_policy(repository_root, preview.candidate_commit)
    if _git_tree(repository_root, preview.candidate_commit) != preview.candidate_tree:
        raise AgentGateValidationError("sealed blind candidate tree does not match Git")
    mint = preview
    verify_payload_signature(
        payload=unsigned_payload(mint),
        signature=mint.authority_signature,
        manifest=anchored.manifest,
        expected_role="SEALED_CUSTODY",
    )
    if mint.authority_policy_sha256 != anchored.sha256:
        raise AgentGateValidationError("sealed blind mint authority policy mismatch")
    state = read_run_state(
        registry_path=custody_registry_path,
        repository_root=repository_root,
        manifest=anchored.manifest,
        one_shot_nonce_sha256=mint.one_shot_nonce_sha256,
    )
    if state["state"] != "COMPLETED":
        raise AgentGateValidationError("sealed scorer did not complete the consumed run")
    attempt_commitment = state["attempt_commitment_sha256"]
    if not isinstance(attempt_commitment, str):
        raise AgentGateValidationError("sealed run has no frozen attempt commitment")
    receipt_snapshot = read_external_snapshot(receipt_path, repository_root)
    claim_attempt_receipt(
        registry_path=custody_registry_path,
        repository_root=repository_root,
        manifest=anchored.manifest,
        mint=mint,
        attempt_commitment_sha256=attempt_commitment,
        attempt_receipt_sha256=receipt_snapshot.sha256,
    )
    state = read_run_state(
        registry_path=custody_registry_path,
        repository_root=repository_root,
        manifest=anchored.manifest,
        one_shot_nonce_sha256=mint.one_shot_nonce_sha256,
    )
    try:
        receipt = SealedAgentBlindReceipt.model_validate_json(
            receipt_snapshot.content
        )
    except ValueError as exc:
        raise AgentGateValidationError(f"invalid sealed blind attempt: {exc}") from exc
    verify_payload_signature(
        payload=unsigned_payload(receipt),
        signature=receipt.authority_signature,
        manifest=anchored.manifest,
        expected_role="SEALED_CUSTODY",
    )
    common_bindings = (
        receipt.goal_id,
        receipt.candidate_commit,
        receipt.candidate_tree,
        receipt.tranche_commitment_sha256,
        receipt.one_shot_nonce_sha256,
        receipt.attempt_commitment_sha256,
        receipt.custody_registry_identity_sha256,
        receipt.mint_receipt_sha256,
        receipt.prompt_sha256,
        receipt.schema_sha256,
        receipt.thresholds_sha256,
        receipt.config_sha256,
        receipt.provider_binding_sha256,
        receipt.custodian_task_id,
        receipt.authority_policy_sha256,
    )
    mint_bindings = (
        mint.goal_id,
        mint.candidate_commit,
        mint.candidate_tree,
        mint.tranche_commitment_sha256,
        mint.one_shot_nonce_sha256,
        attempt_commitment,
        mint.custody_registry_identity_sha256,
        mint_preview.sha256,
        mint.prompt_sha256,
        mint.schema_sha256,
        mint.thresholds_sha256,
        mint.config_sha256,
        mint.provider_binding_sha256,
        mint.custodian_task_id,
        mint.authority_policy_sha256,
    )
    if common_bindings != mint_bindings:
        raise AgentGateValidationError("sealed blind attempt does not match its signed mint")

    expected_thresholds = (
        repository_root
        / "backend/eval_data/trip_text_cards_agent_v2/sealed_blind_thresholds.json"
    ).resolve(strict=True)
    if thresholds_path.resolve(strict=True) != expected_thresholds:
        raise AgentGateValidationError("sealed blind thresholds path is not canonical")
    thresholds_bytes = _git_blob(
        repository_root,
        mint.candidate_commit,
        "backend/eval_data/trip_text_cards_agent_v2/sealed_blind_thresholds.json",
    )
    if hashlib.sha256(thresholds_bytes).hexdigest() != mint.thresholds_sha256:
        raise AgentGateValidationError("sealed blind threshold artifact hash mismatch")
    thresholds = SealedAgentBlindThresholds.model_validate_json(thresholds_bytes)
    if thresholds.goal_id != mint.goal_id:
        raise AgentGateValidationError("sealed blind threshold goal mismatch")

    score_input_snapshot = read_external_snapshot(
        score_input_manifest_path,
        repository_root,
    )
    score_snapshot = read_external_snapshot(
        deterministic_score_receipt_path,
        repository_root,
    )
    if score_input_snapshot.sha256 != receipt.score_input_manifest_sha256:
        raise AgentGateValidationError("sealed score input manifest hash mismatch")
    if score_snapshot.sha256 != receipt.deterministic_score_receipt_sha256:
        raise AgentGateValidationError("sealed blind deterministic score receipt hash mismatch")
    try:
        score_input = SealedScoreInputManifest.model_validate_json(
            score_input_snapshot.content
        )
        score = SealedAgentBlindScoreReceipt.model_validate_json(score_snapshot.content)
    except ValueError as exc:
        raise AgentGateValidationError(f"invalid sealed score artifact: {exc}") from exc
    for artifact in (score_input, score):
        if artifact.authority_policy_sha256 != anchored.sha256:
            raise AgentGateValidationError("sealed score authority policy mismatch")
        verify_payload_signature(
            payload=unsigned_payload(artifact),
            signature=artifact.authority_signature,
            manifest=anchored.manifest,
            expected_role="SEALED_CUSTODY",
        )
    expected_scorer = (
        repository_root / "backend/scripts/score_g01_sealed_agent_blind.py"
    ).resolve(strict=True)
    if scorer_path.resolve(strict=True) != expected_scorer:
        raise AgentGateValidationError("sealed blind scorer path is not canonical")
    if _git_blob_sha256(
        repository_root,
        mint.candidate_commit,
        "backend/scripts/score_g01_sealed_agent_blind.py",
    ) != mint.scorer_sha256:
        raise AgentGateValidationError("sealed blind scorer artifact hash mismatch")
    input_bindings = (
        score_input.goal_id,
        score_input.candidate_commit,
        score_input.candidate_tree,
        score_input.tranche_commitment_sha256,
        score_input.one_shot_nonce_sha256,
        score_input.attempt_commitment_sha256,
        score_input.scorer_sha256,
        score_input.thresholds_sha256,
    )
    if input_bindings != (
        mint.goal_id,
        mint.candidate_commit,
        mint.candidate_tree,
        mint.tranche_commitment_sha256,
        mint.one_shot_nonce_sha256,
        attempt_commitment,
        mint.scorer_sha256,
        mint.thresholds_sha256,
    ):
        raise AgentGateValidationError("sealed score input does not match its mint")
    score_bindings = (
        score.goal_id,
        score.candidate_commit,
        score.candidate_tree,
        score.tranche_commitment_sha256,
        score.thresholds_sha256,
        score.scorer_sha256,
        score.score_input_manifest_sha256,
        score.input_bundle_sha256,
        score.prediction_bundle_sha256,
        score.truth_bundle_commitment,
        score.case_set_commitment_sha256,
        score.scored_case_count,
        score.custody_registry_identity_sha256,
        score.mint_receipt_sha256,
        score.one_shot_nonce_sha256,
        score.attempt_commitment_sha256,
    )
    expected_score_bindings = (
        mint.goal_id,
        mint.candidate_commit,
        mint.candidate_tree,
        mint.tranche_commitment_sha256,
        mint.thresholds_sha256,
        mint.scorer_sha256,
        score_input_snapshot.sha256,
        score_input.input_bundle_sha256,
        score_input.prediction_bundle_sha256,
        score_input.truth_bundle_commitment,
        score_input.case_set_commitment_sha256,
        score_input.input_case_count,
        mint.custody_registry_identity_sha256,
        mint_preview.sha256,
        mint.one_shot_nonce_sha256,
        attempt_commitment,
    )
    if score_bindings != expected_score_bindings:
        raise AgentGateValidationError("sealed deterministic score binding mismatch")
    if score.completed_at > receipt.completed_at:
        raise AgentGateValidationError("sealed blind receipt predates deterministic scoring")
    if (
        score.aggregate_metrics != receipt.aggregate_metrics
        or score.taxonomy_counts != receipt.taxonomy_counts
        or score.input_bundle_sha256 != receipt.input_bundle_sha256
        or score.prediction_bundle_sha256 != receipt.prediction_bundle_sha256
        or score.truth_bundle_commitment != receipt.truth_bundle_commitment
        or score.case_set_commitment_sha256 != receipt.case_set_commitment_sha256
        or score.scored_case_count != receipt.scored_case_count
        or score.attempt_commitment_sha256
        != receipt.attempt_commitment_sha256
    ):
        raise AgentGateValidationError("sealed blind aggregate metrics do not match scorer output")
    if set(score.aggregate_metrics) != set(thresholds.required_metric_names):
        raise AgentGateValidationError("sealed score metric set is not the frozen complete set")

    deterministic_pass = evaluate_frozen_thresholds(
        metrics=score.aggregate_metrics,
        thresholds=thresholds,
    )
    if score.required_gate_metrics_passed != deterministic_pass:
        raise AgentGateValidationError("sealed blind scorer result contradicts frozen thresholds")
    if receipt.required_gate_metrics_passed != deterministic_pass:
        raise AgentGateValidationError("sealed blind verdict contradicts deterministic thresholds")
    expected_verdict = "PASS" if deterministic_pass else "FAIL"
    if receipt.verdict != expected_verdict:
        raise AgentGateValidationError("sealed blind verdict does not match deterministic scoring")

    if (
        state["score_input_manifest_sha256"] != score_input_snapshot.sha256
        or state["score_receipt_sha256"] != score_snapshot.sha256
        or state["mint_receipt_sha256"] != mint_preview.sha256
        or state["attempt_receipt_sha256"] != receipt_snapshot.sha256
    ):
        raise AgentGateValidationError("sealed registry completion bindings disagree")

    return {
        "schema_version": "sealed-agent-blind-verification-receipt-v2",
        "candidate_commit": receipt.candidate_commit,
        "candidate_tree": receipt.candidate_tree,
        "authority_anchor_commit": anchored.anchor_commit,
        "authority_policy_sha256": anchored.sha256,
        "receipt_sha256": receipt_snapshot.sha256,
        "score_input_manifest_sha256": score_input_snapshot.sha256,
        "score_receipt_sha256": score_snapshot.sha256,
        "mint_receipt_sha256": mint_preview.sha256,
        "custody_registry_identity_sha256": (
            anchored.manifest.custody_registry_identity_sha256
        ),
        "registry_state": "COMPLETED",
        "tranche_commitment_sha256": receipt.tranche_commitment_sha256,
        "one_shot_nonce_sha256": receipt.one_shot_nonce_sha256,
        "attempt_commitment_sha256": receipt.attempt_commitment_sha256,
        "evidence_level": "SEALED_AGENT_BLIND",
        "human_evidence": False,
        "verdict": receipt.verdict,
    }
