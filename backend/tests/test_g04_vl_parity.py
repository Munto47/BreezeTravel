from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from app.trip_understanding.screenshot_vl import (
    ALL_METRICS,
    G04VlReceiptError,
    evaluate_vl_candidate,
)
from scripts.run_g04_vl_parity import main


def _binding(**overrides: Any) -> dict[str, Any]:
    value = {
        "schema_version": "g04-vl-exact-binding-v1",
        "provider": "future-vl-provider",
        "account_ref": "approved-account-ref",
        "region": "approved-region",
        "model_snapshot": "frozen-model-snapshot",
        "cost_readback": "approved-existing-cost-boundary",
        "eligible": True,
        "external_image_redaction_required": True,
    }
    value.update(overrides)
    return value


def _metrics(engine: str, **overrides: Any) -> dict[str, Any]:
    value = {
        "schema_version": "g04-vl-metric-receipt-v1",
        "engine": engine,
        "evaluation_set_id": "g04-parity-set-v1",
        "evaluation_set_sha256": "a" * 64,
        "sample_count": 100,
        "key_field_error_rate": 0.20,
        "reading_order_error_rate": 0.20,
        "final_card_error_rate": 0.20,
        "bbox_error_rate": 0.20,
        "three_image_p95_ms": 1000.0,
    }
    value.update(overrides)
    return value


def _winning_candidate(**overrides: Any) -> dict[str, Any]:
    value = _metrics(
        "VL",
        key_field_error_rate=0.15,
        reading_order_error_rate=0.15,
        final_card_error_rate=0.15,
        bbox_error_rate=0.15,
        three_image_p95_ms=900.0,
    )
    value.update(overrides)
    return value


def test_no_binding_is_an_honest_zero_network_not_run() -> None:
    decision = evaluate_vl_candidate()

    assert decision.status == "NOT_RUN_NO_EXACT_BINDING"
    assert decision.reasons == ("NO_EXACT_BINDING",)
    assert decision.comparison is None
    assert decision.evaluation_network_calls == 0
    assert decision.evaluation_provider_calls == 0
    assert decision.runtime_default == "PADDLE"
    assert decision.runtime_change_applied is False


@pytest.mark.parametrize(
    ("metric", "worse_value"),
    [
        ("key_field_error_rate", 0.21),
        ("reading_order_error_rate", 0.21),
        ("final_card_error_rate", 0.21),
        ("bbox_error_rate", 0.21),
        ("three_image_p95_ms", 1001.0),
    ],
)
def test_any_metric_regression_keeps_vl_experimental(
    metric: str,
    worse_value: float,
) -> None:
    candidate = _winning_candidate(**{metric: worse_value})

    decision = evaluate_vl_candidate(
        _metrics("PADDLE"),
        candidate,
        exact_binding=_binding(),
    )

    assert decision.status == "EXPERIMENT_ONLY"
    assert decision.comparison is not None
    assert decision.comparison.metrics_checked == ALL_METRICS
    assert decision.comparison.regressed_metrics == (metric,)
    assert "METRIC_REGRESSION" in decision.reasons
    assert decision.runtime_change_applied is False


def test_non_regressing_candidate_without_twenty_percent_improvement_is_experimental() -> None:
    candidate = _metrics(
        "VL",
        key_field_error_rate=0.17,
        reading_order_error_rate=0.17,
        final_card_error_rate=0.17,
        bbox_error_rate=0.17,
        three_image_p95_ms=900.0,
    )

    decision = evaluate_vl_candidate(
        _metrics("PADDLE"),
        candidate,
        exact_binding=_binding(),
    )

    assert decision.status == "EXPERIMENT_ONLY"
    assert decision.reasons == ("REQUIRED_ERROR_REDUCTION_NOT_MET",)
    assert decision.comparison is not None
    assert decision.comparison.regressed_metrics == ()
    assert decision.comparison.qualifying_error_reductions == ()


def test_complete_win_recommends_promotion_without_applying_runtime_change() -> None:
    decision = evaluate_vl_candidate(
        _metrics("PADDLE"),
        _winning_candidate(),
        exact_binding=_binding(),
    )

    assert decision.status == "PROMOTION_RECOMMENDED"
    assert decision.reasons == (
        "ALL_METRICS_NON_REGRESSING_AND_REQUIRED_REDUCTION_MET",
    )
    assert decision.comparison is not None
    assert decision.comparison.regressed_metrics == ()
    assert decision.comparison.qualifying_error_reductions == (
        "key_field_error_rate",
        "reading_order_error_rate",
        "final_card_error_rate",
        "bbox_error_rate",
    )
    assert decision.runtime_default == "PADDLE"
    assert decision.runtime_change_applied is False
    assert len(decision.exact_binding_sha256 or "") == 64
    assert len(decision.paddle_receipt_sha256 or "") == 64
    assert len(decision.vl_receipt_sha256 or "") == 64


@pytest.mark.parametrize(
    ("paddle", "candidate", "binding"),
    [
        (_metrics("VL"), _winning_candidate(), _binding()),
        (_metrics("PADDLE"), _metrics("PADDLE"), _binding()),
        (
            _metrics("PADDLE"),
            _winning_candidate(evaluation_set_sha256="b" * 64),
            _binding(),
        ),
        (
            _metrics("PADDLE"),
            _winning_candidate(sample_count=99),
            _binding(),
        ),
        (
            _metrics("PADDLE", unexpected_field="not allowed"),
            _winning_candidate(),
            _binding(),
        ),
        (_metrics("PADDLE"), _winning_candidate(), _binding(eligible=False)),
    ],
)
def test_invalid_or_incomparable_receipts_are_rejected(
    paddle: dict[str, Any],
    candidate: dict[str, Any],
    binding: dict[str, Any],
) -> None:
    with pytest.raises(G04VlReceiptError):
        evaluate_vl_candidate(
            paddle,
            candidate,
            exact_binding=binding,
        )


def test_exact_binding_requires_both_metric_receipts() -> None:
    with pytest.raises(G04VlReceiptError):
        evaluate_vl_candidate(exact_binding=_binding())


def test_runner_without_binding_succeeds_without_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    exit_code = main([])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "NOT_RUN_NO_EXACT_BINDING"
    assert output["evaluation_network_calls"] == 0
    assert output["evaluation_provider_calls"] == 0


def test_runner_without_binding_does_not_read_metric_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "metric-receipt-does-not-exist.json"

    exit_code = main(
        [
            "--paddle-receipt",
            str(missing),
            "--vl-receipt",
            str(missing),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "NOT_RUN_NO_EXACT_BINDING"
