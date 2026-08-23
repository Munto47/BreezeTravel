"""Strict eval-only interfaces for the P5 v3 semantic-closure contract.

P5 v3 changes the case/materialization and execution envelopes while keeping
the frozen oracle truth shape at v2.  In particular, screenshot cases bind the
immutable v2 render/OCR/cleanup receipts and their sealed source provenance;
they do not relabel those historical receipts as fresh v3 OCR evidence.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.trip_check_v1.p5.contracts_v2 import P5OracleV2
from evals.trip_check_v1.p5.data_contract import digest


VARIANT_IDS_V3 = ("legacy_a", "core_b", "solver_c")

Sha256V3 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CommitShaV3 = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
NonNegativeIntV3 = Annotated[int, Field(ge=0)]
NonNegativeFloatV3 = Annotated[float, Field(ge=0)]


class TerminalStatusV3(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    NEEDS_USER_RESOLUTION = "NEEDS_USER_RESOLUTION"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


class P5ArtifactBindingV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    content_sha256: Sha256V3


class P5OcrSourceBindingV3(BaseModel):
    """Commitment to the sealed v2 source of reused screenshot receipts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-v3-ocr-source-binding-v1"] = (
        "trip-check-p5-v3-ocr-source-binding-v1"
    )
    source_dataset_id: Literal["trip-check-p5-360-v2"] = "trip-check-p5-360-v2"
    source_manifest_hash: Sha256V3
    source_manifest_file_sha256: Sha256V3
    source_blind_seal_file_sha256: Sha256V3
    source_active_contract_sha256: Sha256V3
    source_active_contract_file_sha256: Sha256V3
    source_candidate_freeze_commit: CommitShaV3
    source_path: Literal[
        "evals/trip_check_v1/p5/materializations_nonblind_v2.jsonl",
        "evals/trip_check_v1/p5/frozen_blind.v2.materializations.jsonl",
    ]
    source_file_sha256: Sha256V3
    source_materialization_hash: Sha256V3
    historical_render_receipt_sha256: Sha256V3
    historical_ocr_receipt_sha256: Sha256V3
    historical_cleanup_receipt_sha256: Sha256V3


class P5MaterializationBindingV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-materialization-binding-v3"] = (
        "trip-check-p5-materialization-binding-v3"
    )
    materialization_id: str = Field(min_length=1)
    materialization_sha256: Sha256V3
    source_payload: P5ArtifactBindingV3
    render_receipt: P5ArtifactBindingV3 | None = None
    ocr_baseline_receipt: P5ArtifactBindingV3 | None = None
    cleanup_receipt: P5ArtifactBindingV3 | None = None
    ocr_source_binding: P5OcrSourceBindingV3 | None = None
    provider_snapshot: P5ArtifactBindingV3
    evidence_snapshot: P5ArtifactBindingV3
    candidate_sets: list[P5ArtifactBindingV3] = Field(default_factory=list)
    fault_script: P5ArtifactBindingV3

    @model_validator(mode="after")
    def screenshot_receipts_are_complete_and_source_bound(self) -> "P5MaterializationBindingV3":
        receipt_values = (
            self.render_receipt,
            self.ocr_baseline_receipt,
            self.cleanup_receipt,
            self.ocr_source_binding,
        )
        if all(value is None for value in receipt_values):
            return self
        if any(value is None for value in receipt_values):
            raise ValueError("screenshot receipt bindings must be all present or all absent")

        assert self.render_receipt is not None
        assert self.ocr_baseline_receipt is not None
        assert self.cleanup_receipt is not None
        assert self.ocr_source_binding is not None
        expected_schemas = (
            (self.render_receipt.schema_version, "trip-check-p5-render-receipt-v2"),
            (self.ocr_baseline_receipt.schema_version, "trip-check-p5-ocr-baseline-receipt-v2"),
            (self.cleanup_receipt.schema_version, "trip-check-p5-cleanup-receipt-v2"),
        )
        if any(actual != expected for actual, expected in expected_schemas):
            raise ValueError("P5 v3 screenshots must retain the historical v2 receipt schemas")
        source = self.ocr_source_binding
        if (
            source.historical_render_receipt_sha256 != self.render_receipt.content_sha256
            or source.historical_ocr_receipt_sha256 != self.ocr_baseline_receipt.content_sha256
            or source.historical_cleanup_receipt_sha256 != self.cleanup_receipt.content_sha256
        ):
            raise ValueError("screenshot receipt hashes do not match their sealed v2 source binding")
        return self


