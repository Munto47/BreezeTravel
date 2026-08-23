"""Frozen eval-only interfaces shared by P5 v2 workstreams."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.trip_check_v1.p5.data_contract import digest


VARIANT_IDS_V2 = ("legacy_a", "core_b", "solver_c")


class TerminalStatusV2(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    NEEDS_USER_RESOLUTION = "NEEDS_USER_RESOLUTION"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


class P5ArtifactBindingV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class P5MaterializationBindingV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-materialization-binding-v2"] = (
        "trip-check-p5-materialization-binding-v2"
    )
    materialization_id: str
    materialization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_payload: P5ArtifactBindingV2
    render_receipt: P5ArtifactBindingV2 | None = None
    ocr_baseline_receipt: P5ArtifactBindingV2 | None = None
    provider_snapshot: P5ArtifactBindingV2
    evidence_snapshot: P5ArtifactBindingV2
    candidate_sets: list[P5ArtifactBindingV2] = Field(default_factory=list)
    fault_script: P5ArtifactBindingV2


class P5OracleV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-oracle-v2"] = "trip-check-p5-oracle-v2"
    task_success_required: bool
    requires_user_resolution: bool
    required_reason_codes: list[str]
    wrong_city_or_poi_max: int = Field(ge=0)
    max_new_blocker_high_unknown: int = Field(ge=0)
    unknown_must_be_preserved: bool
    advice_required: bool
    specific_place_allowed: bool
    candidate_receipt_mode: Literal["REQUIRED", "FORBIDDEN", "NOT_APPLICABLE"]
    expected_strategy_outcome: Literal["FEASIBLE", "UNSAT", "TIMEOUT", "FALLBACK"]
    concurrency_expectation: Literal["NONE", "IDEMPOTENT_REPLAY", "SINGLE_WINNER"]
    ocr_required: bool


class P5CaseV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-eval-case-v2"] = "trip-check-p5-eval-case-v2"
    case_id: str
    split: Literal["pilot", "dev", "regression", "frozen_blind"]
    city: Literal["北京", "上海", "杭州"]
    trip_days: int = Field(ge=2, le=5)
    group_size: int = Field(ge=2, le=5)
    input_kind: Literal["TEXT", "SYNTHETIC_SCREENSHOT"]
    difficulty: Literal["CLEAN", "MEDIUM", "HARD"]
    coverage_tags: list[str]
    product_input: dict[str, Any]
    normalized_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    materialization: P5MaterializationBindingV2
    runner_control: dict[str, Any]
    lineage: dict[str, Any]
    source_ref: dict[str, Any]
    provenance: dict[str, Any]
    oracle: P5OracleV2 | None = None
    oracle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    case_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def oracle_is_split_isolated(self) -> "P5CaseV2":
        if self.split == "frozen_blind":
            if self.oracle is not None or self.oracle_sha256 is not None:
                raise ValueError("frozen blind cases cannot contain oracle fields")
            return self
        if self.oracle is None or self.oracle_sha256 is None:
            raise ValueError("non-blind cases require a hash-bound oracle")
        if self.oracle_sha256 != digest(self.oracle.model_dump(mode="json")):
            raise ValueError("oracle_sha256 does not bind oracle")
        return self


class P5VariantRunSpecV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-variant-run-spec-v2"] = (
        "trip-check-p5-variant-run-spec-v2"
    )
    subject_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    dirty_tree: bool
    lane: Literal["nonblind", "frozen_blind"]
    dataset_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    materialization_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_spec_template_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    renderer_version: str
    ocr_engine_version: str
    evidence_policy_version: str
    fault_registry_version: str
    random_seed: int
    budget: dict[str, int | float]
    replay_hash_policy: Literal["p5-semantic-projection-v2"] = "p5-semantic-projection-v2"
    variant_id: Literal["legacy_a", "core_b", "solver_c"]
    adapter_version: str
    repair_strategy: str

    @property
    def run_spec_hash(self) -> str:
        return digest(self.model_dump(mode="json"))


class P5TerminalOutputV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-terminal-output-v2"] = (
        "trip-check-p5-terminal-output-v2"
    )
    case_id: str
    split: str
    city: str
    input_kind: str
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    materialization_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_receipt_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ocr_receipt_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_set_hashes: list[str]
    fault_script_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant_id: Literal["legacy_a", "core_b", "solver_c"]
    adapter_version: str
    repair_strategy: str
    terminal_status: TerminalStatusV2
    capability_outcomes: dict[str, str]
    native_output: dict[str, Any]
    evaluation_projection: dict[str, Any]
    findings: list[dict[str, Any]]
    advice: list[dict[str, Any]]
    postcheck: dict[str, Any] | None
    receipts: list[dict[str, Any]]
    latency_ms: float = Field(ge=0)
    token_count: int | Literal["NOT_MEASURED"]
    cost_usd: float | Literal["NOT_MEASURED"]
    error_category: str | None
    raw_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
