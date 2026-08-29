from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.agent_gate_v1.contracts import (
    AgentGatePassReceipt,
    AutomatedCheckExecution,
    AutomatedProductExecutionManifest,
    AutomatedProductGateContract,
    CurrentGoalBinding,
    MultiAgentPanelVerificationReceipt,
    SealedAgentBlindReceipt,
)
from evals.agent_gate_v1.path_security import (
    read_external_snapshot,
    write_external_bytes_exclusive,
)
from evals.agent_gate_v1.validator import AgentGateValidationError, verify_review_panel


class CoreAgentGateError(ValueError):
    pass


CORE_CONFIG_ROOTS = (
    "AGENTS.md",
    ".github/workflows/ci.yml",
    "backend/app",
    "backend/evals",
    "backend/scripts",
    "backend/pyproject.toml",
    "backend/pytest.ini",
    "backend/requirements-base.txt",
    "backend/requirements-dev.txt",
    "backend/requirements.txt",
    "docs",
    "frontend",
    "miniapp",
    "packages",
)
CORE_DATA_ROOTS = ("backend/eval_data",)
CORE_FROZEN_BINDING_PATHS = {
    "model": "backend/eval_data/trip_text_cards_agent_v2/qwen_model_panel.json",
    "prompt": "backend/eval_data/trip_text_cards_agent_v2/qwen_inference_prompt.md",
    "schema": "backend/eval_data/trip_text_cards_agent_v2/qwen_semantic_draft.schema.json",
    "config": "backend/eval_data/trip_text_cards_agent_v2/qwen_inference_config.json",
    "provider": "backend/eval_data/trip_text_cards_agent_v2/provider_binding.json",
    "thresholds": "backend/eval_data/trip_text_cards_agent_v2/sealed_blind_thresholds.json",
    "dev_validation_scorer": "backend/scripts/score_g01_agent_dev_validation.py",
    "sealed_scorer": "backend/scripts/score_g01_sealed_agent_blind.py",
    "review_schema": "backend/eval_data/agent_gate_v1/review.schema.json",
    "adjudication_schema": "backend/eval_data/agent_gate_v1/adjudication.schema.json",
}
REQUIRED_COMPONENTS = {
    "AUTOMATED_PRODUCT_GATE",
    "LIVE_PROVIDER_GATE",
    "MULTI_AGENT_PANEL",
    "SEALED_AGENT_BLIND",
}
_PROGRESS_PATTERN = re.compile(
    r"Product progress\s*=\s*(UI|API|MODEL|PROVIDER|EVAL_METRIC|NONE)"
)


def _git(root: Path, *args: str, binary: bool = False) -> str | bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=not binary,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoreAgentGateError(f"Git CORE readback timed out: {' '.join(args)}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace") if binary else result.stderr
        raise CoreAgentGateError(
            f"Git CORE readback failed: {' '.join(args)}: {str(stderr).strip()}"
        )
    if binary:
        return result.stdout
    return result.stdout.strip()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_blob(root: Path, commit: str, relative_path: str) -> bytes:
    value = _git(root, "show", f"{commit}:{relative_path}", binary=True)
    assert isinstance(value, bytes)
    return value


def _git_blob_sha256(root: Path, commit: str, relative_path: str) -> str:
    return _sha256_bytes(_git_blob(root, commit, relative_path))


