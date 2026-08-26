from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.trip_intake.extraction import (
    HybridTripIntakeExtractor,
    SchemaConstrainedTripIntakeExtractor,
)
from app.trip_intake.llm_client import StructuredJsonReceipt, StructuredJsonResult
from evals.trip_nlu_v2.analysis_scorer import score_nonblind
from evals.trip_nlu_v2.runner import (
    BudgetLedger,
    EvaluationBudgetExceeded,
    run_evaluation,
)
from evals.trip_nlu_v2.validator import DatasetValidationError, _read_jsonl


DATA_ROOT = Path("eval_data/trip_nlu_v2")


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in values),
        encoding="utf-8",
    )


def test_nonblind_scorer_supports_arbitrary_complete_subset(tmp_path: Path) -> None:
    labels = _read_jsonl(DATA_ROOT / "dev.jsonl")[:3]
    labels_path = tmp_path / "labels.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    _write_jsonl(labels_path, labels)
    _write_jsonl(
        predictions_path,
        [
            {"case_id": item["case_id"], "prediction": item["expected"]}
            for item in labels
        ],
    )

    receipt = score_nonblind(predictions_path, labels_path)

    assert receipt["case_count"] == 3
    assert receipt["gate"] == "PASS"
    assert len(receipt["case_details"]) == 3


def test_nonblind_scorer_can_select_targeted_dev_case(tmp_path: Path) -> None:
    labels = _read_jsonl(DATA_ROOT / "dev.jsonl")[:2]
    predictions_path = tmp_path / "predictions.jsonl"
    _write_jsonl(
        predictions_path,
        [{"case_id": labels[1]["case_id"], "prediction": labels[1]["expected"]}],
    )

    receipt = score_nonblind(
        predictions_path,
        DATA_ROOT / "dev.jsonl",
        case_ids={labels[1]["case_id"]},
    )

    assert receipt["case_count"] == 1
    assert receipt["gate"] == "PASS"


def test_nonblind_scorer_rejects_blind_case_ids(tmp_path: Path) -> None:
    label = _read_jsonl(DATA_ROOT / "validation.jsonl")[0]
    label = {**label, "case_id": "TRIP_NLU_0097"}
    labels_path = tmp_path / "labels.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    _write_jsonl(labels_path, [label])
    _write_jsonl(
        predictions_path,
        [{"case_id": label["case_id"], "prediction": label["expected"]}],
    )

    with pytest.raises(DatasetValidationError, match="refuses frozen blind"):
        score_nonblind(predictions_path, labels_path)


def test_budget_ledger_reserves_before_call_and_stops(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.json", max_calls=1)

    ledger.reserve_call()

    with pytest.raises(EvaluationBudgetExceeded, match="call budget"):
        ledger.reserve_call()
    assert json.loads((tmp_path / "budget.json").read_text())["model_calls"] == 1


@pytest.mark.asyncio
async def test_deterministic_runner_writes_bound_outputs(tmp_path: Path) -> None:
    receipt = await run_evaluation(
        data_root=DATA_ROOT,
        output_dir=tmp_path / "run",
        split="dev",
        mode="deterministic",
        case_ids={"TRIP_NLU_0001", "TRIP_NLU_0002"},
        git_commit="a" * 40,
        run_id="deterministic-test",
    )

    assert receipt["case_count"] == 2
    assert receipt["model_call_budget"] is None
    predictions = _read_jsonl(tmp_path / "run" / "predictions.jsonl")
    assert {item["case_id"] for item in predictions} == {
        "TRIP_NLU_0001",
        "TRIP_NLU_0002",
    }
    run_spec = json.loads((tmp_path / "run" / "run_spec.json").read_text())
    assert run_spec["product_outputs_sha256"] == receipt["predictions_sha256"]


class StubStructuredClient:
    async def generate_json(self, **kwargs):
        return StructuredJsonResult(
            payload={},
            receipt=StructuredJsonReceipt(
                requested_model=kwargs["model_name"],
                actual_model="DeepSeek-V4-Flash-0731",
                input_tokens=100,
                output_tokens=20,
                latency_ms=5,
                finish_reason="stop",
                system_fingerprint="fp-test",
            ),
        )


@pytest.mark.asyncio
async def test_hybrid_runner_updates_shared_budget(monkeypatch, tmp_path: Path) -> None:
    extractor = HybridTripIntakeExtractor(
        SchemaConstrainedTripIntakeExtractor(
            StubStructuredClient(),
            model_name="deepseek-v4-flash",
        )
    )
    monkeypatch.setattr(
        "evals.trip_nlu_v2.runner.build_trip_intake_extractor",
        lambda _settings: extractor,
    )
    settings = Settings(_env_file=None, deepseek_api_key="test-key")

    receipt = await run_evaluation(
        data_root=DATA_ROOT,
        output_dir=tmp_path / "hybrid-run",
        split="dev",
        mode="hybrid",
        budget_ledger_path=tmp_path / "budget.json",
        case_ids={"TRIP_NLU_0001"},
        settings=settings,
        git_commit="b" * 40,
        run_id="hybrid-test",
    )

    assert receipt["model_call_budget"]["model_calls"] == 1
    assert receipt["model_call_budget"]["input_tokens"] == 100
    assert receipt["actual_model_versions"] == {"DeepSeek-V4-Flash-0731": 1}


@pytest.mark.asyncio
async def test_blind_ledger_allows_only_one_claim(tmp_path: Path) -> None:
    ledger = tmp_path / "blind-ledger.json"
    await run_evaluation(
        data_root=DATA_ROOT,
        output_dir=tmp_path / "blind-first",
        split="frozen_blind",
        mode="deterministic",
        blind_ledger_path=ledger,
        git_commit="c" * 40,
        run_id="blind-first",
    )

    with pytest.raises(ValueError, match="already been claimed"):
        await run_evaluation(
            data_root=DATA_ROOT,
            output_dir=tmp_path / "blind-second",
            split="frozen_blind",
            mode="deterministic",
            blind_ledger_path=ledger,
            git_commit="c" * 40,
            run_id="blind-second",
        )
