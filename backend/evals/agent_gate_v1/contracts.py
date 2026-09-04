from __future__ import annotations

import math
import re
from base64 import b64decode
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AGENT_GATE_SCHEMA_VERSION = "agent-gate-review-v1"
AGENT_GATE_ADJUDICATION_SCHEMA_VERSION = "agent-gate-adjudication-v1"
SEALED_AGENT_BLIND_SCHEMA_VERSION = "sealed-agent-blind-receipt-v2"
SEALED_AGENT_BLIND_THRESHOLDS_SCHEMA_VERSION = "sealed-agent-blind-thresholds-v1"
SEALED_AGENT_BLIND_SCORE_SCHEMA_VERSION = "sealed-agent-blind-score-receipt-v2"
SEALED_AGENT_BLIND_MINT_SCHEMA_VERSION = "sealed-agent-blind-mint-receipt-v2"

EvidenceLevel = Literal[
    "AUTOMATED_TEST",
    "LIVE_PROVIDER_EVIDENCE",
    "MULTI_AGENT_SIMULATED_REVIEW",
    "SEALED_AGENT_BLIND",
    "HUMAN_USABILITY",
    "PRODUCTION_EVIDENCE",
]
ReviewVerdict = Literal["PASS", "FAIL", "INCOMPLETE"]
ScenarioStatus = Literal["PASS", "FAIL", "NOT_RUN"]
Severity = Literal["P0", "P1", "P2", "P3"]
ReviewerRole = Literal[
    "PRODUCT_UX",
    "SEMANTIC_DOMAIN",
    "RELIABILITY_SECURITY",
]
AgentGateComponent = Literal[
    "AUTOMATED_PRODUCT_GATE",
    "LIVE_PROVIDER_GATE",
    "MULTI_AGENT_PANEL",
    "SEALED_AGENT_BLIND",
]
ThresholdOperator = Literal["GE", "LE", "EQ"]
AuthorityRole = Literal[
    "SEALED_CUSTODY",
    "AMAP_LIVE_EXPORTER",
    "QWEN_LIVE_EXPORTER",
    "AUTOMATED_PRODUCT_GATE",
    "LIVE_PROVIDER_GATE",
    "MULTI_AGENT_PANEL",
    "SEALED_AGENT_BLIND",
    "FINAL_GATE",
]

GateProfile = Literal["CORE_AGENT_GATE", "HARDENED_CANDIDATE_GATE"]
MainlinePhase = Literal[
    "CORE_MVP",
    "PRODUCT_ENHANCEMENT",
    "CANDIDATE_HARDENING",
]
WorkPackageStatus = Literal[
    "PREPARED_NOT_INTEGRATED",
    "WAITING_FOR_WRITER_SLOT",
    "IN_PROGRESS",
    "READY_TO_MERGE",
    "MERGED",
    "DEFERRED",
    "BLOCKED_EXTERNAL",
]
WorkPackageRole = Literal["INTEGRATOR", "CONTRIBUTOR"]
WorkPackageExecutionMode = Literal[
    "PRIMARY_INTEGRATOR_DIALOGUE",
    "INDEPENDENT_FUNCTION_DIALOGUE",
]
ScopeWorkKind = Literal["PRODUCT", "CURRENT_GATE_FIX", "EVAL_INFRA", "HARDENING"]
ScopePhase = Literal["IMPLEMENTING", "PREFLIGHT", "EVIDENCE_FROZEN", "GATE_RUNNING"]
ScopeIssueSeverity = Literal["P0", "P1", "P2_BLOCKING"]
ScopeGuardVerdict = Literal["PASS", "SCOPE_REVIEW_REQUIRED", "DEFER_TO_G07", "REJECT"]
ScopeGuardErrorCode = Literal[
    "STAGE_SCOPE_VIOLATION",
    "POLICY_SELF_MODIFICATION",
    "NEW_DURABLE_EVAL_STATE",
    "NEW_CRYPTO_PROTOCOL",
    "BUDGET_EXCEEDED",
    "EVIDENCE_FREEZE_BROKEN",
]
HardeningDecision = Literal["NOT_REQUIRED_WITH_RATIONALE", "REQUIRED"]
HardeningControl = Literal[
    "EXTERNAL_AUTHORITY",
    "PURPOSE_SPECIFIC_BROKER",
    "ROLE_SIGNATURES",
    "IMMUTABLE_REMOTE_REF",
    "ISOLATED_OCI",
]

WORK_PACKAGE_PROTECTED_PATHS_V1: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "docs/governance/CURRENT_GOAL.md",
    "docs/governance/current_goal_binding.json",
    "docs/governance/current_work_packages.json",
    "backend/app/db/migrations",
    "packages/trip-check-client/openapi.json",
    "packages/trip-check-client/openapi.current.json",
    "packages/trip-check-client/src/generated",
    "frontend/package-lock.json",
    "miniapp/package-lock.json",
    "packages/trip-check-client/package-lock.json",
    "y-websocket/package-lock.json",
    "backend/eval_data/agent_gate_v1/automation_runner_requirements.lock",
    (
        "backend/eval_data/agent_gate_v1/"
        "automation_runner_browser_package-lock.json"
    ),
)
WORK_PACKAGE_PROTECTED_PATHS: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "docs/governance",
    "backend/app/db/migrations",
    "packages/trip-check-client/openapi.json",
    "packages/trip-check-client/openapi.current.json",
    "packages/trip-check-client/src/generated",
    "frontend/package-lock.json",
    "miniapp/package-lock.json",
    "packages/trip-check-client/package-lock.json",
    "y-websocket/package-lock.json",
    "backend/eval_data/agent_gate_v1/automation_runner_requirements.lock",
    (
        "backend/eval_data/agent_gate_v1/"
        "automation_runner_browser_package-lock.json"
    ),
)
BlindErrorCategory = Literal[
    "WRONG_CITY",
    "WRONG_CATEGORY",
    "NON_ATOMIC_PLACE",
    "MENTION_FALSE_POSITIVE",
    "MENTION_FALSE_NEGATIVE",
    "DAY_ASSIGNMENT",
    "ROLE_CLASSIFICATION",
    "PROVIDER_RESOLUTION",
    "PUBLIC_LEAK",
    "LATENCY",
    "OTHER_AGGREGATED",
]
BLIND_ERROR_CATEGORY_ORDER: tuple[BlindErrorCategory, ...] = (
    "WRONG_CITY",
    "WRONG_CATEGORY",
    "NON_ATOMIC_PLACE",
    "MENTION_FALSE_POSITIVE",
    "MENTION_FALSE_NEGATIVE",
    "DAY_ASSIGNMENT",
    "ROLE_CLASSIFICATION",
    "PROVIDER_RESOLUTION",
    "PUBLIC_LEAK",
    "LATENCY",
    "OTHER_AGGREGATED",
)


def _validate_aggregate_metrics(metrics: dict[str, float | int | bool]) -> None:
    for name, value in metrics.items():
        if not re.fullmatch(r"[a-z][a-z0-9_.]{2,119}", name):
            raise ValueError("aggregate metric names must be non-identifying machine keys")
        if any(token in name for token in ("case_id", "answer", "truth", "g01-tc")):
            raise ValueError("aggregate metrics cannot expose per-case blind information")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("aggregate metrics must be finite")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthorityPublicKey(StrictModel):
    role: AuthorityRole
    authority_id: str = Field(pattern=r"^AUTH-[A-Z0-9-]{6,100}$")
    algorithm: Literal["ED25519"] = "ED25519"
    public_key_base64: str = Field(min_length=40, max_length=60)
    public_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_by_task_id: str = Field(min_length=8, max_length=160)
    private_key_storage: Literal["REPOSITORY_EXTERNAL"] = "REPOSITORY_EXTERNAL"
    human_evidence: Literal[False] = False

    @model_validator(mode="after")
    def public_key_is_raw_ed25519(self) -> "AuthorityPublicKey":
        try:
            raw = b64decode(self.public_key_base64, validate=True)
        except ValueError as exc:
            raise ValueError("authority public key is not valid base64") from exc
        if len(raw) != 32 or sha256(raw).hexdigest() != self.public_key_sha256:
            raise ValueError("authority public key bytes do not match their binding")
        return self


class DetachedAuthoritySignature(StrictModel):
    authority_role: AuthorityRole
    authority_id: str = Field(pattern=r"^AUTH-[A-Z0-9-]{6,100}$")
    algorithm: Literal["ED25519"] = "ED25519"
    signed_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_base64: str = Field(min_length=80, max_length=100)


class LiveCaptureActivationReadiness(StrictModel):
    amap_execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qwen_execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_runner_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    direct_https_capture: Literal[True] = True
    one_shot_mint: Literal[True] = True
    complete_coverage: Literal[True] = True
    status: Literal["READY"] = "READY"


class ExternalSignerActivationReadiness(StrictModel):
    signer_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signer_execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repository_external: Literal[True] = True
    imports_candidate_code: Literal[False] = False
    private_key_in_candidate_process: Literal[False] = False
    key_path_in_candidate_environment: Literal[False] = False
    status: Literal["READY"] = "READY"


class AuthorityActivationReadinessReceipt(StrictModel):
    """Custody-signed proof required before the G01 authority can become ACTIVE."""

    schema_version: Literal["authority-activation-readiness-v1"] = (
        "authority-activation-readiness-v1"
    )
    policy_id: Literal["TC-VNEXT-AGENT-GATE-V1"] = "TC-VNEXT-AGENT-GATE-V1"
    authority_generation: Literal[1] = 1
    goal_id: Literal["TC-VNEXT-G01-TEXT-CARDS"] = "TC-VNEXT-G01-TEXT-CARDS"
    bootstrap_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    bootstrap_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    bootstrap_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bootstrap_core_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_tree_without_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_program_core_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    live_capture: LiveCaptureActivationReadiness
    external_signer: ExternalSignerActivationReadiness
    created_at: datetime
    process_isolation_only: Literal[True] = True
    human_evidence: Literal[False] = False
    status: Literal["FORMAL_ACTIVATION_READY"] = "FORMAL_ACTIVATION_READY"
    authority_signature: DetachedAuthoritySignature

    @model_validator(mode="after")
    def uses_custody_authority(self) -> "AuthorityActivationReadinessReceipt":
        if self.authority_signature.authority_role != "SEALED_CUSTODY":
            raise ValueError("authority activation requires the pinned custody authority")
        return self


class ProgramGoalAuthorityBinding(StrictModel):
    goal_sequence: int = Field(ge=1, le=7)
    goal_id: str = Field(pattern=r"^TC-VNEXT-G0[1-7]-[A-Z0-9-]+$")
    predecessor_goal_id: str = Field(pattern=r"^TC-(?:BP-G00|VNEXT-G0[1-6])-[A-Z0-9-]+$")
    initial_predecessor_completion_commit: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40}$",
    )
    automated_gate_contract_path: str = Field(min_length=1, max_length=300)
    automated_gate_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_profile: GateProfile

    @model_validator(mode="after")
    def binding_is_safe(self) -> "ProgramGoalAuthorityBinding":
        if not self.goal_id.startswith(f"TC-VNEXT-G{self.goal_sequence:02d}-"):
            raise ValueError("program Goal sequence and Goal ID disagree")
        path = self.automated_gate_contract_path.replace("\\", "/")
        if path != self.automated_gate_contract_path:
            raise ValueError("program Goal contract paths must use forward slashes")
        if path.startswith("/") or ".." in Path(path).parts:
            raise ValueError("program Goal contract path must be repository-relative")
        expected_profile = (
            "HARDENED_CANDIDATE_GATE"
            if self.goal_sequence == 7
            else "CORE_AGENT_GATE"
        )
        if self.gate_profile != expected_profile:
            raise ValueError("program Goal uses the wrong Agent Gate profile")
        return self


