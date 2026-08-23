"""Strict eval-only interfaces for the P5 v3 semantic-closure contract.

P5 v3 changes the case/materialization and execution envelopes while keeping
the frozen oracle truth shape at v2.  In particular, screenshot cases bind the
immutable v2 render/OCR/cleanup receipts and their sealed source provenance;
they do not relabel those historical receipts as fresh v3 OCR evidence.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class GateStatusV3(str, Enum):
    NOT_RUN = "NOT_RUN"
    RUNNING = "RUNNING"
    PASS = "PASS"
    REJECT = "REJECT"
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"


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


class P5CaseResultV3(BaseModel):
    """Hash-bound case result with explicit revision/postcheck lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-case-result-v3"] = (
        "trip-check-p5-case-result-v3"
    )
    case_result_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    terminal_output: P5TerminalOutputV3
    revision_lineage: dict[str, Any]
    case_result_hash: Sha256V3

    @model_validator(mode="after")
    def hash_binds_complete_result(self) -> "P5CaseResultV3":
        payload = self.model_dump(mode="json", exclude={"case_result_hash"})
        if self.case_result_hash != digest(payload):
            raise ValueError("case_result_hash does not bind the complete case result")
        return self


class P5FailureRecordV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-failure-record-v3"] = (
        "trip-check-p5-failure-record-v3"
    )
    run_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    lane: Literal["nonblind", "frozen_blind"]
    variant_id: Literal["legacy_a", "core_b", "solver_c"]
    failure_status: Literal["REJECT", "BLOCKED_EXTERNAL", "INVALID_EVIDENCE"]
    failure_category: str = Field(min_length=1)
    terminal_status: TerminalStatusV3 | None = None
    first_attempt_receipt_hash: Sha256V3 | None = None
    reproduction_command: str = Field(min_length=1)
    retry_allowed: bool
    retry_count: NonNegativeIntV3
    failure_record_hash: Sha256V3

    @model_validator(mode="after")
    def retry_and_hash_are_consistent(self) -> "P5FailureRecordV3":
        if not self.retry_allowed and self.retry_count:
            raise ValueError("retry_count must be zero when retry is not allowed")
        payload = self.model_dump(mode="json", exclude={"failure_record_hash"}, exclude_none=True)
        if self.failure_record_hash != digest(payload):
            raise ValueError("failure_record_hash does not bind the complete failure record")
        return self


class P5ArtifactIndexEntryV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    byte_size: NonNegativeIntV3
    sha256: Sha256V3
    generated_by: str = Field(min_length=1)
    generated_at: datetime

    @field_validator("path")
    @classmethod
    def artifact_path_is_repository_relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError("artifact paths must be normalized repository-relative POSIX paths")
        return value

    @field_validator("generated_at")
    @classmethod
    def generated_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        return value


class P5ArtifactIndexV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-artifact-index-v3"] = (
        "trip-check-p5-artifact-index-v3"
    )
    subject_commit: CommitShaV3
    dirty_tree: bool
    entries: list[P5ArtifactIndexEntryV3] = Field(min_length=1)
    artifact_index_hash: Sha256V3

    @model_validator(mode="after")
    def paths_are_unique_and_hash_is_bound(self) -> "P5ArtifactIndexV3":
        paths = [entry.path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact index paths must be unique")
        payload = self.model_dump(mode="json", exclude={"artifact_index_hash"})
        if self.artifact_index_hash != digest(payload):
            raise ValueError("artifact_index_hash does not bind the complete index")
        return self


class P5GateCheckV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_id: str = Field(min_length=1)
    status: GateStatusV3
    hard_thresholds: dict[str, Any]
    evidence_boundary: dict[str, GateStatusV3]
    artifact_hashes: list[Sha256V3]
    notes: list[str] = Field(default_factory=list)


class P5GateManifestV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-gate-manifest-v3"] = (
        "trip-check-p5-gate-manifest-v3"
    )
    subject_commit: CommitShaV3
    dirty_tree: bool
    status: GateStatusV3
    gates: list[P5GateCheckV3] = Field(min_length=1)
    artifact_index_hash: Sha256V3
    dataset_manifest_hash: Sha256V3
    human_calibration_performed: bool
    human_evidence: GateStatusV3
    production_release: GateStatusV3
    main_merge: GateStatusV3
    gate_manifest_hash: Sha256V3

    @model_validator(mode="after")
    def pass_is_fail_closed_and_hash_bound(self) -> "P5GateManifestV3":
        if self.status is GateStatusV3.PASS and (
            self.dirty_tree
            or any(gate.status is not GateStatusV3.PASS for gate in self.gates)
            or self.human_calibration_performed
            or self.human_evidence is not GateStatusV3.NOT_RUN
            or self.production_release is not GateStatusV3.NOT_RUN
            or self.main_merge is not GateStatusV3.NOT_RUN
        ):
            raise ValueError("PASS gate manifest contradicts its evidence boundary")
        payload = self.model_dump(mode="json", exclude={"gate_manifest_hash"})
        if self.gate_manifest_hash != digest(payload):
            raise ValueError("gate_manifest_hash does not bind the complete manifest")
        return self


__all__ = [
    "GateStatusV3",
    "P5ArtifactBindingV3",
    "P5ArtifactIndexEntryV3",
    "P5ArtifactIndexV3",
    "P5CaseV3",
    "P5CaseResultV3",
    "P5FailureRecordV3",
    "P5GateCheckV3",
    "P5GateManifestV3",
    "P5MaterializationBindingV3",
    "P5OcrSourceBindingV3",
    "P5RunBudgetV3",
    "P5TerminalOutputV3",
    "P5VariantRunSpecV3",
    "TerminalStatusV3",
    "VARIANT_IDS_V3",
]
