"""Versioned execution contracts for the P5 A/B/C comparison.

The product-facing adapter input intentionally excludes the deterministic oracle.
Scoring is a separate phase and can only join on ``case_id`` after every variant
output has been sealed.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evals.trip_check_v1.p5.data_contract import digest


VARIANT_IDS = ("legacy_a", "core_b", "solver_c")


class TerminalStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    NEEDS_USER_RESOLUTION = "NEEDS_USER_RESOLUTION"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


class P5VariantRunSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-variant-run-spec-v1"] = (
        "trip-check-p5-variant-run-spec-v1"
    )
    subject_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    dirty_tree: bool
    lane: Literal["nonblind", "frozen_blind"]
    dataset_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_spec_template_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_snapshot_id: str
    execution_mode: Literal["controlled_snapshot"] = "controlled_snapshot"
    random_seed: int
    budget: dict[str, int | float]
    replay_hash_policy: Literal["p5-semantic-projection-v1"] = (
        "p5-semantic-projection-v1"
    )
    variant_id: Literal["legacy_a", "core_b", "solver_c"]
    adapter_version: str
    repair_strategy: str

    @property
    def run_spec_hash(self) -> str:
        return digest(self.model_dump(mode="json"))


class P5AdapterInput(BaseModel):
    """Only fields an evaluated system is allowed to observe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-adapter-input-v1"] = (
        "trip-check-p5-adapter-input-v1"
    )
    case_id: str
    split: Literal["pilot", "dev", "regression", "frozen_blind"]
    city: Literal["北京", "上海", "杭州"]
    trip_days: int = Field(ge=2, le=5)
    group_size: int = Field(ge=2, le=5)
    input_kind: Literal["TEXT", "SYNTHETIC_SCREENSHOT"]
    product_input: dict[str, Any]
    normalized_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_control: dict[str, Any]

    @classmethod
    def from_case(cls, case: dict[str, Any]) -> "P5AdapterInput":
        forbidden = {"oracle", "oracle_sha256", "expected", "human_label", "ground_truth"}
        payload = {
            key: value
            for key, value in case.items()
            if key
            in {
                "case_id",
                "split",
                "city",
                "trip_days",
                "group_size",
                "input_kind",
                "product_input",
                "normalized_input_sha256",
                "runner_control",
            }
        }
        if forbidden & payload.keys():  # defensive if the allowlist changes later
            raise ValueError("oracle-bearing field reached adapter input")
        return cls.model_validate(payload)


class P5AdapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terminal_status: TerminalStatus
    capability_outcomes: dict[str, str] = Field(default_factory=dict)
    native_output: dict[str, Any] = Field(default_factory=dict)
    evaluation_projection: dict[str, Any] = Field(default_factory=dict)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    advice: list[dict[str, Any]] = Field(default_factory=list)
    postcheck: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    receipts: list[dict[str, Any]] = Field(default_factory=list)
    raw_artifact: dict[str, Any] = Field(default_factory=dict)
    token_count: int | Literal["NOT_MEASURED"] = 0
    cost_usd: float | Literal["NOT_MEASURED"] = 0.0
    error_category: str | None = None


class P5TerminalOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-terminal-output-v1"] = (
        "trip-check-p5-terminal-output-v1"
    )
    case_id: str
    split: str
    city: str
    input_kind: str
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_snapshot_id: str
    fault_profile_id: str
    case_seed: int
    run_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant_id: Literal["legacy_a", "core_b", "solver_c"]
    adapter_version: str
    repair_strategy: str
    terminal_status: TerminalStatus
    capability_outcomes: dict[str, str]
    native_output: dict[str, Any]
    evaluation_projection: dict[str, Any]
    findings: list[dict[str, Any]]
    advice: list[dict[str, Any]]
    postcheck: dict[str, Any] | None
    trace: list[dict[str, Any]]
    receipts: list[dict[str, Any]]
    latency_ms: float = Field(ge=0)
    token_count: int | Literal["NOT_MEASURED"]
    cost_usd: float | Literal["NOT_MEASURED"]
    error_category: str | None
    raw_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def semantic_projection(
    *,
    adapter_input: P5AdapterInput,
    run_spec: P5VariantRunSpec,
    result: P5AdapterResult,
) -> dict[str, Any]:
    """Return the versioned, volatility-free replay projection."""

    return {
        "policy": run_spec.replay_hash_policy,
        "case_id": adapter_input.case_id,
        "input_hash": adapter_input.normalized_input_sha256,
        "run_spec_hash": run_spec.run_spec_hash,
        "variant_id": run_spec.variant_id,
        "adapter_version": run_spec.adapter_version,
        "repair_strategy": run_spec.repair_strategy,
        "terminal_status": result.terminal_status.value,
        "capability_outcomes": result.capability_outcomes,
        "native_output": result.native_output,
        "evaluation_projection": result.evaluation_projection,
        "findings": result.findings,
        "advice": result.advice,
        "postcheck": result.postcheck,
        "token_count": result.token_count,
        "cost_usd": result.cost_usd,
        "error_category": result.error_category,
    }