class AgentGateAuthorityManifest(StrictModel):
    schema_version: Literal["agent-gate-authority-manifest-v1"] = (
        "agent-gate-authority-manifest-v1"
    )
    program_id: Literal["TC-VNEXT-2026"] = "TC-VNEXT-2026"
    policy_id: Literal["TC-VNEXT-AGENT-GATE-V1"] = "TC-VNEXT-AGENT-GATE-V1"
    scope_goal_ids: list[str] = Field(min_length=7, max_length=7)
    canonical_origin_url: Literal["https://github.com/Munto47/BreezeTravel.git"] = (
        "https://github.com/Munto47/BreezeTravel.git"
    )
    canonical_candidate_ref: Literal[
        "refs/heads/codex/trip-check-product-reset"
    ] = "refs/heads/codex/trip-check-product-reset"
    candidate_freeze_ref_prefix: Literal[
        "refs/heads/codex/agent-gate-candidates/"
    ] = "refs/heads/codex/agent-gate-candidates/"
    authority_generation: int = Field(ge=1, le=7)
    authority_phase: Literal["BOOTSTRAP", "ACTIVE"]
    legacy_baseline_commit: Literal[
        "7bdd1a6abd9c10c6076aca67f08de785027501a0"
    ] = "7bdd1a6abd9c10c6076aca67f08de785027501a0"
    custody_registry_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    custody_registry_path_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_roots: list[str] = Field(min_length=1, max_length=150)
    data_roots: list[str] = Field(min_length=1, max_length=150)
    program_core_paths: list[str] = Field(min_length=1, max_length=100)
    immutable_protocol_paths: list[str] = Field(min_length=1, max_length=100)
    bootstrap_core_paths: list[str] = Field(min_length=1, max_length=100)
    current_goal_binding_path: Literal[
        "docs/governance/current_goal_binding.json"
    ] = "docs/governance/current_goal_binding.json"
    current_goal_document_path: Literal[
        "docs/governance/CURRENT_GOAL.md"
    ] = "docs/governance/CURRENT_GOAL.md"
    authority_activation_receipt_path: Literal[
        "backend/eval_data/agent_gate_v1/authority_activation_readiness.json"
    ] = "backend/eval_data/agent_gate_v1/authority_activation_readiness.json"
    goal_bindings: list[ProgramGoalAuthorityBinding] = Field(
        min_length=7,
        max_length=7,
    )
    component_verifier_paths: dict[AgentGateComponent, str]
    live_exporter_paths: dict[AuthorityRole, str]
    authorities: list[AuthorityPublicKey] = Field(min_length=8, max_length=20)
    frozen_at: datetime
    process_isolation_only: Literal[True] = True
    human_evidence: Literal[False] = False
    status: Literal["GOAL_SCOPED_IMMUTABLE_GENERATION"] = (
        "GOAL_SCOPED_IMMUTABLE_GENERATION"
    )

    @model_validator(mode="after")
    def authority_manifest_is_complete(self) -> "AgentGateAuthorityManifest":
        expected_goals = [
            f"TC-VNEXT-G{index:02d}-{suffix}"
            for index, suffix in enumerate(
                (
                    "TEXT-CARDS",
                    "MAP-STAY",
                    "TOP3-AUDIT",
                    "SCREENSHOT",
                    "CITY-KNOWLEDGE",
                    "MEMORY-SHARE",
                    "CANDIDATE",
                ),
                start=1,
            )
        ]
        if self.scope_goal_ids != expected_goals:
            raise ValueError("authority policy must cover the ordered G01-G07 program goals")
        if [item.goal_id for item in self.goal_bindings] != expected_goals:
            raise ValueError("authority policy Goal bindings must follow the G01-G07 order")
        if self.authority_generation > len(expected_goals):
            raise ValueError("authority generation exceeds the governed Program")
        if self.authority_phase == "BOOTSTRAP" and self.authority_generation != 1:
            raise ValueError("only G01 may use the pre-anchor bootstrap phase")
        for index, item in enumerate(self.goal_bindings):
            if item.goal_sequence != index + 1:
                raise ValueError("authority policy Goal binding sequence is not contiguous")
            expected_predecessor = (
                "TC-BP-G00-BLUEPRINT" if index == 0 else expected_goals[index - 1]
            )
            if item.predecessor_goal_id != expected_predecessor:
                raise ValueError("authority policy Goal predecessor is incorrect")
            if index == 0:
                if (
                    item.initial_predecessor_completion_commit
                    != "f3b5f3e0c36ff3977f826bd82a83b3150a2e97ac"
                ):
                    raise ValueError("G01 must pin the completed Blueprint activation commit")
            elif item.initial_predecessor_completion_commit is not None:
                raise ValueError("only G01 may pin an initial predecessor completion")
        required_roles: set[AuthorityRole] = {
            "SEALED_CUSTODY",
            "AMAP_LIVE_EXPORTER",
            "QWEN_LIVE_EXPORTER",
            "AUTOMATED_PRODUCT_GATE",
            "LIVE_PROVIDER_GATE",
            "MULTI_AGENT_PANEL",
            "SEALED_AGENT_BLIND",
            "FINAL_GATE",
        }
        roles = [item.role for item in self.authorities]
        if set(roles) != required_roles or len(roles) != len(set(roles)):
            raise ValueError("authority manifest must pin exactly one key for every role")
        if set(self.component_verifier_paths) != {
            "AUTOMATED_PRODUCT_GATE",
            "LIVE_PROVIDER_GATE",
            "MULTI_AGENT_PANEL",
            "SEALED_AGENT_BLIND",
        }:
            raise ValueError("authority manifest component verifier set is incomplete")
        paths = [
            *self.config_roots,
            *self.data_roots,
            *self.program_core_paths,
            *self.immutable_protocol_paths,
            *self.bootstrap_core_paths,
            *self.component_verifier_paths.values(),
            *self.live_exporter_paths.values(),
            *(item.automated_gate_contract_path for item in self.goal_bindings),
            self.current_goal_binding_path,
            self.current_goal_document_path,
            self.authority_activation_receipt_path,
        ]
        if len(self.config_roots) != len(set(self.config_roots)):
            raise ValueError("authority manifest config roots must be unique")
        if len(self.data_roots) != len(set(self.data_roots)):
            raise ValueError("authority manifest data roots must be unique")
        if len(self.immutable_protocol_paths) != len(
            set(self.immutable_protocol_paths)
        ):
            raise ValueError("authority manifest immutable paths must be unique")
        if len(self.program_core_paths) != len(set(self.program_core_paths)):
            raise ValueError("authority manifest Program core paths must be unique")
        if self.program_core_paths != self.immutable_protocol_paths:
            raise ValueError("Program core and immutable protocol paths must be identical")
        if not set(self.bootstrap_core_paths).issubset(self.program_core_paths):
            raise ValueError("bootstrap core paths must remain inside the Program core")
        if len(self.bootstrap_core_paths) != len(set(self.bootstrap_core_paths)):
            raise ValueError("bootstrap core paths must be unique")
        if not set(self.component_verifier_paths.values()).issubset(
            self.immutable_protocol_paths
        ):
            raise ValueError("component verifiers must be immutable protocol paths")
        if set(self.live_exporter_paths) != {
            "AMAP_LIVE_EXPORTER",
            "QWEN_LIVE_EXPORTER",
        }:
            raise ValueError("authority manifest must pin both live exporter paths")
        if not set(self.live_exporter_paths.values()).issubset(
            self.immutable_protocol_paths
        ):
            raise ValueError("live exporters must be immutable protocol paths")
        if not {
            item.automated_gate_contract_path for item in self.goal_bindings
        }.issubset(self.immutable_protocol_paths):
            raise ValueError("all Goal automation contracts must be immutable protocol paths")
        if any(
            not path
            or "\\" in path
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in path.split("/"))
            for path in paths
        ):
            raise ValueError("authority manifest paths must be safe repository-relative paths")
        return self


def _mainline_phase_for_sequence(sequence: int) -> MainlinePhase:
    if sequence <= 3:
        return "CORE_MVP"
    if sequence <= 6:
        return "PRODUCT_ENHANCEMENT"
    return "CANDIDATE_HARDENING"


def _normalize_repository_path(path: str) -> str:
    if not path or "\\" in path or path.startswith("/"):
        raise ValueError("work package paths must be repository-relative")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("work package paths contain an unsafe segment")
    return "/".join(parts)


def _paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def _normalize_worktree_path(path: str) -> str:
    normalized = path.replace("\\", "/").rstrip("/")
    if not (
        normalized.startswith("/")
        or re.fullmatch(r"[A-Za-z]:/.*", normalized)
    ):
        raise ValueError("work package worktree path must be absolute")
    parts = normalized.split("/")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("work package worktree path contains an unsafe segment")
    return normalized


class WorkPackageBinding(StrictModel):
    package_id: str = Field(pattern=r"^WP-G0[1-7]-[A-Z0-9-]{3,80}$")
    goal_id: str = Field(pattern=r"^TC-VNEXT-G0[1-7]-[A-Z0-9-]+$")
    baseline_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    branch: str = Field(pattern=r"^codex/[A-Za-z0-9._/-]+$")
    remote_branch: str | None = Field(
        default=None,
        pattern=r"^origin/codex/[A-Za-z0-9._/-]+$",
    )
    worktree_path: str | None = Field(default=None, min_length=3, max_length=500)
    role: WorkPackageRole
    execution_mode: WorkPackageExecutionMode | None = None
    dialogue_ref: str | None = Field(default=None, min_length=3, max_length=300)
    prompt_path: str | None = Field(default=None, min_length=1, max_length=300)
    prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    registry_activation_commit: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40}$",
    )
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    owned_paths: list[str] = Field(min_length=1, max_length=80)
    forbidden_paths: list[str] = Field(default_factory=list, max_length=80)
    acceptance: list[str] = Field(min_length=1, max_length=40)
    merge_order: int = Field(ge=1, le=100)
    status: WorkPackageStatus
    ready_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    merged_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @model_validator(mode="after")
    def paths_and_dependencies_are_safe(self) -> "WorkPackageBinding":
        owned = [_normalize_repository_path(path) for path in self.owned_paths]
        forbidden = [_normalize_repository_path(path) for path in self.forbidden_paths]
        if self.prompt_path is not None:
            self.prompt_path = _normalize_repository_path(self.prompt_path)
        if self.worktree_path is not None:
            self.worktree_path = _normalize_worktree_path(self.worktree_path)
        if len(owned) != len(set(owned)) or len(forbidden) != len(set(forbidden)):
            raise ValueError("work package paths must be unique")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("work package dependencies must be unique")
        if self.package_id in self.dependencies:
            raise ValueError("work package cannot depend on itself")
        if any(
            _paths_overlap(left, right)
            for index, left in enumerate(owned)
            for right in owned[index + 1 :]
        ):
            raise ValueError("work package owned paths overlap internally")
        if any(_paths_overlap(left, right) for left in owned for right in forbidden):
            raise ValueError("work package owned and forbidden paths overlap")
        if self.execution_mode is not None:
            expected_mode = (
                "PRIMARY_INTEGRATOR_DIALOGUE"
                if self.role == "INTEGRATOR"
                else "INDEPENDENT_FUNCTION_DIALOGUE"
            )
            if self.execution_mode != expected_mode:
                raise ValueError(
                    "work package role must use its matching user-visible dialogue mode"
                )
        if self.remote_branch is not None and self.remote_branch != f"origin/{self.branch}":
            raise ValueError("work package remote branch must match its local branch")
        self.owned_paths = owned
        self.forbidden_paths = forbidden
        return self