class P5CaseV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-eval-case-v3"] = "trip-check-p5-eval-case-v3"
    case_id: str = Field(min_length=1)
    split: Literal["pilot", "dev", "regression", "frozen_blind"]
    city: Literal["北京", "上海", "杭州"]
    trip_days: int = Field(ge=2, le=5)
    group_size: int = Field(ge=2, le=5)
    input_kind: Literal["TEXT", "SYNTHETIC_SCREENSHOT"]
    difficulty: Literal["CLEAN", "MEDIUM", "HARD"]
    coverage_tags: list[str] = Field(min_length=3)
    product_input: dict[str, Any]
    normalized_input_sha256: Sha256V3
    materialization: P5MaterializationBindingV3
    runner_control: dict[str, Any]
    lineage: dict[str, Any]
    source_ref: dict[str, Any]
    provenance: dict[str, Any]
    oracle: P5OracleV2 | None = None
    oracle_sha256: Sha256V3 | None = None
    case_hash: Sha256V3

    @model_validator(mode="after")
    def split_truth_screenshot_and_hash_are_bound(self) -> "P5CaseV3":
        if self.split == "frozen_blind":
            if self.oracle is not None or self.oracle_sha256 is not None:
                raise ValueError("frozen blind cases cannot contain oracle fields")
        else:
            if self.oracle is None or self.oracle_sha256 is None:
                raise ValueError("non-blind cases require a hash-bound v2 oracle")
            if self.oracle_sha256 != digest(self.oracle.model_dump(mode="json")):
                raise ValueError("oracle_sha256 does not bind the v2 oracle")

        screenshot_bound = self.materialization.ocr_source_binding is not None
        if self.input_kind == "SYNTHETIC_SCREENSHOT" and not screenshot_bound:
            raise ValueError("screenshot cases require sealed v2 screenshot receipt provenance")
        if self.input_kind == "TEXT" and screenshot_bound:
            raise ValueError("text cases cannot bind screenshot receipt provenance")
        if screenshot_bound:
            assert self.materialization.ocr_source_binding is not None
            expected_source_path = (
                "evals/trip_check_v1/p5/frozen_blind.v2.materializations.jsonl"
                if self.split == "frozen_blind"
                else "evals/trip_check_v1/p5/materializations_nonblind_v2.jsonl"
            )
            if self.materialization.ocr_source_binding.source_path != expected_source_path:
                raise ValueError("screenshot source path does not match the case lane")

        case_payload = self.model_dump(
            mode="json",
            exclude={"case_hash"},
            exclude_none=True,
        )
        if self.case_hash != digest(case_payload):
            raise ValueError("case_hash does not bind the complete P5 v3 case")
        return self


class P5RunBudgetV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_cost_usd: NonNegativeFloatV3
    max_provider_queries: NonNegativeIntV3
    max_retries: NonNegativeIntV3
    max_tokens: NonNegativeIntV3
    timeout_seconds: float = Field(gt=0)


class P5VariantRunSpecV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-variant-run-spec-v3"] = (
        "trip-check-p5-variant-run-spec-v3"
    )
    subject_commit: CommitShaV3
    dirty_tree: bool
    lane: Literal["nonblind", "frozen_blind"]
    dataset_manifest_hash: Sha256V3
    case_set_hash: Sha256V3
    materialization_set_hash: Sha256V3
    run_spec_template_hash: Sha256V3
    rubric_hash: Sha256V3
    renderer_version: str = Field(min_length=1)
    ocr_engine_version: str = Field(min_length=1)
    evidence_policy_version: str = Field(min_length=1)
    fault_registry_version: str = Field(min_length=1)
    random_seed: int = Field(ge=0)
    budget: P5RunBudgetV3
    replay_hash_policy: Literal["p5-semantic-projection-v3"] = "p5-semantic-projection-v3"
    variant_id: Literal["legacy_a", "core_b", "solver_c"]
    adapter_version: str = Field(min_length=1)
    repair_strategy: str = Field(min_length=1)

    @property
    def run_spec_hash(self) -> str:
        return digest(self.model_dump(mode="json"))


class P5TerminalOutputV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-terminal-output-v3"] = (
        "trip-check-p5-terminal-output-v3"
    )
    case_id: str = Field(min_length=1)
    split: Literal["pilot", "dev", "regression", "frozen_blind"]
    city: Literal["北京", "上海", "杭州"]
    input_kind: Literal["TEXT", "SYNTHETIC_SCREENSHOT"]
    input_hash: Sha256V3
    materialization_hash: Sha256V3
    render_receipt_hash: Sha256V3 | None = None
    ocr_receipt_hash: Sha256V3 | None = None
    provider_snapshot_hash: Sha256V3
    evidence_snapshot_hash: Sha256V3
    candidate_set_hashes: list[Sha256V3]
    fault_script_hash: Sha256V3
    run_spec_hash: Sha256V3
    variant_id: Literal["legacy_a", "core_b", "solver_c"]
    adapter_version: str = Field(min_length=1)
    repair_strategy: str = Field(min_length=1)
    terminal_status: TerminalStatusV3
    capability_outcomes: dict[str, str]
    native_output: dict[str, Any]
    evaluation_projection: dict[str, Any]
    findings: list[dict[str, Any]]
    advice: list[dict[str, Any]]
    postcheck: dict[str, Any] | None
    receipts: list[dict[str, Any]]
    latency_ms: NonNegativeFloatV3
    token_count: NonNegativeIntV3 | Literal["NOT_MEASURED"]
    cost_usd: NonNegativeFloatV3 | Literal["NOT_MEASURED"]
    error_category: str | None
    raw_artifact_hash: Sha256V3
    semantic_output_hash: Sha256V3
    replay_hash: Sha256V3


__all__ = [
    "P5ArtifactBindingV3",
    "P5CaseV3",
    "P5MaterializationBindingV3",
    "P5OcrSourceBindingV3",
    "P5RunBudgetV3",
    "P5TerminalOutputV3",
    "P5VariantRunSpecV3",
    "TerminalStatusV3",
    "VARIANT_IDS_V3",
]