def _git_bundle_sha256(root: Path, commit: str, roots: tuple[str, ...]) -> str:
    output = _git(
        root,
        "ls-tree",
        "-r",
        "--full-tree",
        commit,
        "--",
        *roots,
        binary=True,
    )
    assert isinstance(output, bytes)
    entries: dict[str, tuple[str, str]] = {}
    for raw in output.splitlines():
        metadata, raw_path = raw.split(b"\t", maxsplit=1)
        _mode, object_type, object_id = metadata.decode("ascii").split()
        if object_type != "blob":
            continue
        path = raw_path.decode("utf-8")
        entries[path] = (object_type, object_id)
    if not entries:
        raise CoreAgentGateError("CORE Git bundle resolved no candidate blobs")
    canonical = json.dumps(
        [[path, *entries[path]] for path in sorted(entries)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(canonical)


def read_worktree_binding(repository_root: Path) -> CurrentGoalBinding:
    path = repository_root / "docs/governance/current_goal_binding.json"
    try:
        return CurrentGoalBinding.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CoreAgentGateError(f"invalid worktree Goal binding: {exc}") from exc


@dataclass(frozen=True)
class CoreCandidateContext:
    repository_root: Path
    candidate_commit: str
    candidate_tree: str
    binding: CurrentGoalBinding
    config_sha256: str
    data_sha256: str
    frozen_binding_sha256: dict[str, str]
    current_goal_binding_sha256: str
    current_goal_document_sha256: str

    @classmethod
    def load(
        cls,
        *,
        repository_root: Path,
        candidate_commit: str,
        candidate_tree: str,
        require_clean: bool = True,
    ) -> "CoreCandidateContext":
        root = repository_root.resolve(strict=True)
        if _git(root, "rev-parse", "HEAD") != candidate_commit:
            raise CoreAgentGateError("CORE checkout HEAD does not match candidate")
        if _git(root, "show", "-s", "--format=%T", "HEAD") != candidate_tree:
            raise CoreAgentGateError("CORE checkout tree does not match candidate")
        if require_clean and _git(
            root, "status", "--porcelain=v1", "--untracked-files=all"
        ):
            raise CoreAgentGateError("CORE candidate checkout is not clean")
        binding_path = "docs/governance/current_goal_binding.json"
        try:
            binding = CurrentGoalBinding.model_validate_json(
                _git_blob(root, candidate_commit, binding_path)
            )
        except ValueError as exc:
            raise CoreAgentGateError(f"invalid candidate Goal binding: {exc}") from exc
        if binding.gate_profile != "CORE_AGENT_GATE" or not 1 <= binding.goal_sequence <= 6:
            raise CoreAgentGateError("CORE Agent Gate is restricted to G01-G06")
        contract_sha = _git_blob_sha256(
            root, candidate_commit, binding.automated_gate_contract_path
        )
        if contract_sha != binding.automated_gate_contract_sha256:
            raise CoreAgentGateError("CORE automated contract hash mismatch")
        goal_path = "docs/governance/CURRENT_GOAL.md"
        goal_bytes = _git_blob(root, candidate_commit, goal_path)
        goal_text = goal_bytes.decode("utf-8")
        if f"Goal ID: {binding.goal_id}" not in goal_text:
            raise CoreAgentGateError("CURRENT_GOAL does not match the CORE binding")
        progress = _PROGRESS_PATTERN.findall(goal_text)
        if len(progress) >= 2 and progress[-2:] == ["NONE", "NONE"]:
            raise CoreAgentGateError(
                "checkpoint ledger ends with two Product progress=NONE entries"
            )
        frozen = {
            name: _git_blob_sha256(root, candidate_commit, path)
            for name, path in CORE_FROZEN_BINDING_PATHS.items()
        }
        return cls(
            repository_root=root,
            candidate_commit=candidate_commit,
            candidate_tree=candidate_tree,
            binding=binding,
            config_sha256=_git_bundle_sha256(
                root, candidate_commit, CORE_CONFIG_ROOTS
            ),
            data_sha256=_git_bundle_sha256(root, candidate_commit, CORE_DATA_ROOTS),
            frozen_binding_sha256=frozen,
            current_goal_binding_sha256=_git_blob_sha256(
                root, candidate_commit, binding_path
            ),
            current_goal_document_sha256=_sha256_bytes(goal_bytes),
        )


def _canonical_sha256(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def run_core_automated_checks(
    *, context: CoreCandidateContext, output_path: Path
) -> AutomatedProductExecutionManifest:
    contract_bytes = _git_blob(
        context.repository_root,
        context.candidate_commit,
        context.binding.automated_gate_contract_path,
    )
    try:
        contract = AutomatedProductGateContract.model_validate_json(contract_bytes)
    except ValueError as exc:
        raise CoreAgentGateError(f"invalid CORE automated contract: {exc}") from exc
    if (
        contract.goal_id != context.binding.goal_id
        or contract.gate_profile != "CORE_AGENT_GATE"
    ):
        raise CoreAgentGateError("CORE automated contract uses the wrong Goal profile")
    executions: list[AutomatedCheckExecution] = []
    for check in contract.checks:
        cwd = (context.repository_root / check.workdir).resolve(strict=True)
        if context.repository_root not in cwd.parents and cwd != context.repository_root:
            raise CoreAgentGateError(f"CORE check escapes checkout: {check.check_id}")
        started = datetime.now(timezone.utc)
        try:
            result = subprocess.run(
                check.argv,
                cwd=cwd,
                capture_output=True,
                check=False,
                timeout=check.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise CoreAgentGateError(
                f"CORE automated check timed out: {check.check_id}"
            ) from exc
        completed = datetime.now(timezone.utc)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-4000:]
            stdout = result.stdout.decode("utf-8", errors="replace")[-4000:]
            raise CoreAgentGateError(
                f"CORE automated check failed: {check.check_id}; "
                f"stdout={stdout!r}; stderr={stderr!r}"
            )
        executions.append(
            AutomatedCheckExecution(
                check_id=check.check_id,
                argv_sha256=_canonical_sha256(check.argv),
                workdir=check.workdir,
                exit_code=0,
                stdout_sha256=_sha256_bytes(result.stdout),
                stderr_sha256=_sha256_bytes(result.stderr),
                started_at=started,
                completed_at=completed,
                verdict="PASS",
            )
        )
    manifest = AutomatedProductExecutionManifest(
        goal_id=context.binding.goal_id,
        gate_profile="CORE_AGENT_GATE",
        candidate_commit=context.candidate_commit,
        candidate_tree=context.candidate_tree,
        candidate_config_sha256=context.config_sha256,
        candidate_data_sha256=context.data_sha256,
        gate_contract_sha256=context.binding.automated_gate_contract_sha256,
        isolation_mode="FRESH_CLEAN_CHECKOUT",
        network_access=False,
        host_mount_count=0,
        host_pid_namespace=False,
        synthetic_profile=False,
        authority_secret_mount_count=0,
        checks=executions,
        checks_not_run=[],
        failure_stage=None,
        verdict="PASS",
    )
    content = (
        json.dumps(
            manifest.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    write_external_bytes_exclusive(output_path, content, context.repository_root)
    return manifest


def _require_common_receipt(
    value: dict[str, Any], context: CoreCandidateContext
) -> None:
    expected = (
        context.binding.goal_id,
        context.candidate_commit,
        context.candidate_tree,
    )
    actual = (
        value.get("goal_id"),
        value.get("candidate_commit"),
        value.get("candidate_tree"),
    )
    if actual != expected:
        raise CoreAgentGateError("CORE component receipt candidate binding mismatch")


_CORE_LIVE_EXTERNAL_ARTIFACTS = {
    "reference_a": "--reference-a",
    "reference_b": "--reference-b",
    "adjudication": "--adjudication",
    "provider_receipt_index": "--provider-receipt-index",
    "provider_runtime_receipts": "--provider-runtime-receipts",
    "predictions": "--predictions",
    "inference_outputs": "--inference-outputs",
    "prediction_envelope": "--prediction-envelope",
    "inference_receipt_bundle": "--inference-receipt-bundle",
}


def _verify_core_live_reproduction(
    value: dict[str, Any],
    context: CoreCandidateContext,
) -> None:
    reproduction = value.get("core_reproduction")
    if not isinstance(reproduction, dict):
        raise CoreAgentGateError("CORE live score has no deterministic reproduction binding")
    scorer_relative = reproduction.get("scorer_repository_path")
    if scorer_relative != CORE_FROZEN_BINDING_PATHS["dev_validation_scorer"]:
        raise CoreAgentGateError("CORE live score uses the wrong frozen scorer")
    if reproduction.get("scorer_sha256") != context.frozen_binding_sha256[
        "dev_validation_scorer"
    ]:
        raise CoreAgentGateError("CORE live scorer byte binding mismatch")
    raw_artifacts = reproduction.get("external_artifacts")
    if not isinstance(raw_artifacts, dict) or set(raw_artifacts) != set(
        _CORE_LIVE_EXTERNAL_ARTIFACTS
    ):
        raise CoreAgentGateError("CORE live score source artifact set is incomplete")
    snapshots: dict[str, Any] = {}
    for name in _CORE_LIVE_EXTERNAL_ARTIFACTS:
        artifact = raw_artifacts.get(name)
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise CoreAgentGateError("CORE live source artifact binding is invalid")
        path = artifact.get("path")
        if not isinstance(path, str):
            raise CoreAgentGateError("CORE live source artifact path is invalid")
        source = read_external_snapshot(Path(path), context.repository_root)
        if source.sha256 != artifact.get("sha256"):
            raise CoreAgentGateError("CORE live source artifact hash mismatch")
        snapshots[name] = source
    database_export_sha = reproduction.get(
        "expected_database_export_receipt_sha256"
    )
    provider_http_sha = reproduction.get(
        "expected_provider_http_receipt_bundle_sha256"
    )
    if not all(
        isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
        for item in (database_export_sha, provider_http_sha)
    ):
        raise CoreAgentGateError("CORE live Provider receipt binding is invalid")

    scorer = context.repository_root / str(scorer_relative)
    data_root = (
        context.repository_root / "backend/eval_data/trip_text_cards_v1"
    )
    command = [sys.executable, str(scorer), "--split", "validation"]
    for name, flag in _CORE_LIVE_EXTERNAL_ARTIFACTS.items():
        command.extend([flag, str(snapshots[name].path)])
    command.extend(
        [
            "--model-binding-artifact",
            str(context.repository_root / CORE_FROZEN_BINDING_PATHS["model"]),
            "--prompt-artifact",
            str(context.repository_root / CORE_FROZEN_BINDING_PATHS["prompt"]),
            "--schema-artifact",
            str(context.repository_root / CORE_FROZEN_BINDING_PATHS["schema"]),
            "--config-artifact",
            str(context.repository_root / CORE_FROZEN_BINDING_PATHS["config"]),
            "--expected-candidate-commit",
            context.candidate_commit,
            "--expected-candidate-tree",
            context.candidate_tree,
            "--expected-model-binding-sha256",
            context.frozen_binding_sha256["model"],
            "--expected-prompt-sha256",
            context.frozen_binding_sha256["prompt"],
            "--expected-schema-sha256",
            context.frozen_binding_sha256["schema"],
            "--expected-config-sha256",
            context.frozen_binding_sha256["config"],
            "--expected-provider-binding-sha256",
            context.frozen_binding_sha256["provider"],
            "--expected-inference-receipt-bundle-sha256",
            snapshots["inference_receipt_bundle"].sha256,
            "--expected-provider-runtime-bundle-sha256",
            snapshots["provider_runtime_receipts"].sha256,
            "--expected-database-export-receipt-sha256",
            str(database_export_sha),
            "--expected-provider-http-receipt-bundle-sha256",
            str(provider_http_sha),
            "--data-root",
            str(data_root),
        ]
    )
    with tempfile.TemporaryDirectory(prefix="g01-core-live-") as directory:
        output_path = Path(directory) / "validation.score.json"
        command.extend(["--output", str(output_path)])
        result = subprocess.run(
            command,
            cwd=context.repository_root / "backend",
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "PYTHONPATH": str(context.repository_root / "backend"),
            },
            timeout=600,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[-2000:]
            raise CoreAgentGateError(
                f"CORE live deterministic reproduction failed: {detail}"
            )
        try:
            reproduced = json.loads(output_path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise CoreAgentGateError(
                f"CORE live reproduction output is invalid: {exc}"
            ) from exc
    observed = json.loads(json.dumps(value))
    expected = json.loads(json.dumps(reproduced))
    for receipt in (observed, expected):
        access = receipt.get("input_access")
        if isinstance(access, dict):
            access["artifact_path"] = "CANONICAL_VALIDATION_INPUTS"
    if observed != expected:
        raise CoreAgentGateError("CORE live score is not derived from frozen source artifacts")


def verify_core_live_score(path: Path, context: CoreCandidateContext) -> str:
    snapshot = read_external_snapshot(path, context.repository_root)
    try:
        value = json.loads(snapshot.content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoreAgentGateError(f"invalid CORE live score receipt: {exc}") from exc
    _require_common_receipt(value, context)
    _verify_core_live_reproduction(value, context)
    if (
        value.get("schema_version") != "g01-text-card-agent-scored-receipt-v2"
        or value.get("split") != "validation"
        or value.get("gate_claim") != "VALIDATION_ONLY"
        or value.get("blind_inputs_read") != 0
        or value.get("blind_truth_read") != 0
        or value.get("human_usability_status") != "NOT_RUN"
        or value.get("production_status") != "NOT_RUN"
        or value.get("agent_adjudication", {}).get("human_evidence") is not False
    ):
        raise CoreAgentGateError("CORE live score provenance or evidence boundary failed")
    score = value.get("score", {})
    adjudication = value.get("agent_adjudication", {})
    prediction_bindings = value.get("prediction_bindings", {})
    confirmation = score.get("estimated_confirmation_required_count", {})
    checks = (
        score.get("forbidden_content_as_place_count") == 0,
        score.get("severe_wrong_auto_match_count") == 0,
        score.get("wrong_city_auto_match_count") == 0,
        score.get("wrong_category_auto_match_count") == 0,
        score.get("auto_match", {}).get("denominator", 0) >= 50,
        score.get("auto_match", {}).get("precision", 0) >= 0.99,
        score.get("executable_mentions", {}).get("precision", 0) >= 0.98,
        score.get("executable_mentions", {}).get("recall", 0) >= 0.95,
        score.get("day_assignment", {}).get("f1", 0) >= 0.97,
        score.get("role_macro_f1", 0) >= 0.94,
        score.get("deep_city_auto_match", {}).get("coverage", 0) >= 0.80,
        confirmation.get("median", math.inf) <= 1,
        confirmation.get("p90", math.inf) <= 3,
        score.get("evidence_span_validity", 0) == 1.0,
        score.get("scoring_coverage", 0) == 1.0,
        score.get("candidate_auto_selected_minimum_met") is True,
        value.get("inference_effect_count", 0) >= score.get("case_count", 0) >= 18,
        adjudication.get("live_provider_evidence_verified") is True,
        adjudication.get("canonical_provider_bound_mentions", 0) >= 50,
        adjudication.get("provider_binding_sha256")
        == context.frozen_binding_sha256["provider"],
        prediction_bindings.get("provider_binding_sha256")
        == context.frozen_binding_sha256["provider"],
        prediction_bindings.get("model_binding_sha256")
        == context.frozen_binding_sha256["model"],
        prediction_bindings.get("prompt_sha256")
        == context.frozen_binding_sha256["prompt"],
        prediction_bindings.get("schema_sha256")
        == context.frozen_binding_sha256["schema"],
        prediction_bindings.get("config_sha256")
        == context.frozen_binding_sha256["config"],
    )
    if not all(checks):
        raise CoreAgentGateError("CORE live validation score missed a frozen threshold")
    return snapshot.sha256


def verify_core_panel(path: Path, context: CoreCandidateContext) -> str:
    snapshot = read_external_snapshot(path, context.repository_root)
    try:
        receipt = MultiAgentPanelVerificationReceipt.model_validate_json(snapshot.content)
    except ValueError as exc:
        raise CoreAgentGateError(f"invalid CORE panel receipt: {exc}") from exc
    if (
        receipt.goal_id != context.binding.goal_id
        or receipt.candidate_commit != context.candidate_commit
        or receipt.candidate_tree != context.candidate_tree
        or receipt.candidate_config_sha256 != context.config_sha256
        or receipt.candidate_data_sha256 != context.data_sha256
        or receipt.verdict != "PASS"
        or receipt.accepted_p0_count
        or receipt.accepted_p1_count
        or receipt.accepted_in_scope_p2_count
        or not receipt.required_scenario_union_complete
    ):
        raise CoreAgentGateError("CORE panel receipt failed its blocking conditions")
    observed_reviews = sorted(
        read_external_snapshot(Path(value), context.repository_root).sha256
        for value in receipt.review_paths
    )
    if observed_reviews != sorted(receipt.review_sha256):
        raise CoreAgentGateError("CORE panel review hash readback failed")
    adjudication = read_external_snapshot(
        Path(receipt.adjudication_path), context.repository_root
    )
    if adjudication.sha256 != receipt.adjudication_sha256:
        raise CoreAgentGateError("CORE panel adjudication hash readback failed")
    try:
        verified = verify_review_panel(
            review_paths=[Path(value) for value in receipt.review_paths],
            adjudication_path=Path(receipt.adjudication_path),
            repository_root=context.repository_root,
            expected_goal_id=context.binding.goal_id,
            expected_candidate_commit=context.candidate_commit,
            expected_candidate_tree=context.candidate_tree,
            expected_candidate_config_sha256=context.config_sha256,
            expected_candidate_data_sha256=context.data_sha256,
            expected_input_bundle_sha256=receipt.expected_input_bundle_sha256,
        )
    except (AgentGateValidationError, ValueError) as exc:
        raise CoreAgentGateError(f"CORE panel source verification failed: {exc}") from exc
    if (
        verified["review_sha256"] != sorted(receipt.review_sha256)
        or verified["adjudication_sha256"] != receipt.adjudication_sha256
        or verified["accepted_p0_count"] != 0
        or verified["accepted_p1_count"] != 0
        or verified["accepted_in_scope_p2_count"] != 0
        or verified["roles_complete"] is not True
        or verified["verdict"] != "PASS"
    ):
        raise CoreAgentGateError("CORE panel source artifacts failed their frozen contract")
    return snapshot.sha256


def _thresholds_pass(metrics: dict[str, float | int | bool]) -> bool:
    conditions = (
        metrics.get("forbidden_content_as_place_count") == 0,
        metrics.get("severe_wrong_auto_match_count") == 0,
        metrics.get("wrong_city_auto_match_count") == 0,
        metrics.get("wrong_category_auto_match_count") == 0,
        float(metrics.get("auto_match.precision", 0)) >= 0.99,
        int(metrics.get("auto_match.denominator", 0)) >= 50,
        float(metrics.get("executable_mentions.precision", 0)) >= 0.98,
        float(metrics.get("executable_mentions.recall", 0)) >= 0.95,
        float(metrics.get("day_assignment.f1", 0)) >= 0.97,
        float(metrics.get("role_macro_f1", 0)) >= 0.94,
        float(metrics.get("deep_city_auto_match.coverage", 0)) >= 0.80,
        float(metrics.get("estimated_confirmation_required_count.median", math.inf)) <= 1,
        float(metrics.get("estimated_confirmation_required_count.p90", math.inf)) <= 3,
        metrics.get("evidence_span_validity") == 1.0,
        float(metrics.get("destination.exact_name_accuracy", 0)) >= 0.99,
        float(metrics.get("destination.basis_accuracy", 0)) >= 0.99,
        metrics.get("destination.explicit_evidence_validity") == 1.0,
        metrics.get("public_projection.forbidden_key_hits") == 0,
        metrics.get("public_projection.full_source_leak_hits") == 0,
        float(metrics.get("public_api_latency.cards_ready_p95_ms", math.inf))
        <= 8000,
    )
    return all(conditions)


def verify_core_sealed(
    path: Path,
    deterministic_score_path: Path,
    context: CoreCandidateContext,
) -> str:
    snapshot = read_external_snapshot(path, context.repository_root)
    score_snapshot = read_external_snapshot(
        deterministic_score_path,
        context.repository_root,
    )
    try:
        receipt = SealedAgentBlindReceipt.model_validate_json(snapshot.content)
        score_receipt = SealedAgentBlindReceipt.model_validate_json(
            score_snapshot.content
        )
    except ValueError as exc:
        raise CoreAgentGateError(f"invalid CORE sealed receipt: {exc}") from exc
    if (
        receipt.gate_profile != "CORE_AGENT_GATE"
        or receipt.goal_id != context.binding.goal_id
        or receipt.candidate_commit != context.candidate_commit
        or receipt.candidate_tree != context.candidate_tree
        or receipt.verdict != "PASS"
        or not receipt.required_gate_metrics_passed
        or receipt.human_evidence
        or not receipt.process_isolation
        or receipt.organizational_independence_claimed
        or receipt.blind_truth_returned_to_developer
        or receipt.raw_truth_stored_in_repository
        or not receipt.one_shot_nonce_consumed
        or not _thresholds_pass(receipt.aggregate_metrics)
        or receipt.deterministic_score_receipt_sha256 != score_snapshot.sha256
        or receipt.score_input_manifest_sha256 is not None
    ):
        raise CoreAgentGateError("CORE sealed receipt failed its frozen thresholds")
    if (
        score_receipt.gate_profile != "CORE_AGENT_GATE"
        or score_receipt.deterministic_score_receipt_sha256 is not None
        or score_receipt.score_input_manifest_sha256 is not None
        or not score_receipt.required_gate_metrics_passed
        or score_receipt.verdict != "PASS"
        or not _thresholds_pass(score_receipt.aggregate_metrics)
    ):
        raise CoreAgentGateError("CORE sealed deterministic score receipt is invalid")
    derived_fields = (
        "gate_profile",
        "goal_id",
        "candidate_commit",
        "candidate_tree",
        "prompt_sha256",
        "schema_sha256",
        "thresholds_sha256",
        "config_sha256",
        "provider_binding_sha256",
        "scorer_sha256",
        "input_bundle_sha256",
        "prediction_bundle_sha256",
        "scored_case_count",
        "custodian_task_id",
        "model",
        "reasoning_effort",
        "process_isolation",
        "organizational_independence_claimed",
        "human_evidence",
        "blind_truth_returned_to_developer",
        "raw_truth_stored_in_repository",
        "one_shot_nonce_consumed",
        "aggregate_metrics",
        "taxonomy_counts",
        "error_taxonomy",
        "required_gate_metrics_passed",
        "evidence_level",
        "verdict",
        "completed_at",
    )
    if any(
        getattr(receipt, field) != getattr(score_receipt, field)
        for field in derived_fields
    ):
        raise CoreAgentGateError(
            "CORE sealed summary is not derived from its deterministic score receipt"
        )
    expected_bindings = (
        (receipt.prompt_sha256, context.frozen_binding_sha256["prompt"]),
        (receipt.schema_sha256, context.frozen_binding_sha256["schema"]),
        (receipt.config_sha256, context.frozen_binding_sha256["config"]),
        (receipt.provider_binding_sha256, context.frozen_binding_sha256["provider"]),
        (receipt.thresholds_sha256, context.frozen_binding_sha256["thresholds"]),
        (receipt.scorer_sha256, context.frozen_binding_sha256["sealed_scorer"]),
    )
    if any(observed != expected for observed, expected in expected_bindings):
        raise CoreAgentGateError("CORE sealed receipt frozen binding mismatch")
    return snapshot.sha256


def _read_remote_candidate(
    *, context: CoreCandidateContext
) -> tuple[str, str, str]:
    root = context.repository_root
    remote_ref = context.binding.canonical_candidate_ref
    lines = str(_git(root, "ls-remote", "--refs", "origin", remote_ref)).splitlines()
    if len(lines) != 1:
        raise CoreAgentGateError("CORE remote ref did not resolve exactly once")
    remote_subject, observed_ref = lines[0].split(maxsplit=1)
    if observed_ref != remote_ref or remote_subject != context.candidate_commit:
        raise CoreAgentGateError("CORE remote subject does not match candidate")
    _git(root, "fetch", "--no-tags", "origin", remote_ref)
    remote_tree = str(_git(root, "show", "-s", "--format=%T", remote_subject))
    if remote_tree != context.candidate_tree:
        raise CoreAgentGateError("CORE remote tree does not match candidate")
    origin_url = str(_git(root, "remote", "get-url", "origin"))
    if origin_url != "https://github.com/Munto47/BreezeTravel.git":
        raise CoreAgentGateError("CORE checkout is not bound to the canonical origin")
    return remote_subject, remote_tree, remote_ref


def verify_core_agent_gate_pass(
    *,
    repository_root: Path,
    development_checkout_root: Path,
    expected_candidate_commit: str,
    expected_candidate_tree: str,
    automated_manifest_output: Path,
    live_score_path: Path,
    panel_verification_path: Path,
    sealed_receipt_path: Path,
    sealed_score_receipt_path: Path,
    output_path: Path,
) -> AgentGatePassReceipt:
    fresh_root = repository_root.resolve(strict=True)
    development_root = development_checkout_root.resolve(strict=True)
    if fresh_root == development_root:
        raise CoreAgentGateError("CORE final Gate requires a distinct clean checkout")
    if _git(development_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise CoreAgentGateError("CORE development checkout is not clean")
    context = CoreCandidateContext.load(
        repository_root=fresh_root,
        candidate_commit=expected_candidate_commit,
        candidate_tree=expected_candidate_tree,
    )
    run_core_automated_checks(context=context, output_path=automated_manifest_output)
    automated_snapshot = read_external_snapshot(
        automated_manifest_output, context.repository_root
    )
    live_sha = verify_core_live_score(live_score_path, context)
    panel_sha = verify_core_panel(panel_verification_path, context)
    sealed_sha = verify_core_sealed(
        sealed_receipt_path,
        sealed_score_receipt_path,
        context,
    )
    remote_subject, remote_tree, remote_ref = _read_remote_candidate(context=context)
    # Re-read every external component and the remote subject after the lengthy
    # automated checks so replacement or branch drift cannot be hidden.
    component_hashes = {
        "AUTOMATED_PRODUCT_GATE": read_external_snapshot(
            automated_manifest_output, context.repository_root
        ).sha256,
        "LIVE_PROVIDER_GATE": verify_core_live_score(live_score_path, context),
        "MULTI_AGENT_PANEL": verify_core_panel(panel_verification_path, context),
        "SEALED_AGENT_BLIND": verify_core_sealed(
            sealed_receipt_path,
            sealed_score_receipt_path,
            context,
        ),
    }
    if component_hashes["AUTOMATED_PRODUCT_GATE"] != automated_snapshot.sha256:
        raise CoreAgentGateError("CORE automated manifest changed during verification")
    if (
        component_hashes["LIVE_PROVIDER_GATE"] != live_sha
        or component_hashes["MULTI_AGENT_PANEL"] != panel_sha
        or component_hashes["SEALED_AGENT_BLIND"] != sealed_sha
    ):
        raise CoreAgentGateError("CORE component receipt changed during verification")
    remote_subject, remote_tree, remote_ref = _read_remote_candidate(context=context)
    receipt = AgentGatePassReceipt(
        gate_profile="CORE_AGENT_GATE",
        goal_sequence=context.binding.goal_sequence,
        goal_id=context.binding.goal_id,
        predecessor_goal_id=context.binding.predecessor_goal_id,
        predecessor_completion_commit=context.binding.predecessor_completion_commit,
        current_goal_binding_sha256=context.current_goal_binding_sha256,
        current_goal_document_sha256=context.current_goal_document_sha256,
        automated_gate_contract_sha256=(
            context.binding.automated_gate_contract_sha256
        ),
        candidate_commit=context.candidate_commit,
        candidate_tree=context.candidate_tree,
        candidate_config_sha256=context.config_sha256,
        candidate_data_sha256=context.data_sha256,
        frozen_binding_sha256=context.frozen_binding_sha256,
        component_receipt_sha256=component_hashes,
        fresh_checkout_root_sha256=_sha256_bytes(str(fresh_root).encode("utf-8")),
        remote_name="origin",
        remote_ref=remote_ref,
        remote_subject=remote_subject,
        remote_tree=remote_tree,
        verifier_sha256=_git_blob_sha256(
            fresh_root,
            context.candidate_commit,
            "backend/evals/agent_gate_v1/core_gate.py",
        ),
        evidence_levels=[
            "AUTOMATED_TEST",
            "LIVE_PROVIDER_EVIDENCE",
            "MULTI_AGENT_SIMULATED_REVIEW",
            "SEALED_AGENT_BLIND",
        ],
        human_usability_status="NOT_RUN",
        production_status="NOT_RUN",
        verdict="AGENT_GATE_PASS",
        completed_at=datetime.now(timezone.utc),
    )
    content = (
        json.dumps(
            receipt.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    write_external_bytes_exclusive(output_path, content, context.repository_root)
    return receipt