class ScopeBlockingIssue(StrictModel):
    severity: ScopeIssueSeverity
    reproduction: str = Field(min_length=3, max_length=1000)
    current_goal_acceptance_ref: str = Field(min_length=3, max_length=500)
    impact_chain: str = Field(min_length=3, max_length=1000)
    minimum_fix: str = Field(min_length=3, max_length=1000)
    stop_condition: str = Field(min_length=3, max_length=1000)


class ActiveSlice(StrictModel):
    slice_id: str = Field(pattern=r"^G0[1-7]-[A-Z0-9-]{3,80}$")
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    work_kind: ScopeWorkKind
    phase: ScopePhase
    user_outcome: str = Field(min_length=3, max_length=1000)
    acceptance_refs: list[str] = Field(min_length=1, max_length=20)
    allowed_paths: list[str] = Field(min_length=1, max_length=80)
    minimum_change: str = Field(min_length=3, max_length=1000)
    forbidden_mechanisms: list[str] = Field(min_length=1, max_length=30)
    evidence_invalidated: list[str] = Field(default_factory=list, max_length=30)
    stop_condition: str = Field(min_length=3, max_length=1000)
    blocking_issue: ScopeBlockingIssue | None = None
    hardening_decision_ref: str | None = Field(default=None, min_length=3, max_length=500)
    freeze_parent_commit: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40}$",
    )
    preflight_entrypoints: list[str] = Field(default_factory=list, max_length=20)
    preflight_required_tokens: dict[str, list[str]] = Field(
        default_factory=dict,
        max_length=20,
    )

    @model_validator(mode="after")
    def scope_contract_is_complete(self) -> "ActiveSlice":
        self.allowed_paths = [
            _normalize_repository_path(path) for path in self.allowed_paths
        ]
        self.preflight_entrypoints = [
            _normalize_repository_path(path) for path in self.preflight_entrypoints
        ]
        self.preflight_required_tokens = {
            _normalize_repository_path(path): tokens
            for path, tokens in self.preflight_required_tokens.items()
        }
        if len(self.allowed_paths) != len(set(self.allowed_paths)):
            raise ValueError("active slice allowed paths must be unique")
        unknown_preflight_paths = set(self.preflight_required_tokens) - set(
            self.preflight_entrypoints
        )
        if unknown_preflight_paths:
            raise ValueError("preflight token paths must also be entrypoints")
        if any(not token.strip() for tokens in self.preflight_required_tokens.values() for token in tokens):
            raise ValueError("preflight tokens must not be blank")
        if self.work_kind == "CURRENT_GATE_FIX" and self.blocking_issue is None:
            raise ValueError("CURRENT_GATE_FIX requires a reproducible blocking issue")
        if self.work_kind != "CURRENT_GATE_FIX" and self.blocking_issue is not None:
            raise ValueError("only CURRENT_GATE_FIX may declare a blocking issue")
        if self.work_kind == "HARDENING" and self.hardening_decision_ref is None:
            raise ValueError("HARDENING requires an explicit G07 decision reference")
        if self.work_kind != "HARDENING" and self.hardening_decision_ref is not None:
            raise ValueError("only HARDENING may declare a hardening decision")
        if self.phase in {"EVIDENCE_FROZEN", "GATE_RUNNING"}:
            if self.freeze_parent_commit is None:
                raise ValueError("frozen scope phases require freeze_parent_commit")
        elif self.freeze_parent_commit is not None:
            raise ValueError("freeze_parent_commit is only valid after freeze")
        return self


class WorkPackageRegistry(StrictModel):
    schema_version: Literal[
        "work-package-registry-v1",
        "work-package-registry-v2",
    ] = "work-package-registry-v1"
    program_id: Literal["TC-VNEXT-2026"] = "TC-VNEXT-2026"
    active_goal_sequence: int = Field(ge=1, le=7)
    active_goal_id: str = Field(pattern=r"^TC-VNEXT-G0[1-7]-[A-Z0-9-]+$")
    mainline_phase: MainlinePhase
    gate_profile: GateProfile
    guidance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mismatch_policy: Literal["READ_ONLY"] = "READ_ONLY"
    max_parallel_writers: Literal[3] = 3
    max_prepared_next_goal_packages: Literal[2] = 2
    integration_order: list[str] = Field(default_factory=list, max_length=20)
    e2e_after_all_merges: bool | None = None
    scope_guard_version: Literal["scope-guard-v1"] | None = None
    scope_policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    active_slice: ActiveSlice | None = None
    packages: list[WorkPackageBinding] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def execution_topology_is_safe(self) -> "WorkPackageRegistry":
        expected_prefix = f"TC-VNEXT-G{self.active_goal_sequence:02d}-"
        if not self.active_goal_id.startswith(expected_prefix):
            raise ValueError("work package registry active Goal sequence disagrees")
        expected_phase = _mainline_phase_for_sequence(self.active_goal_sequence)
        if self.mainline_phase != expected_phase:
            raise ValueError("work package registry uses the wrong mainline phase")
        expected_profile = (
            "HARDENED_CANDIDATE_GATE"
            if self.active_goal_sequence == 7
            else "CORE_AGENT_GATE"
        )
        if self.gate_profile != expected_profile:
            raise ValueError("work package registry uses the wrong Gate profile")
        if (self.scope_guard_version is None) != (self.active_slice is None):
            raise ValueError("scope guard version and active slice must be installed together")
        if (self.scope_guard_version is None) != (self.scope_policy_sha256 is None):
            raise ValueError("scope guard version and policy hash must be installed together")

        package_ids = [item.package_id for item in self.packages]
        branches = [item.branch for item in self.packages]
        merge_orders = [item.merge_order for item in self.packages]
        if len(package_ids) != len(set(package_ids)):
            raise ValueError("work package IDs must be unique")
        if len(branches) != len(set(branches)):
            raise ValueError("work package branches must be unique")
        if len(merge_orders) != len(set(merge_orders)):
            raise ValueError("work package merge order must be unique")

        if self.schema_version == "work-package-registry-v2":
            if self.e2e_after_all_merges is not True:
                raise ValueError("v2 work package registry must run E2E after all merges")
            current_contributors = [
                item
                for item in self.packages
                if item.goal_id == self.active_goal_id and item.role == "CONTRIBUTOR"
            ]
            expected_integration_order = [
                item.package_id
                for item in sorted(current_contributors, key=lambda item: item.merge_order)
            ]
            if self.integration_order != expected_integration_order:
                raise ValueError(
                    "v2 integration order must list every current contributor by merge order"
                )
            if self.active_goal_id == "TC-VNEXT-G02-MAP-STAY" and (
                self.integration_order
                != [
                    "WP-G02-STAY-DOMAIN",
                    "WP-G02-MAP-STAY-BACKEND",
                    "WP-G02-MAP-THEATER-UI",
                ]
            ):
                raise ValueError(
                    "G02 integration order must be stay domain, backend/API, then map UI"
                )

            protected = set(WORK_PACKAGE_PROTECTED_PATHS)
            active_worktrees: list[str] = []
            for item in self.packages:
                sequence = int(item.goal_id.split("-G", maxsplit=1)[1][:2])
                if sequence != self.active_goal_sequence:
                    continue
                if item.status == "PREPARED_NOT_INTEGRATED":
                    raise ValueError(
                        "current Goal packages must wait with WAITING_FOR_WRITER_SLOT"
                    )
                required_common = (
                    item.remote_branch,
                    item.worktree_path,
                    item.execution_mode,
                    item.dialogue_ref,
                )
                if item.role == "INTEGRATOR":
                    if any(value is None for value in required_common):
                        raise ValueError(
                            "v2 integrator requires branch, worktree and primary dialogue binding"
                        )
                else:
                    if not protected.issubset(item.forbidden_paths):
                        raise ValueError(
                            "contributor must forbid every integrator-owned path"
                        )
                    required_prompt = (
                        item.remote_branch,
                        item.worktree_path,
                        item.execution_mode,
                        item.prompt_path,
                        item.prompt_sha256,
                        item.registry_activation_commit,
                    )
                    if any(value is None for value in required_prompt):
                        raise ValueError(
                            "v2 contributor requires a complete prompt, branch and worktree binding"
                        )
                    if item.status != "WAITING_FOR_WRITER_SLOT" and item.dialogue_ref is None:
                        raise ValueError(
                            "started contributor requires an independent function dialogue reference"
                        )
                    if item.prompt_path is not None and not item.prompt_path.startswith(
                        "docs/governance/work-package-prompts/"
                    ):
                        raise ValueError(
                            "contributor prompt must use the governed prompt directory"
                        )
                if item.worktree_path is not None and item.status != "DEFERRED":
                    active_worktrees.append(item.worktree_path.casefold())
                if item.status in {"READY_TO_MERGE", "MERGED"}:
                    if item.ready_commit is None:
                        raise ValueError(
                            "READY_TO_MERGE and MERGED packages require ready_commit"
                        )
                elif item.ready_commit is not None:
                    raise ValueError("ready_commit is only valid after package freeze")
                if item.status == "MERGED":
                    if item.merged_commit is None:
                        raise ValueError("MERGED package requires merged_commit")
                elif item.merged_commit is not None:
                    raise ValueError("merged_commit is only valid for MERGED packages")
            if len(active_worktrees) != len(set(active_worktrees)):
                raise ValueError("work package worktrees must be unique")
        else:
            protected_v1 = set(WORK_PACKAGE_PROTECTED_PATHS_V1)
            for item in self.packages:
                if item.role == "CONTRIBUTOR" and not protected_v1.issubset(
                    item.forbidden_paths
                ):
                    raise ValueError(
                        "contributor must forbid every integrator-owned path"
                    )

        known = {item.package_id: item for item in self.packages}
        prepared_next = 0
        active_baselines: set[str] = set()
        coordinated = {
            "PREPARED_NOT_INTEGRATED",
            "WAITING_FOR_WRITER_SLOT",
            "IN_PROGRESS",
            "READY_TO_MERGE",
            "BLOCKED_EXTERNAL",
        }
        for item in self.packages:
            sequence = int(item.goal_id.split("-G", maxsplit=1)[1][:2])
            if sequence not in {
                self.active_goal_sequence,
                self.active_goal_sequence + 1,
            }:
                raise ValueError("work packages may cover only the active or next Goal")
            if sequence == self.active_goal_sequence + 1:
                if item.role != "CONTRIBUTOR":
                    raise ValueError("next Goal packages must be contributors")
                if item.status not in {"PREPARED_NOT_INTEGRATED", "DEFERRED"}:
                    raise ValueError("next Goal work packages may only be prepared")
                if item.status == "PREPARED_NOT_INTEGRATED":
                    prepared_next += 1
            if item.status in coordinated:
                active_baselines.add(item.baseline_commit)
            for dependency in item.dependencies:
                dependency_item = known.get(dependency)
                if dependency_item is None:
                    raise ValueError("work package dependency is absent")
                dependency_sequence = int(
                    dependency_item.goal_id.split("-G", maxsplit=1)[1][:2]
                )
                if (
                    sequence == self.active_goal_sequence
                    and dependency_sequence > sequence
                ):
                    raise ValueError("current Goal cannot depend on the next Goal")
                if dependency_item.merge_order >= item.merge_order:
                    raise ValueError("work package dependency must merge earlier")
        if prepared_next > self.max_prepared_next_goal_packages:
            raise ValueError("too many next Goal work packages were prepared")

        active_writers = [
            item
            for item in self.packages
            if (
                item.role == "INTEGRATOR" and item.status not in {"MERGED", "DEFERRED"}
            )
            or (
                item.goal_id == self.active_goal_id
                and item.role == "CONTRIBUTOR"
                and item.status in {"IN_PROGRESS", "BLOCKED_EXTERNAL"}
            )
        ]
        if len(active_writers) > self.max_parallel_writers:
            raise ValueError("too many parallel writable work packages")

        terminal = {"MERGED", "DEFERRED"}
        active_integrators = [
            item
            for item in self.packages
            if item.role == "INTEGRATOR" and item.status not in terminal
        ]
        if len(active_integrators) != 1:
            raise ValueError("exactly one active work package integrator is required")
        if active_integrators[0].goal_id != self.active_goal_id:
            raise ValueError("the active integrator must belong to the active Goal")
        if len(active_baselines) != 1:
            raise ValueError("all active work packages must share one exact baseline")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(package_id: str) -> None:
            if package_id in visiting:
                raise ValueError("work package dependency graph contains a cycle")
            if package_id in visited:
                return
            visiting.add(package_id)
            for dependency in known[package_id].dependencies:
                visit(dependency)
            visiting.remove(package_id)
            visited.add(package_id)

        for package_id in known:
            visit(package_id)

        coordinated_packages = [
            item for item in self.packages if item.status in coordinated
        ]
        for index, left in enumerate(coordinated_packages):
            for right in coordinated_packages[index + 1 :]:
                if any(
                    _paths_overlap(left_path, right_path)
                    for left_path in left.owned_paths
                    for right_path in right.owned_paths
                ):
                    raise ValueError("parallel work package owned paths overlap")
        return self


