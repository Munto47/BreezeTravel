from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .contracts import (
    ALL_METRICS,
    ERROR_METRICS,
    G04VlComparisonV1,
    G04VlDecisionReceiptV1,
    G04VlExactBindingV1,
    G04VlMetricReceiptV1,
)


RELATIVE_ERROR_REDUCTION_MIN = 0.20
COMPARISON_EPSILON = 1e-12

ModelT = TypeVar("ModelT", bound=BaseModel)
MetricReceiptInput = G04VlMetricReceiptV1 | Mapping[str, Any]
ExactBindingInput = G04VlExactBindingV1 | Mapping[str, Any]


class G04VlReceiptError(ValueError):
    """Raised when comparison inputs are malformed or not comparable."""


def _coerce_model(value: Any, model: type[ModelT], label: str) -> ModelT:
    if isinstance(value, model):
        return value
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise G04VlReceiptError(f"invalid {label}") from exc


def _canonical_sha256(value: BaseModel) -> str:
    payload = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_comparable_receipts(
    paddle: G04VlMetricReceiptV1,
    vl: G04VlMetricReceiptV1,
) -> None:
    if paddle.engine != "PADDLE":
        raise G04VlReceiptError("baseline receipt must use the PADDLE engine role")
    if vl.engine != "VL":
        raise G04VlReceiptError("candidate receipt must use the VL engine role")
    if paddle.evaluation_set_id != vl.evaluation_set_id:
        raise G04VlReceiptError("metric receipts use different evaluation set IDs")
    if paddle.evaluation_set_sha256 != vl.evaluation_set_sha256:
        raise G04VlReceiptError("metric receipts use different evaluation set hashes")
    if paddle.sample_count != vl.sample_count:
        raise G04VlReceiptError("metric receipts use different sample counts")


def _relative_reduction(baseline: float, candidate: float) -> float:
    if baseline <= COMPARISON_EPSILON:
        return 0.0
    return (baseline - candidate) / baseline


def evaluate_vl_candidate(
    paddle_receipt: MetricReceiptInput | None = None,
    vl_receipt: MetricReceiptInput | None = None,
    *,
    exact_binding: ExactBindingInput | None = None,
) -> G04VlDecisionReceiptV1:
    """Evaluate frozen receipts without network, provider, or runtime side effects."""

    if exact_binding is None:
        return G04VlDecisionReceiptV1(
            status="NOT_RUN_NO_EXACT_BINDING",
            reasons=("NO_EXACT_BINDING",),
        )

    binding = _coerce_model(exact_binding, G04VlExactBindingV1, "exact binding")
    if paddle_receipt is None or vl_receipt is None:
        raise G04VlReceiptError(
            "an exact binding requires both PADDLE and VL metric receipts"
        )
    paddle = _coerce_model(
        paddle_receipt,
        G04VlMetricReceiptV1,
        "PADDLE metric receipt",
    )
    vl = _coerce_model(vl_receipt, G04VlMetricReceiptV1, "VL metric receipt")
    _require_comparable_receipts(paddle, vl)

    regressed_metrics = tuple(
        metric
        for metric in ALL_METRICS
        if float(getattr(vl, metric))
        > float(getattr(paddle, metric)) + COMPARISON_EPSILON
    )
    relative_reductions = {
        metric: round(
            _relative_reduction(
                float(getattr(paddle, metric)),
                float(getattr(vl, metric)),
            ),
            6,
        )
        for metric in ERROR_METRICS
    }
    qualifying_reductions = tuple(
        metric
        for metric in ERROR_METRICS
        if _relative_reduction(
            float(getattr(paddle, metric)),
            float(getattr(vl, metric)),
        )
        + COMPARISON_EPSILON
        >= RELATIVE_ERROR_REDUCTION_MIN
    )
    comparison = G04VlComparisonV1(
        metrics_checked=ALL_METRICS,
        regressed_metrics=regressed_metrics,
        qualifying_error_reductions=qualifying_reductions,
        relative_error_reductions=relative_reductions,
    )

    reasons = []
    if regressed_metrics:
        reasons.append("METRIC_REGRESSION")
    if not qualifying_reductions:
        reasons.append("REQUIRED_ERROR_REDUCTION_NOT_MET")
    if reasons:
        status = "EXPERIMENT_ONLY"
    else:
        status = "PROMOTION_RECOMMENDED"
        reasons.append("ALL_METRICS_NON_REGRESSING_AND_REQUIRED_REDUCTION_MET")

    return G04VlDecisionReceiptV1(
        status=status,
        reasons=tuple(reasons),
        exact_binding_sha256=_canonical_sha256(binding),
        paddle_receipt_sha256=_canonical_sha256(paddle),
        vl_receipt_sha256=_canonical_sha256(vl),
        comparison=comparison,
    )
