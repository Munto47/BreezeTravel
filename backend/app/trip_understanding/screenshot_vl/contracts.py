from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


HASH_PATTERN = r"^[0-9a-f]{64}$"
ERROR_METRICS = (
    "key_field_error_rate",
    "reading_order_error_rate",
    "final_card_error_rate",
    "bbox_error_rate",
)
ALL_METRICS = (*ERROR_METRICS, "three_image_p95_ms")

MetricName: TypeAlias = Literal[
    "key_field_error_rate",
    "reading_order_error_rate",
    "final_card_error_rate",
    "bbox_error_rate",
    "three_image_p95_ms",
]
ErrorMetricName: TypeAlias = Literal[
    "key_field_error_rate",
    "reading_order_error_rate",
    "final_card_error_rate",
    "bbox_error_rate",
]
DecisionStatus: TypeAlias = Literal[
    "NOT_RUN_NO_EXACT_BINDING",
    "EXPERIMENT_ONLY",
    "PROMOTION_RECOMMENDED",
]
DecisionReason: TypeAlias = Literal[
    "NO_EXACT_BINDING",
    "METRIC_REGRESSION",
    "REQUIRED_ERROR_REDUCTION_NOT_MET",
    "ALL_METRICS_NON_REGRESSING_AND_REQUIRED_REDUCTION_MET",
]


class G04VlMetricReceiptV1(BaseModel):
    """Frozen, aggregate metrics produced outside this zero-network evaluator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["g04-vl-metric-receipt-v1"] = (
        "g04-vl-metric-receipt-v1"
    )
    engine: Literal["PADDLE", "VL"]
    evaluation_set_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,99}$")
    evaluation_set_sha256: str = Field(pattern=HASH_PATTERN)
    sample_count: int = Field(ge=1)
    key_field_error_rate: float = Field(ge=0, le=1)
    reading_order_error_rate: float = Field(ge=0, le=1)
    final_card_error_rate: float = Field(ge=0, le=1)
    bbox_error_rate: float = Field(ge=0, le=1)
    three_image_p95_ms: float = Field(ge=0)


class G04VlExactBindingV1(BaseModel):
    """Non-secret identity needed before measured VL evidence is admissible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["g04-vl-exact-binding-v1"] = "g04-vl-exact-binding-v1"
    provider: str = Field(min_length=1, max_length=100)
    account_ref: str = Field(min_length=1, max_length=200)
    region: str = Field(min_length=1, max_length=100)
    model_snapshot: str = Field(min_length=1, max_length=200)
    cost_readback: str = Field(min_length=1, max_length=300)
    eligible: Literal[True] = True
    external_image_redaction_required: Literal[True] = True


class G04VlComparisonV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics_checked: tuple[MetricName, ...]
    regressed_metrics: tuple[MetricName, ...]
    qualifying_error_reductions: tuple[ErrorMetricName, ...]
    relative_error_reductions: dict[ErrorMetricName, float]

    @model_validator(mode="after")
    def validate_metric_sets(self) -> "G04VlComparisonV1":
        if self.metrics_checked != ALL_METRICS:
            raise ValueError("comparison must check the complete locked metric set")
        if set(self.relative_error_reductions) != set(ERROR_METRICS):
            raise ValueError("comparison must report every locked error metric")
        if any(item not in self.metrics_checked for item in self.regressed_metrics):
            raise ValueError("regressed metrics must belong to the checked metric set")
        if any(
            item not in self.relative_error_reductions
            for item in self.qualifying_error_reductions
        ):
            raise ValueError("qualifying reductions must belong to the error metric set")
        return self


class G04VlDecisionReceiptV1(BaseModel):
    """Deterministic admission decision; it never changes the runtime engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["g04-vl-decision-receipt-v1"] = (
        "g04-vl-decision-receipt-v1"
    )
    status: DecisionStatus
    reasons: tuple[DecisionReason, ...]
    exact_binding_sha256: str | None = Field(default=None, pattern=HASH_PATTERN)
    paddle_receipt_sha256: str | None = Field(default=None, pattern=HASH_PATTERN)
    vl_receipt_sha256: str | None = Field(default=None, pattern=HASH_PATTERN)
    comparison: G04VlComparisonV1 | None = None
    evaluation_network_calls: Literal[0] = 0
    evaluation_provider_calls: Literal[0] = 0
    runtime_default: Literal["PADDLE"] = "PADDLE"
    runtime_change_applied: Literal[False] = False

    @model_validator(mode="after")
    def validate_decision(self) -> "G04VlDecisionReceiptV1":
        evidence_hashes = (
            self.exact_binding_sha256,
            self.paddle_receipt_sha256,
            self.vl_receipt_sha256,
        )
        if self.status == "NOT_RUN_NO_EXACT_BINDING":
            if self.reasons != ("NO_EXACT_BINDING",):
                raise ValueError("not-run decisions require the no-binding reason")
            if any(value is not None for value in evidence_hashes):
                raise ValueError("not-run decisions cannot claim comparison evidence")
            if self.comparison is not None:
                raise ValueError("not-run decisions cannot contain a comparison")
            return self

        if any(value is None for value in evidence_hashes) or self.comparison is None:
            raise ValueError("evaluated decisions require bound evidence and comparison")

        if self.status == "PROMOTION_RECOMMENDED":
            if self.reasons != (
                "ALL_METRICS_NON_REGRESSING_AND_REQUIRED_REDUCTION_MET",
            ):
                raise ValueError("promotion requires the complete success reason")
            if self.comparison.regressed_metrics:
                raise ValueError("promotion cannot contain a regressed metric")
            if not self.comparison.qualifying_error_reductions:
                raise ValueError("promotion requires a qualifying error reduction")
        else:
            expected_reasons: list[DecisionReason] = []
            if self.comparison.regressed_metrics:
                expected_reasons.append("METRIC_REGRESSION")
            if not self.comparison.qualifying_error_reductions:
                expected_reasons.append("REQUIRED_ERROR_REDUCTION_NOT_MET")
            if self.reasons != tuple(expected_reasons):
                raise ValueError(
                    "experiment-only reasons must match the comparison outcome"
                )
        return self