class HardeningDecisionReceipt(StrictModel):
    schema_version: Literal["hardening-decision-receipt-v1"] = (
        "hardening-decision-receipt-v1"
    )
    goal_id: Literal["TC-VNEXT-G07-CANDIDATE"] = "TC-VNEXT-G07-CANDIDATE"
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    threat_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: HardeningDecision
    identified_threats: list[str] = Field(min_length=1, max_length=30)
    selected_controls: list[HardeningControl] = Field(default_factory=list)
    alternative_controls: list[str] = Field(min_length=1, max_length=30)
    residual_risks: list[str] = Field(min_length=1, max_length=30)
    rationale: str = Field(min_length=20, max_length=4000)
    human_evidence: Literal[False] = False
    production_evidence: Literal[False] = False

    @model_validator(mode="after")
    def decision_and_controls_are_consistent(self) -> "HardeningDecisionReceipt":
        if len(self.selected_controls) != len(set(self.selected_controls)):
            raise ValueError("hardening controls must be unique")
        if self.decision == "NOT_REQUIRED_WITH_RATIONALE" and self.selected_controls:
            raise ValueError("non-required hardening cannot enable legacy controls")
        if self.decision == "REQUIRED" and not self.selected_controls:
            raise ValueError("required hardening must name at least one control")
        return self


class HardeningControlVerificationReceipt(StrictModel):
    schema_version: Literal["hardening-control-verification-receipt-v1"] = (
        "hardening-control-verification-receipt-v1"
    )
    goal_id: Literal["TC-VNEXT-G07-CANDIDATE"] = "TC-VNEXT-G07-CANDIDATE"
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    control: HardeningControl
    verifier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: dict[str, str] = Field(min_length=1, max_length=30)
    human_evidence: Literal[False] = False
    production_evidence: Literal[False] = False
    verdict: Literal["PASS"] = "PASS"

    @model_validator(mode="after")
    def evidence_hashes_are_safe(self) -> "HardeningControlVerificationReceipt":
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_.-]{2,79}", key)
            or not re.fullmatch(r"[0-9a-f]{64}", value)
            for key, value in self.evidence_sha256.items()
        ):
            raise ValueError("hardening control evidence bindings are invalid")
        return self


class CandidateGateComponentReceipt(StrictModel):
    schema_version: Literal["candidate-gate-component-receipt-v2"] = (
        "candidate-gate-component-receipt-v2"
    )
    goal_id: Literal["TC-VNEXT-G07-CANDIDATE"] = "TC-VNEXT-G07-CANDIDATE"
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    automated_gate_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    component: AgentGateComponent
    evidence_level: EvidenceLevel
    upstream_artifact_path: dict[str, str] = Field(min_length=1, max_length=100)
    upstream_artifact_sha256: dict[str, str] = Field(min_length=1, max_length=100)
    verifier_path: Literal[
        "backend/evals/agent_gate_v1/candidate_component_verifiers.py"
    ] = "backend/evals/agent_gate_v1/candidate_component_verifiers.py"
    verifier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    isolation_mode: Literal[
        "FRESH_CLEAN_CHECKOUT", "OCI_EPHEMERAL_NO_HOST_MOUNTS"
    ] | None = None
    human_evidence: Literal[False] = False
    production_evidence: Literal[False] = False
    verdict: Literal["PASS"] = "PASS"

    @model_validator(mode="after")
    def component_evidence_is_consistent(self) -> "CandidateGateComponentReceipt":
        expected_level: dict[AgentGateComponent, EvidenceLevel] = {
            "AUTOMATED_PRODUCT_GATE": "AUTOMATED_TEST",
            "LIVE_PROVIDER_GATE": "LIVE_PROVIDER_EVIDENCE",
            "MULTI_AGENT_PANEL": "MULTI_AGENT_SIMULATED_REVIEW",
            "SEALED_AGENT_BLIND": "SEALED_AGENT_BLIND",
        }
        if self.evidence_level != expected_level[self.component]:
            raise ValueError("candidate component uses the wrong evidence level")
        if self.component == "AUTOMATED_PRODUCT_GATE":
            if self.isolation_mode is None:
                raise ValueError("automated candidate component requires isolation mode")
        elif self.isolation_mode is not None:
            raise ValueError("only automated candidate evidence may claim isolation")
        if set(self.upstream_artifact_path) != set(self.upstream_artifact_sha256):
            raise ValueError("candidate component artifact path/hash keys disagree")
        if len(set(self.upstream_artifact_path.values())) != len(
            self.upstream_artifact_path
        ):
            raise ValueError("candidate component artifact paths must be unique")
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_.-]{2,79}", key)
            or not re.fullmatch(r"[0-9a-f]{64}", value)
            for key, value in self.upstream_artifact_sha256.items()
        ):
            raise ValueError("candidate component evidence bindings are invalid")
        if any(
            not Path(value).is_absolute() or len(value) > 500
            for value in self.upstream_artifact_path.values()
        ):
            raise ValueError("candidate component artifact paths must be absolute")
        return self


class G07CandidateGatePassReceipt(StrictModel):
    schema_version: Literal["g07-candidate-agent-gate-pass-v1"] = (
        "g07-candidate-agent-gate-pass-v1"
    )
    goal_id: Literal["TC-VNEXT-G07-CANDIDATE"] = "TC-VNEXT-G07-CANDIDATE"
    gate_profile: Literal["HARDENED_CANDIDATE_GATE"] = "HARDENED_CANDIDATE_GATE"
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    current_goal_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_package_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hardening_decision: HardeningDecision
    hardening_decision_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    component_receipt_sha256: dict[AgentGateComponent, str]
    selected_control_receipt_sha256: dict[HardeningControl, str]
    remote_ref: str = Field(pattern=r"^refs/heads/[A-Za-z0-9._/-]+$")
    remote_subject: str = Field(pattern=r"^[0-9a-f]{40}$")
    remote_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    human_usability_status: Literal["NOT_RUN"] = "NOT_RUN"
    production_status: Literal["NOT_RUN"] = "NOT_RUN"
    verdict: Literal["AGENT_GATE_PASS"] = "AGENT_GATE_PASS"
    completed_at: datetime

    @model_validator(mode="after")
    def all_candidate_evidence_is_present(self) -> "G07CandidateGatePassReceipt":
        if set(self.component_receipt_sha256) != {
            "AUTOMATED_PRODUCT_GATE",
            "LIVE_PROVIDER_GATE",
            "MULTI_AGENT_PANEL",
            "SEALED_AGENT_BLIND",
        }:
            raise ValueError("G07 candidate Gate requires all four evidence components")
        selected = set(self.selected_control_receipt_sha256)
        if self.hardening_decision == "NOT_REQUIRED_WITH_RATIONALE" and selected:
            raise ValueError("non-required hardening cannot carry control receipts")
        if self.hardening_decision == "REQUIRED" and not selected:
            raise ValueError("required hardening must verify selected controls")
        return self


class CurrentGoalBinding(StrictModel):
    schema_version: Literal[
        "current-goal-binding-v1", "current-goal-binding-v2", "current-goal-binding-v3"
    ] = "current-goal-binding-v1"
    program_id: Literal["TC-VNEXT-2026"] = "TC-VNEXT-2026"
    goal_sequence: int = Field(ge=1, le=7)
    goal_id: str = Field(pattern=r"^TC-VNEXT-G0[1-7]-[A-Z0-9-]+$")
    status: Literal["APPROVED", "IN_PROGRESS"]
    canonical_candidate_ref: Literal[
        "refs/heads/codex/trip-check-product-reset",
        "refs/heads/codex/g07-candidate",
        "refs/heads/codex/g07-candidate-cycle-2",
        "refs/heads/codex/g07-candidate-cycle-3",
    ] = "refs/heads/codex/trip-check-product-reset"
    predecessor_goal_id: str
    predecessor_completion_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    automated_gate_contract_path: str = Field(min_length=1, max_length=300)
    automated_gate_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_profile: GateProfile
    mainline_phase: MainlinePhase | None = None
    work_package_registry_path: Literal[
        "docs/governance/current_work_packages.json"
    ] | None = None
    implementation_baseline_commit: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40}$",
    )
    last_completed_goal_id: str | None = None
    next_goal_id: str | None = None
    next_goal_status: str | None = None
    program_state: str | None = None
    candidate_gate_contract_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
    )
    candidate_gate_contract_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def sequence_matches_goal(self) -> "CurrentGoalBinding":
        expected_prefix = f"TC-VNEXT-G{self.goal_sequence:02d}-"
        if not self.goal_id.startswith(expected_prefix):
            raise ValueError("current Goal sequence and Goal ID disagree")
        path = self.automated_gate_contract_path.replace("\\", "/")
        if path.startswith("/") or ".." in Path(path).parts:
            raise ValueError("automated Gate contract path must be repository-relative")
        expected_profile = (
            "HARDENED_CANDIDATE_GATE"
            if self.goal_sequence == 7
            else "CORE_AGENT_GATE"
        )
        if self.gate_profile != expected_profile:
            raise ValueError("current Goal uses the wrong Agent Gate profile")
        expected_phase = _mainline_phase_for_sequence(self.goal_sequence)
        if self.schema_version in {
            "current-goal-binding-v2",
            "current-goal-binding-v3",
        }:
            if self.mainline_phase != expected_phase:
                raise ValueError("current Goal uses the wrong mainline phase")
            if self.work_package_registry_path is None:
                raise ValueError("current Goal v2/v3 must bind the work package registry")
            if (
                self.schema_version == "current-goal-binding-v3"
                and self.goal_sequence == 7
            ):
                if self.canonical_candidate_ref not in {
                    "refs/heads/codex/g07-candidate",
                    "refs/heads/codex/g07-candidate-cycle-2",
                    "refs/heads/codex/g07-candidate-cycle-3",
                }:
                    raise ValueError("current Goal v3 must bind the G07 candidate ref")
                required_v3 = (
                    self.implementation_baseline_commit,
                    self.last_completed_goal_id,
                    self.next_goal_id,
                    self.next_goal_status,
                    self.program_state,
                    self.candidate_gate_contract_path,
                    self.candidate_gate_contract_sha256,
                )
                if any(value is None for value in required_v3):
                    raise ValueError("current Goal v3 candidate binding is incomplete")
                assert self.candidate_gate_contract_path is not None
                candidate_path = self.candidate_gate_contract_path.replace("\\", "/")
                if candidate_path.startswith("/") or ".." in Path(candidate_path).parts:
                    raise ValueError(
                        "candidate Gate contract path must be repository-relative"
                    )
        else:
            # Historical commits remain readable without rewriting their v1 bytes.
            self.mainline_phase = expected_phase
            self.work_package_registry_path = (
                "docs/governance/current_work_packages.json"
            )
        return self


class CurrentGoalDocumentState(StrictModel):
    schema_version: Literal["current-goal-document-state-v1"] = (
        "current-goal-document-state-v1"
    )
    program_id: Literal["TC-VNEXT-2026"] = "TC-VNEXT-2026"
    goal_id: str = Field(pattern=r"^TC-VNEXT-G0[1-7]-[A-Z0-9-]+$")
    goal_status: Literal["APPROVED", "IN_PROGRESS"]
    required_gate: str = Field(min_length=8, max_length=160)
    completion_status: Literal["PENDING"] = "PENDING"
    gate_result: Literal["AGENT_GATE_NOT_RUN"] = "AGENT_GATE_NOT_RUN"
    goal_archived: Literal[False] = False
    next_goal_id: str = Field(
        pattern=r"^(TC-VNEXT-G0[2-7]-[A-Z0-9-]+|TC-H1-G01-HUMAN-USABILITY)$"
    )
    next_activated: Literal[False] = False
    h1_status: Literal["NOT_RUN"] = "NOT_RUN"
    production_status: Literal["NOT_RUN"] = "NOT_RUN"
    commercial_status: Literal["NOT_RUN"] = "NOT_RUN"

    @model_validator(mode="after")
    def pre_pass_state_is_consistent(self) -> "CurrentGoalDocumentState":
        if "AGENT_GATE_PASS" not in self.required_gate:
            raise ValueError("current Goal required gate must include AGENT_GATE_PASS")
        current_sequence = int(self.goal_id.split("-G", maxsplit=1)[1][:2])
        expected_next = (
            "TC-H1-G01-HUMAN-USABILITY"
            if current_sequence == 7
            else f"TC-VNEXT-G{current_sequence + 1:02d}-"
        )
        if not self.next_goal_id.startswith(expected_next):
            raise ValueError("current Goal document must name the next Program Goal")
        return self


class AuthorityAnchorReceipt(StrictModel):
    schema_version: Literal["agent-gate-authority-anchor-receipt-v1"] = (
        "agent-gate-authority-anchor-receipt-v1"
    )
    authority_generation: int = Field(ge=1, le=7)
    canonical_candidate_ref: Literal[
        "refs/heads/codex/trip-check-product-reset"
    ] = "refs/heads/codex/trip-check-product-reset"
    anchor_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    anchor_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    authority_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    program_core_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    immutable_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    public_key_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    custody_registry_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    activated_at: datetime
    human_evidence: Literal[False] = False
    authority_signature: DetachedAuthoritySignature

    @model_validator(mode="after")
    def uses_custody_authority(self) -> "AuthorityAnchorReceipt":
        if self.authority_signature.authority_role != "SEALED_CUSTODY":
            raise ValueError("authority anchor requires the pinned custody authority")
        return self


class AgentTaskAttestation(StrictModel):
    task_id: str = Field(min_length=8, max_length=160)
    model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    reasoning_effort: Literal["xhigh", "ultra"]
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    subject_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    started_at: datetime
    completed_at: datetime
    frozen_at: datetime
    context_fork: Literal["none"] = "none"
    isolated_context: Literal[True] = True
    human_evidence: Literal[False] = False
    saw_prior_verdict: Literal[False] = False

    @model_validator(mode="after")
    def completion_is_not_before_start(self) -> "AgentTaskAttestation":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be before started_at")
        if self.frozen_at < self.completed_at:
            raise ValueError("frozen_at cannot be before completed_at")
        return self


class EvidenceRef(StrictModel):
    kind: Literal["COMMAND", "TEST", "SCREENSHOT", "LOG", "FILE", "PROVIDER_RECEIPT"]
    artifact_path: str = Field(min_length=1, max_length=500)
    storage: Literal["REPOSITORY", "REPOSITORY_EXTERNAL"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ScenarioCheck(StrictModel):
    status: ScenarioStatus
    evidence: list[EvidenceRef] = Field(max_length=20)
    not_run_reason: str | None = Field(default=None, min_length=8, max_length=500)

    @model_validator(mode="after")
    def evidence_matches_status(self) -> "ScenarioCheck":
        if self.status == "NOT_RUN":
            if self.evidence:
                raise ValueError("NOT_RUN scenarios cannot claim execution evidence")
            if self.not_run_reason is None:
                raise ValueError("NOT_RUN scenarios require a reason")
        else:
            if not self.evidence:
                raise ValueError("executed scenarios require at least one evidence artifact")
            if self.not_run_reason is not None:
                raise ValueError("executed scenarios cannot carry a NOT_RUN reason")
        return self


class ScenarioCoverage(StrictModel):
    normal: ScenarioCheck
    ambiguous: ScenarioCheck
    boundary: ScenarioCheck
    adversarial: ScenarioCheck
    provider_failure: ScenarioCheck
    privacy: ScenarioCheck
    concurrency: ScenarioCheck


class AgentFinding(StrictModel):
    finding_id: str = Field(pattern=r"^AGF-[A-Z0-9-]{4,80}$")
    severity: Severity
    category: str = Field(min_length=3, max_length=120)
    expected: str = Field(min_length=8, max_length=2000)
    observed: str = Field(min_length=8, max_length=2000)
    reproduction_steps: list[str] = Field(min_length=1, max_length=20)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=20)


class AgentGateReviewReceipt(StrictModel):
    schema_version: Literal["agent-gate-review-v1"] = AGENT_GATE_SCHEMA_VERSION
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_role: ReviewerRole
    attestation: AgentTaskAttestation
    scenario_coverage: ScenarioCoverage
    findings: list[AgentFinding]
    checks_not_run: list[str]
    evidence_level: Literal["MULTI_AGENT_SIMULATED_REVIEW"] = "MULTI_AGENT_SIMULATED_REVIEW"
    verdict: ReviewVerdict

    @model_validator(mode="after")
    def binding_and_verdict_are_consistent(self) -> "AgentGateReviewReceipt":
        if (
            self.attestation.subject_commit != self.candidate_commit
            or self.attestation.subject_tree != self.candidate_tree
        ):
            raise ValueError("review attestation must bind the candidate commit and tree")
        if self.attestation.reasoning_effort != "xhigh":
            raise ValueError("gate reviewers require xhigh reasoning effort")
        blocking = any(item.severity in {"P0", "P1"} for item in self.findings)
        if blocking and self.verdict == "PASS":
            raise ValueError("a review with P0/P1 findings cannot pass")
        if self.checks_not_run and self.verdict == "PASS":
            raise ValueError("a review with checks_not_run cannot pass")
        coverage = (
            item.status
            for item in self.scenario_coverage.__dict__.values()
        )
        if any(status != "PASS" for status in coverage) and self.verdict == "PASS":
            raise ValueError("a review with incomplete or failed scenario coverage cannot pass")
        return self


class AdjudicatedFinding(StrictModel):
    finding_id: str = Field(pattern=r"^AGF-[A-Z0-9-]{4,80}$")
    source_review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    severity: Severity
    scope_disposition: Literal["IN_CURRENT_GOAL", "DEFERRED_BY_PROGRAM", "NOT_APPLICABLE"]
    resolution: Literal["ACCEPTED", "REJECTED", "DUPLICATE"]
    rationale: str = Field(min_length=12, max_length=2000)


class AgentGateAdjudicationReceipt(StrictModel):
    schema_version: Literal["agent-gate-adjudication-v1"] = AGENT_GATE_ADJUDICATION_SCHEMA_VERSION
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_review_sha256: list[str] = Field(min_length=3, max_length=3)
    attestation: AgentTaskAttestation
    findings: list[AdjudicatedFinding]
    accepted_p0_count: int = Field(ge=0)
    accepted_p1_count: int = Field(ge=0)
    accepted_in_scope_p2_count: int = Field(ge=0)
    required_scenario_union_complete: bool
    evidence_level: Literal["MULTI_AGENT_SIMULATED_REVIEW"] = "MULTI_AGENT_SIMULATED_REVIEW"
    verdict: ReviewVerdict

    @model_validator(mode="after")
    def panel_is_distinct_and_verdict_is_consistent(self) -> "AgentGateAdjudicationReceipt":
        if len(set(self.source_review_sha256)) != 3:
            raise ValueError("adjudication requires three distinct review receipts")
        if (
            self.attestation.subject_commit != self.candidate_commit
            or self.attestation.subject_tree != self.candidate_tree
        ):
            raise ValueError("adjudication must bind the candidate commit and tree")
        if self.attestation.reasoning_effort != "ultra":
            raise ValueError("gate adjudication requires ultra reasoning effort")
        accepted = [item for item in self.findings if item.resolution == "ACCEPTED"]
        expected_counts = (
            sum(item.severity == "P0" for item in accepted),
            sum(item.severity == "P1" for item in accepted),
            sum(
                item.severity == "P2" and item.scope_disposition == "IN_CURRENT_GOAL"
                for item in accepted
            ),
        )
        actual_counts = (
            self.accepted_p0_count,
            self.accepted_p1_count,
            self.accepted_in_scope_p2_count,
        )
        if actual_counts != expected_counts:
            raise ValueError("accepted finding counts do not match adjudicated findings")
        if (
            self.accepted_p0_count
            or self.accepted_p1_count
            or self.accepted_in_scope_p2_count
            or not self.required_scenario_union_complete
        ) and self.verdict == "PASS":
            raise ValueError("blocking findings, in-scope P2, or incomplete coverage cannot pass")
        return self


class SealedBlindMetricThreshold(StrictModel):
    metric: str = Field(pattern=r"^[a-z][a-z0-9_.]{2,119}$")
    operator: ThresholdOperator
    value: bool | int | float


class SealedAgentBlindThresholds(StrictModel):
    schema_version: Literal["sealed-agent-blind-thresholds-v1"] = (
        SEALED_AGENT_BLIND_THRESHOLDS_SCHEMA_VERSION
    )
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    required_metric_names: list[str] = Field(min_length=1, max_length=100)
    conditions: list[SealedBlindMetricThreshold] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def metrics_are_unique(self) -> "SealedAgentBlindThresholds":
        metrics = [item.metric for item in self.conditions]
        if len(metrics) != len(set(metrics)):
            raise ValueError("sealed blind threshold metrics must be unique")
        if len(self.required_metric_names) != len(set(self.required_metric_names)):
            raise ValueError("sealed blind required metric names must be unique")
        if not set(metrics).issubset(self.required_metric_names):
            raise ValueError("sealed blind conditions must use required metrics")
        _validate_aggregate_metrics({metric: 0 for metric in self.required_metric_names})
        return self


class TruthBundleCommitment(StrictModel):
    algorithm: Literal["HMAC-SHA256"] = "HMAC-SHA256"
    key_id: str = Field(pattern=r"^CUSTODY-[A-Z0-9-]{6,100}$")
    value: str = Field(pattern=r"^[0-9a-f]{64}$")


class SealedScoreInputManifest(StrictModel):
    schema_version: Literal["sealed-score-input-manifest-v2"] = (
        "sealed-score-input-manifest-v2"
    )
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    tranche_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    one_shot_nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prediction_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    truth_bundle_commitment: TruthBundleCommitment
    case_set_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_case_count: int = Field(ge=1)
    prediction_case_count: int = Field(ge=1)
    truth_case_count: int = Field(ge=1)
    scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    thresholds_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_signature: DetachedAuthoritySignature

    @model_validator(mode="after")
    def case_counts_and_authority_match(self) -> "SealedScoreInputManifest":
        if len(
            {
                self.input_case_count,
                self.prediction_case_count,
                self.truth_case_count,
            }
        ) != 1:
            raise ValueError("sealed scorer input, prediction, and truth counts must match")
        if self.authority_signature.authority_role != "SEALED_CUSTODY":
            raise ValueError("sealed score inputs require the pinned custody authority")
        return self


class SealedAgentBlindScoreReceipt(StrictModel):
    schema_version: Literal["sealed-agent-blind-score-receipt-v2"] = (
        SEALED_AGENT_BLIND_SCORE_SCHEMA_VERSION
    )
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    tranche_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    thresholds_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score_input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prediction_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    truth_bundle_commitment: TruthBundleCommitment
    case_set_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scored_case_count: int = Field(ge=1)
    custody_registry_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mint_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    one_shot_nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_by: Literal["SEALED_AGENT_BLIND_SCORER"] = "SEALED_AGENT_BLIND_SCORER"
    aggregate_metrics: dict[str, float | int | bool] = Field(min_length=1, max_length=100)
    taxonomy_counts: dict[BlindErrorCategory, int]
    unmapped_error_count: int = Field(ge=0)
    required_gate_metrics_passed: bool
    raw_truth_in_receipt: Literal[False] = False
    completed_at: datetime
    authority_signature: DetachedAuthoritySignature

    @model_validator(mode="after")
    def metrics_are_safe(self) -> "SealedAgentBlindScoreReceipt":
        _validate_aggregate_metrics(self.aggregate_metrics)
        if set(self.taxonomy_counts) != set(BLIND_ERROR_CATEGORY_ORDER):
            raise ValueError("sealed scorer taxonomy count set is incomplete")
        hard_counts = {
            "WRONG_CITY": self.aggregate_metrics.get("wrong_city_auto_match_count"),
            "WRONG_CATEGORY": self.aggregate_metrics.get(
                "wrong_category_auto_match_count"
            ),
            "NON_ATOMIC_PLACE": self.aggregate_metrics.get(
                "forbidden_content_as_place_count"
            ),
            "MENTION_FALSE_POSITIVE": self.aggregate_metrics.get(
                "executable_mentions.fp"
            ),
            "MENTION_FALSE_NEGATIVE": self.aggregate_metrics.get(
                "executable_mentions.fn"
            ),
            "DAY_ASSIGNMENT": self.aggregate_metrics.get(
                "day_assignment.error_count"
            ),
            "ROLE_CLASSIFICATION": self.aggregate_metrics.get(
                "role_classification.error_count"
            ),
            "PROVIDER_RESOLUTION": self.aggregate_metrics.get(
                "provider_resolution.error_count"
            ),
            "PUBLIC_LEAK": (
                int(
                    self.aggregate_metrics.get(
                        "public_projection.forbidden_key_hits",
                        -1,
                    )
                )
                + int(
                    self.aggregate_metrics.get(
                        "public_projection.full_source_leak_hits",
                        -1,
                    )
                )
            ),
            "LATENCY": self.aggregate_metrics.get("latency.violation_count"),
            "OTHER_AGGREGATED": self.aggregate_metrics.get(
                "other_aggregated_error_count"
            ),
        }
        if any(
            hard_counts[name] is None
            or int(hard_counts[name]) != self.taxonomy_counts[name]
            for name in hard_counts
        ):
            raise ValueError("sealed scorer taxonomy contradicts hard error metrics")
        if self.unmapped_error_count or self.authority_signature.authority_role != "SEALED_CUSTODY":
            raise ValueError("sealed scorer has unmapped errors or an untrusted authority")
        return self


class SealedAgentBlindMintReceipt(StrictModel):
    schema_version: Literal["sealed-agent-blind-mint-receipt-v2"] = (
        SEALED_AGENT_BLIND_MINT_SCHEMA_VERSION
    )
    custody_registry_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    custody_registry_path_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mint_sequence: int = Field(ge=1)
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    tranche_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    one_shot_nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    custodian_task_id: str = Field(min_length=8, max_length=160)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    thresholds_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["MINTED"] = "MINTED"
    minted_at: datetime
    authority_signature: DetachedAuthoritySignature

    @model_validator(mode="after")
    def mint_uses_custody_authority(self) -> "SealedAgentBlindMintReceipt":
        if self.authority_signature.authority_role != "SEALED_CUSTODY":
            raise ValueError("sealed blind mint requires the pinned custody authority")
        return self


class SealedAgentBlindReceipt(StrictModel):
    schema_version: Literal["sealed-agent-blind-receipt-v2"] = SEALED_AGENT_BLIND_SCHEMA_VERSION
    gate_profile: GateProfile
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    tranche_commitment_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    one_shot_nonce_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    attempt_commitment_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    custody_registry_identity_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authority_policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    mint_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    thresholds_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deterministic_score_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    score_input_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prediction_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    truth_bundle_commitment: TruthBundleCommitment | None = None
    case_set_commitment_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    scored_case_count: int = Field(ge=1)
    custodian_task_id: str = Field(min_length=8, max_length=160)
    model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    reasoning_effort: Literal["ultra"] = "ultra"
    process_isolation: Literal[True] = True
    organizational_independence_claimed: Literal[False] = False
    human_evidence: Literal[False] = False
    blind_truth_returned_to_developer: Literal[False] = False
    raw_truth_stored_in_repository: Literal[False] = False
    one_shot_nonce_consumed: Literal[True] = True
    aggregate_metrics: dict[str, float | int | bool] = Field(min_length=1, max_length=100)
    taxonomy_counts: dict[BlindErrorCategory, int]
    error_taxonomy: list[BlindErrorCategory]
    required_gate_metrics_passed: bool
    evidence_level: Literal["SEALED_AGENT_BLIND"] = "SEALED_AGENT_BLIND"
    verdict: ReviewVerdict
    completed_at: datetime
    authority_signature: DetachedAuthoritySignature | None = None

    @model_validator(mode="after")
    def verdict_matches_deterministic_gate_result(self) -> "SealedAgentBlindReceipt":
        _validate_aggregate_metrics(self.aggregate_metrics)
        if set(self.taxonomy_counts) != set(BLIND_ERROR_CATEGORY_ORDER):
            raise ValueError("sealed blind taxonomy count set is incomplete")
        if self.verdict == "PASS" and not self.required_gate_metrics_passed:
            raise ValueError("sealed blind cannot pass when required metrics failed")
        if self.verdict == "FAIL" and self.required_gate_metrics_passed:
            raise ValueError("sealed blind FAIL contradicts required metrics result")
        expected_taxonomy = [
            name for name in BLIND_ERROR_CATEGORY_ORDER if self.taxonomy_counts[name] > 0
        ]
        if self.error_taxonomy != expected_taxonomy:
            raise ValueError("sealed blind error taxonomy contradicts scorer counts")
        hardened_custody_values = (
            self.tranche_commitment_sha256,
            self.one_shot_nonce_sha256,
            self.attempt_commitment_sha256,
            self.custody_registry_identity_sha256,
            self.authority_policy_sha256,
            self.mint_receipt_sha256,
            self.score_input_manifest_sha256,
            self.truth_bundle_commitment,
            self.case_set_commitment_sha256,
        )
        if self.gate_profile == "CORE_AGENT_GATE":
            if not re.match(r"^TC-VNEXT-G0[1-6]-", self.goal_id):
                raise ValueError("CORE sealed blind is restricted to G01-G06")
            if any(value is not None for value in hardened_custody_values):
                raise ValueError("CORE sealed blind cannot claim HARDENED custody evidence")
            if self.authority_signature is not None:
                raise ValueError("CORE sealed blind cannot carry an authority signature")
        elif (
            any(
                value is None
                for value in (
                    *hardened_custody_values,
                    self.deterministic_score_receipt_sha256,
                )
            )
            or self.authority_signature is None
            or self.authority_signature.authority_role != "SEALED_CUSTODY"
        ):
            raise ValueError("HARDENED sealed blind requires complete custody authority")
        return self


class AgentGateComponentReceiptBase(StrictModel):
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_path: str = Field(min_length=1, max_length=300)
    verifier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_receipt_path: str = Field(min_length=1, max_length=500)
    verification_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks_not_run: list[str] = Field(max_length=0)
    human_evidence: Literal[False] = False
    verdict: Literal["PASS"] = "PASS"
    completed_at: datetime
    authority_signature: DetachedAuthoritySignature


class AutomatedProductGateReceipt(AgentGateComponentReceiptBase):
    schema_version: Literal["automated-product-gate-receipt-v2"] = (
        "automated-product-gate-receipt-v2"
    )
    component: Literal["AUTOMATED_PRODUCT_GATE"] = "AUTOMATED_PRODUCT_GATE"
    gate_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executed_check_count: int = Field(ge=1)
    failed_check_count: Literal[0] = 0
    evidence_level: Literal["AUTOMATED_TEST"] = "AUTOMATED_TEST"
    gate_contract_path: str = Field(min_length=1, max_length=300)
    execution_manifest_path: str = Field(min_length=1, max_length=500)
    isolation_mode: Literal["OCI_EPHEMERAL_NO_HOST_MOUNTS"]
    runner_recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_entrypoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_context_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runner_image_archive_format: Literal["DOCKER_IMAGE_ARCHIVE_V1"]
    runner_image_archive_path: str = Field(min_length=1, max_length=500)
    runner_image_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_image_archive_size: int = Field(ge=1)
    network_access: Literal[False] = False
    host_mount_count: Literal[0] = 0
    host_pid_namespace: Literal[False] = False

    @model_validator(mode="after")
    def uses_automated_authority(self) -> "AutomatedProductGateReceipt":
        if self.authority_signature.authority_role != "AUTOMATED_PRODUCT_GATE":
            raise ValueError("automated product Gate requires its pinned authority")
        return self


class LiveProviderGateReceipt(AgentGateComponentReceiptBase):
    schema_version: Literal["live-provider-gate-receipt-v2"] = (
        "live-provider-gate-receipt-v2"
    )
    component: Literal["LIVE_PROVIDER_GATE"] = "LIVE_PROVIDER_GATE"
    amap_provider_receipt_index_path: str = Field(min_length=1, max_length=500)
    amap_provider_receipt_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    amap_runtime_receipt_path: str = Field(min_length=1, max_length=500)
    amap_runtime_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qwen_runtime_receipt_path: str = Field(min_length=1, max_length=500)
    qwen_runtime_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split: Literal["dev", "validation", "frozen_blind"]
    amap_provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    amap_database_export_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    amap_http_receipt_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    amap_live_effect_count: int = Field(ge=1)
    qwen_live_effect_count: int = Field(ge=1)
    fixture_effect_count: Literal[0] = 0
    evidence_level: Literal["LIVE_PROVIDER_EVIDENCE"] = "LIVE_PROVIDER_EVIDENCE"

    @model_validator(mode="after")
    def uses_live_provider_authority(self) -> "LiveProviderGateReceipt":
        if self.authority_signature.authority_role != "LIVE_PROVIDER_GATE":
            raise ValueError("live Provider Gate requires its pinned authority")
        return self


class MultiAgentPanelGateReceipt(AgentGateComponentReceiptBase):
    schema_version: Literal["multi-agent-panel-gate-receipt-v2"] = (
        "multi-agent-panel-gate-receipt-v2"
    )
    component: Literal["MULTI_AGENT_PANEL"] = "MULTI_AGENT_PANEL"
    review_paths: list[str] = Field(min_length=3, max_length=3)
    adjudication_path: str = Field(min_length=1, max_length=500)
    expected_input_bundle_sha256: dict[ReviewerRole, str]
    review_receipt_sha256: list[str] = Field(min_length=3, max_length=3)
    adjudication_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_task_count: Literal[3] = 3
    adjudicator_task_count: Literal[1] = 1
    accepted_p0_count: Literal[0] = 0
    accepted_p1_count: Literal[0] = 0
    accepted_in_scope_p2_count: Literal[0] = 0
    required_scenario_union_complete: Literal[True] = True
    evidence_level: Literal["MULTI_AGENT_SIMULATED_REVIEW"] = (
        "MULTI_AGENT_SIMULATED_REVIEW"
    )

    @model_validator(mode="after")
    def uses_panel_authority(self) -> "MultiAgentPanelGateReceipt":
        if len(set(self.review_paths)) != 3:
            raise ValueError("multi-Agent panel requires three distinct review paths")
        if set(self.expected_input_bundle_sha256) != {
            "PRODUCT_UX",
            "SEMANTIC_DOMAIN",
            "RELIABILITY_SECURITY",
        }:
            raise ValueError("multi-Agent panel requires one input hash per frozen role")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.expected_input_bundle_sha256.values()):
            raise ValueError("multi-Agent panel input hashes must be SHA-256 values")
        if len(set(self.review_receipt_sha256)) != 3:
            raise ValueError("multi-Agent panel requires three distinct review receipts")
        if self.authority_signature.authority_role != "MULTI_AGENT_PANEL":
            raise ValueError("multi-Agent panel Gate requires its pinned authority")
        return self


class SealedAgentBlindGateReceipt(AgentGateComponentReceiptBase):
    schema_version: Literal["sealed-agent-blind-gate-receipt-v2"] = (
        "sealed-agent-blind-gate-receipt-v2"
    )
    component: Literal["SEALED_AGENT_BLIND"] = "SEALED_AGENT_BLIND"
    receipt_path: str = Field(min_length=1, max_length=500)
    score_input_manifest_path: str = Field(min_length=1, max_length=500)
    deterministic_score_receipt_path: str = Field(min_length=1, max_length=500)
    mint_receipt_path: str = Field(min_length=1, max_length=500)
    thresholds_repository_path: str = Field(min_length=1, max_length=300)
    scorer_repository_path: str = Field(min_length=1, max_length=300)
    custody_registry_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mint_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score_input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    one_shot_nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tranche_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_state: Literal["COMPLETED"] = "COMPLETED"
    evidence_level: Literal["SEALED_AGENT_BLIND"] = "SEALED_AGENT_BLIND"

    @model_validator(mode="after")
    def uses_sealed_authority(self) -> "SealedAgentBlindGateReceipt":
        if self.authority_signature.authority_role != "SEALED_AGENT_BLIND":
            raise ValueError("sealed blind Gate requires its pinned authority")
        return self


StrictComponentReceipt = (
    AutomatedProductGateReceipt
    | LiveProviderGateReceipt
    | MultiAgentPanelGateReceipt
    | SealedAgentBlindGateReceipt
)


class AutomatedCheckContract(StrictModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,79}$")
    argv: list[str] = Field(min_length=3, max_length=40)
    workdir: str = Field(min_length=1, max_length=200)
    timeout_seconds: int = Field(ge=1, le=3600)

    @model_validator(mode="after")
    def command_is_allowlisted(self) -> "AutomatedCheckContract":
        python_check = (
            self.argv[0] in {"python", "python3"}
            and self.argv[1] == "-m"
            and self.argv[2] in {"pytest", "ruff"}
        )
        npm_check = (
            self.argv[0] in {"npm", "npm.cmd"}
            and self.argv[1] == "run"
            and len(self.argv) == 3
            and re.fullmatch(r"[a-z0-9][a-z0-9:_-]{1,79}", self.argv[2])
        )
        if not python_check and not npm_check:
            raise ValueError("automated checks must use pytest, ruff, or an npm script")
        if any("\x00" in value or "\n" in value or "\r" in value for value in self.argv):
            raise ValueError("automated check arguments contain control characters")
        workdir = self.workdir.replace("\\", "/")
        if workdir.startswith("/") or ".." in Path(workdir).parts:
            raise ValueError("automated check workdir must be repository-relative")
        return self


class AutomatedIsolationContract(StrictModel):
    mode: Literal["OCI_EPHEMERAL_NO_HOST_MOUNTS"] = (
        "OCI_EPHEMERAL_NO_HOST_MOUNTS"
    )
    runner_recipe_path: Literal[
        "backend/eval_data/agent_gate_v1/automation_runner.Dockerfile"
    ] = "backend/eval_data/agent_gate_v1/automation_runner.Dockerfile"
    runner_recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_entrypoint_path: Literal[
        "backend/eval_data/agent_gate_v1/automation_runner_entrypoint.sh"
    ] = "backend/eval_data/agent_gate_v1/automation_runner_entrypoint.sh"
    runner_entrypoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_context_policy_path: Literal[
        "backend/eval_data/agent_gate_v1/automation_runner.Dockerfile.dockerignore"
    ] = "backend/eval_data/agent_gate_v1/automation_runner.Dockerfile.dockerignore"
    runner_context_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    network_access: Literal[False] = False
    host_mount_count: Literal[0] = 0
    host_pid_namespace: Literal[False] = False
    synthetic_profile: Literal[True] = True
    authority_secret_mount_count: Literal[0] = 0


class CoreAutomationIsolationContract(StrictModel):
    mode: Literal["FRESH_CLEAN_CHECKOUT"] = "FRESH_CLEAN_CHECKOUT"
    network_access: Literal[False] = False
    synthetic_profile: Literal[False] = False
    authority_secret_mount_count: Literal[0] = 0


class AutomatedProductGateContract(StrictModel):
    schema_version: Literal[
        "automated-product-gate-contract-v1",
        "automated-product-gate-contract-v2",
    ] = "automated-product-gate-contract-v2"
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    gate_profile: GateProfile
    isolation: CoreAutomationIsolationContract | AutomatedIsolationContract
    checks: list[AutomatedCheckContract] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def check_ids_are_unique(self) -> "AutomatedProductGateContract":
        ids = [item.check_id for item in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("automated check IDs must be unique")
        sequence_match = re.search(r"-G(?P<sequence>0[1-7])-", self.goal_id)
        if sequence_match is None:
            raise ValueError("automated Gate Goal ID has no Program sequence")
        sequence = int(sequence_match.group("sequence"))
        expected_profile = (
            "HARDENED_CANDIDATE_GATE" if sequence == 7 else "CORE_AGENT_GATE"
        )
        if self.gate_profile != expected_profile:
            raise ValueError("automated Gate contract uses the wrong profile")
        if self.schema_version == "automated-product-gate-contract-v2":
            if self.gate_profile == "CORE_AGENT_GATE" and not isinstance(
                self.isolation, CoreAutomationIsolationContract
            ):
                raise ValueError("CORE automated Gate must use a fresh clean checkout")
        return self


class AutomatedCheckExecution(StrictModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,79}$")
    argv_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workdir: str = Field(min_length=1, max_length=200)
    exit_code: Literal[0] = 0
    stdout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    completed_at: datetime
    verdict: Literal["PASS"] = "PASS"

    @model_validator(mode="after")
    def execution_time_is_valid(self) -> "AutomatedCheckExecution":
        if self.completed_at < self.started_at:
            raise ValueError("automated check completed before it started")
        return self


class AutomatedProductExecutionManifest(StrictModel):
    schema_version: Literal["automated-product-execution-manifest-v1"] = (
        "automated-product-execution-manifest-v1"
    )
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    gate_profile: GateProfile
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    isolation_mode: Literal[
        "FRESH_CLEAN_CHECKOUT", "OCI_EPHEMERAL_NO_HOST_MOUNTS"
    ]
    runner_recipe_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    runner_entrypoint_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    runner_context_policy_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    runner_image_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    runner_image_archive_format: Literal["DOCKER_IMAGE_ARCHIVE_V1"] | None = None
    runner_image_archive_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )
    runner_image_archive_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    runner_image_archive_size: int | None = Field(default=None, ge=1)
    network_access: Literal[False] = False
    host_mount_count: Literal[0] = 0
    host_pid_namespace: Literal[False] = False
    synthetic_profile: bool
    authority_secret_mount_count: Literal[0] = 0
    checks: list[AutomatedCheckExecution] = Field(max_length=30)
    checks_not_run: list[str] = Field(max_length=30)
    failure_stage: Literal[
        "OCI_RUNNER_UNAVAILABLE",
        "OCI_BUILD_FAILED",
        "OCI_ARCHIVE_FAILED",
        "OCI_EXECUTION_FAILED",
    ] | None = None
    verdict: Literal["PASS", "NOT_RUN"]

    @model_validator(mode="after")
    def executed_check_ids_are_unique(self) -> "AutomatedProductExecutionManifest":
        ids = [item.check_id for item in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("automated execution check IDs must be unique")
        if self.gate_profile == "CORE_AGENT_GATE" and self.isolation_mode != (
            "FRESH_CLEAN_CHECKOUT"
        ):
            raise ValueError("CORE automation must use a fresh clean checkout")
        if self.isolation_mode == "FRESH_CLEAN_CHECKOUT":
            if self.synthetic_profile:
                raise ValueError("fresh checkout automation cannot claim OCI synthesis")
            if any(
                value is not None
                for value in (
                    self.runner_recipe_sha256,
                    self.runner_entrypoint_sha256,
                    self.runner_context_policy_sha256,
                    self.runner_image_id,
                    self.runner_image_archive_format,
                    self.runner_image_archive_path,
                    self.runner_image_archive_sha256,
                    self.runner_image_archive_size,
                    self.failure_stage,
                )
            ):
                raise ValueError("fresh checkout automation cannot claim OCI evidence")
            if self.verdict != "PASS" or not self.checks or self.checks_not_run:
                raise ValueError("fresh checkout automation requires all checks to pass")
            return self
        if self.isolation_mode != "OCI_EPHEMERAL_NO_HOST_MOUNTS":
            raise ValueError("HARDENED automation must use the isolated OCI runner")
        if not self.synthetic_profile:
            raise ValueError("HARDENED automation requires the synthetic OCI profile")
        if any(
            value is None
            for value in (
                self.runner_recipe_sha256,
                self.runner_entrypoint_sha256,
                self.runner_context_policy_sha256,
            )
        ):
            raise ValueError("HARDENED automation requires the OCI runner bindings")
        if self.verdict == "PASS":
            if (
                not self.checks
                or self.checks_not_run
                or self.failure_stage is not None
                or self.runner_image_id is None
                or self.runner_image_archive_format is None
                or self.runner_image_archive_path is None
                or self.runner_image_archive_sha256 is None
                or self.runner_image_archive_size is None
            ):
                raise ValueError("PASS automation manifests require a complete OCI run")
        elif (
            self.checks
            or not self.checks_not_run
            or self.failure_stage is None
            or self.runner_image_id is not None
            or self.runner_image_archive_format is not None
            or self.runner_image_archive_path is not None
            or self.runner_image_archive_sha256 is not None
            or self.runner_image_archive_size is not None
        ):
            raise ValueError("NOT_RUN automation manifests must describe an unstarted OCI run")
        return self


class AutomatedProductVerificationReceipt(StrictModel):
    schema_version: Literal["automated-product-verification-receipt-v2"] = (
        "automated-product-verification-receipt-v2"
    )
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_contract_path: str = Field(min_length=1, max_length=300)
    gate_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    isolation_mode: Literal["OCI_EPHEMERAL_NO_HOST_MOUNTS"]
    runner_recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_entrypoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_context_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runner_image_archive_format: Literal["DOCKER_IMAGE_ARCHIVE_V1"]
    runner_image_archive_path: str = Field(min_length=1, max_length=500)
    runner_image_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_image_archive_size: int = Field(ge=1)
    network_access: Literal[False] = False
    host_mount_count: Literal[0] = 0
    host_pid_namespace: Literal[False] = False
    execution_manifest_path: str = Field(min_length=1, max_length=500)
    execution_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executed_check_count: int = Field(ge=1)
    failed_check_count: Literal[0] = 0
    checks_not_run: list[str] = Field(max_length=0)
    verdict: Literal["PASS"] = "PASS"


class LiveProviderVerificationReceipt(StrictModel):
    schema_version: Literal["live-provider-verification-receipt-v2"] = (
        "live-provider-verification-receipt-v2"
    )
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    authority_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    amap_provider_receipt_index_path: str = Field(min_length=1, max_length=500)
    amap_provider_receipt_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    amap_runtime_receipt_path: str = Field(min_length=1, max_length=500)
    amap_runtime_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qwen_runtime_receipt_path: str = Field(min_length=1, max_length=500)
    qwen_runtime_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split: Literal["dev", "validation", "frozen_blind"]
    amap_provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    amap_database_export_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    amap_http_receipt_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    amap_live_effect_count: int = Field(ge=1)
    qwen_live_effect_count: int = Field(ge=1)
    amap_execution_mode: Literal["LIVE"] = "LIVE"
    qwen_execution_mode: Literal["LIVE"] = "LIVE"
    fixture_effect_count: Literal[0] = 0
    verdict: Literal["PASS"] = "PASS"


class MultiAgentPanelVerificationReceipt(StrictModel):
    schema_version: Literal["multi-agent-panel-verification-receipt-v2"] = (
        "multi-agent-panel-verification-receipt-v2"
    )
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_paths: list[str] = Field(min_length=3, max_length=3)
    adjudication_path: str = Field(min_length=1, max_length=500)
    expected_input_bundle_sha256: dict[ReviewerRole, str]
    review_count: Literal[3] = 3
    review_sha256: list[str] = Field(min_length=3, max_length=3)
    adjudication_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_p0_count: Literal[0] = 0
    accepted_p1_count: Literal[0] = 0
    accepted_in_scope_p2_count: Literal[0] = 0
    required_scenario_union_complete: Literal[True] = True
    verdict: Literal["PASS"] = "PASS"

    @model_validator(mode="after")
    def review_receipts_are_distinct(self) -> "MultiAgentPanelVerificationReceipt":
        if len(set(self.review_paths)) != 3:
            raise ValueError("panel verification requires three distinct review paths")
        if set(self.expected_input_bundle_sha256) != {
            "PRODUCT_UX",
            "SEMANTIC_DOMAIN",
            "RELIABILITY_SECURITY",
        }:
            raise ValueError("panel verification input hash set is incomplete")
        if len(set(self.review_sha256)) != 3:
            raise ValueError("panel verification requires three distinct reviews")
        return self


class SealedBlindVerificationReceipt(StrictModel):
    schema_version: Literal["sealed-agent-blind-verification-receipt-v2"] = (
        "sealed-agent-blind-verification-receipt-v2"
    )
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    authority_anchor_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    authority_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_path: str = Field(min_length=1, max_length=500)
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    thresholds_repository_path: str = Field(min_length=1, max_length=300)
    score_input_manifest_path: str = Field(min_length=1, max_length=500)
    score_input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deterministic_score_receipt_path: str = Field(min_length=1, max_length=500)
    score_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scorer_repository_path: str = Field(min_length=1, max_length=300)
    mint_receipt_path: str = Field(min_length=1, max_length=500)
    mint_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    custody_registry_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_state: Literal["COMPLETED"] = "COMPLETED"
    tranche_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    one_shot_nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_level: Literal["SEALED_AGENT_BLIND"] = "SEALED_AGENT_BLIND"
    human_evidence: Literal[False] = False
    verdict: Literal["PASS"] = "PASS"


class AgentGatePassReceipt(StrictModel):
    schema_version: Literal["agent-gate-pass-receipt-v2"] = "agent-gate-pass-receipt-v2"
    gate_profile: GateProfile
    goal_sequence: int = Field(ge=1, le=7)
    goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    predecessor_goal_id: str = Field(pattern=r"^TC-[A-Z0-9-]+$")
    predecessor_completion_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    current_goal_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_goal_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    automated_gate_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    authority_anchor_commit: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{40}$"
    )
    authority_policy_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    authority_generation: int | None = Field(default=None, ge=1, le=7)
    authority_anchor_receipt_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    canonical_origin_url: Literal["https://github.com/Munto47/BreezeTravel.git"] = (
        "https://github.com/Munto47/BreezeTravel.git"
    )
    candidate_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_binding_sha256: dict[str, str] = Field(default_factory=dict)
    component_receipt_sha256: dict[AgentGateComponent, str]
    fresh_checkout_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    remote_name: str = Field(min_length=1, max_length=80)
    remote_ref: str = Field(pattern=r"^refs/heads/[A-Za-z0-9._/-]+$")
    remote_subject: str = Field(pattern=r"^[0-9a-f]{40}$")
    remote_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    verifier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_levels: list[EvidenceLevel] = Field(min_length=4)
    human_usability_status: Literal["NOT_RUN"] = "NOT_RUN"
    production_status: Literal["NOT_RUN"] = "NOT_RUN"
    verdict: Literal["AGENT_GATE_PASS"] = "AGENT_GATE_PASS"
    completed_at: datetime
    authority_signature: DetachedAuthoritySignature | None = None

    @model_validator(mode="after")
    def all_components_and_levels_are_present(self) -> "AgentGatePassReceipt":
        if not self.goal_id.startswith(f"TC-VNEXT-G{self.goal_sequence:02d}-"):
            raise ValueError("AGENT_GATE_PASS Goal sequence and ID disagree")
        required_components = {
            "AUTOMATED_PRODUCT_GATE",
            "LIVE_PROVIDER_GATE",
            "MULTI_AGENT_PANEL",
            "SEALED_AGENT_BLIND",
        }
        if set(self.component_receipt_sha256) != required_components:
            raise ValueError("AGENT_GATE_PASS requires all four component receipts")
        required_levels = {
            "AUTOMATED_TEST",
            "LIVE_PROVIDER_EVIDENCE",
            "MULTI_AGENT_SIMULATED_REVIEW",
            "SEALED_AGENT_BLIND",
        }
        if not required_levels.issubset(self.evidence_levels):
            raise ValueError("AGENT_GATE_PASS evidence levels are incomplete")
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_.-]{2,79}", key)
            or not re.fullmatch(r"[0-9a-f]{64}", value)
            for key, value in self.frozen_binding_sha256.items()
        ):
            raise ValueError("AGENT_GATE_PASS frozen bindings are invalid")
        authority_values = (
            self.authority_anchor_commit,
            self.authority_policy_sha256,
            self.authority_generation,
            self.authority_anchor_receipt_sha256,
        )
        if self.gate_profile == "CORE_AGENT_GATE":
            if self.goal_sequence == 7:
                raise ValueError("CORE_AGENT_GATE is restricted to G01-G06")
            if any(value is not None for value in authority_values):
                raise ValueError("CORE_AGENT_GATE cannot claim HARDENED authority")
            if self.authority_signature is not None:
                raise ValueError("CORE_AGENT_GATE cannot carry an authority signature")
            required_bindings = {
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
            if set(self.frozen_binding_sha256) != required_bindings:
                raise ValueError("CORE_AGENT_GATE frozen binding set is incomplete")
        elif (
            self.authority_generation != self.goal_sequence
            or any(value is None for value in authority_values)
            or self.authority_signature is None
            or self.authority_signature.authority_role != "FINAL_GATE"
        ):
            raise ValueError("HARDENED_CANDIDATE_GATE requires G07 authority")
        return self
