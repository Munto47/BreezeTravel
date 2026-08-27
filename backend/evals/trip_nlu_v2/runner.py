from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from app.config import Settings, get_settings
from app.trip_intake.extraction import (
    TRIP_INTAKE_SYSTEM_PROMPT,
    ExtractionOutcome,
    HybridTripIntakeExtractor,
)
from app.trip_intake.models import IntakeSource, IntakeSourceType
from app.trip_intake.runtime import build_trip_intake_extractor
from app.trip_intake.semantic import trip_intake_semantic_prompt_schema
from evals.trip_nlu_v2.validator import _read_jsonl


SplitName = Literal["dev", "validation", "frozen_blind"]
SPLIT_FILES: dict[SplitName, str] = {
    "dev": "dev.jsonl",
    "validation": "validation.jsonl",
    "frozen_blind": "frozen_blind.inputs.jsonl",
}

DEFAULT_MAX_CALLS = 300
DEFAULT_MAX_COST_CNY = 30.0
DEFAULT_USD_CNY = 8.0
# Conservative peak-rate snapshot from the approved Goal. The RunSpec records
# these values so later price changes do not rewrite historical estimates.
DEFAULT_INPUT_USD_PER_MILLION = 0.28
DEFAULT_OUTPUT_USD_PER_MILLION = 0.56
DEFAULT_MAX_INPUT_TOKENS_PER_CALL = 32768


class EvaluationBudgetExceeded(RuntimeError):
    pass


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


class BudgetLedger:
    def __init__(
        self,
        path: Path,
        *,
        max_calls: int = DEFAULT_MAX_CALLS,
        max_cost_cny: float = DEFAULT_MAX_COST_CNY,
        usd_cny: float = DEFAULT_USD_CNY,
        input_usd_per_million: float = DEFAULT_INPUT_USD_PER_MILLION,
        output_usd_per_million: float = DEFAULT_OUTPUT_USD_PER_MILLION,
        max_input_tokens_per_call: int = DEFAULT_MAX_INPUT_TOKENS_PER_CALL,
        max_output_tokens_per_call: int = 4096,
    ) -> None:
        self.path = path.resolve()
        self.limits = {
            "max_calls": max_calls,
            "max_cost_cny": max_cost_cny,
            "usd_cny": usd_cny,
            "input_usd_per_million": input_usd_per_million,
            "output_usd_per_million": output_usd_per_million,
            "max_input_tokens_per_call": max_input_tokens_per_call,
            "max_output_tokens_per_call": max_output_tokens_per_call,
        }
        if self.path.exists():
            self.value = json.loads(self.path.read_text(encoding="utf-8"))
            if self.value.get("limits") != self.limits:
                raise ValueError("budget ledger limits do not match this run")
        else:
            self.value = {
                "schema_version": "trip-nlu-v2-budget-ledger-v1",
                "limits": self.limits,
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_cny": 0.0,
                "failed_call_reservations": 0,
                "updated_at": None,
            }
            self._save()

    def _projected_call_cost(self) -> float:
        return (
            (
                self.limits["max_input_tokens_per_call"]
                * self.limits["input_usd_per_million"]
                + self.limits["max_output_tokens_per_call"]
                * self.limits["output_usd_per_million"]
            )
            / 1_000_000
            * self.limits["usd_cny"]
        )

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        _write_json(temporary, self.value)
        temporary.replace(self.path)

    def reserve_call(self) -> None:
        if self.value["model_calls"] >= self.limits["max_calls"]:
            raise EvaluationBudgetExceeded("model call budget exhausted")
        projected = self.value["estimated_cost_cny"] + self._projected_call_cost()
        if projected > self.limits["max_cost_cny"]:
            raise EvaluationBudgetExceeded("estimated CNY budget exhausted")
        # Calls are persisted before the network side effect. A crash therefore
        # cannot make an attempted request disappear from the budget.
        self.value["model_calls"] += 1
        self.value["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def record(self, outcome: ExtractionOutcome) -> None:
        receipt = outcome.runtime_receipt
        if receipt is None:
            raise ValueError("hybrid outcome did not provide a runtime receipt")
        self.value["input_tokens"] += receipt.input_tokens
        self.value["output_tokens"] += receipt.output_tokens
        if receipt.input_tokens or receipt.output_tokens:
            cost_cny = (
                (
                    receipt.input_tokens * self.limits["input_usd_per_million"]
                    + receipt.output_tokens * self.limits["output_usd_per_million"]
                )
                / 1_000_000
                * self.limits["usd_cny"]
            )
        else:
            # A failed request may still be billed while omitting usage. Keep a
            # conservative reservation rather than reporting zero cost.
            cost_cny = self._projected_call_cost()
            self.value["failed_call_reservations"] += 1
        self.value["estimated_cost_cny"] = round(
            self.value["estimated_cost_cny"] + cost_cny,
            8,
        )
        self.value["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.value))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _git_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _load_cases(
    data_root: Path,
    split: SplitName,
    case_ids: set[str] | None,
) -> list[dict[str, Any]]:
    cases = _read_jsonl(data_root / SPLIT_FILES[split])
    if case_ids is not None:
        if split != "dev":
            raise ValueError("targeted case selection is allowed only for dev")
        cases = [item for item in cases if item["case_id"] in case_ids]
        if {item["case_id"] for item in cases} != case_ids:
            raise ValueError("one or more requested dev case IDs do not exist")
    return cases


def _claim_blind_attempt(path: Path, run_id: str, git_commit: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "trip-nlu-v2-blind-attempt-v1",
        "run_id": run_id,
        "git_commit": git_commit,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
    except FileExistsError as exc:
        raise ValueError("frozen blind has already been claimed for this ledger") from exc


async def run_evaluation(
    *,
    data_root: Path,
    output_dir: Path,
    split: SplitName,
    mode: Literal["deterministic", "hybrid"],
    budget_ledger_path: Path | None = None,
    blind_ledger_path: Path | None = None,
    case_ids: set[str] | None = None,
    warmup_calls: int = 0,
    settings: Settings | None = None,
    run_id: str | None = None,
    git_commit: str | None = None,
) -> dict[str, Any]:
    data_root = data_root.resolve(strict=True)
    output_dir = output_dir.resolve()
    if output_dir == data_root or data_root in output_dir.parents:
        raise ValueError("evaluation outputs must not be written inside the frozen dataset")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("evaluation output directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True)

    base_settings = settings or get_settings()
    run_settings = base_settings.model_copy(update={"trip_intake_extractor_mode": mode})
    extractor = build_trip_intake_extractor(run_settings)
    if mode == "hybrid" and not isinstance(extractor, HybridTripIntakeExtractor):
        raise ValueError("hybrid evaluation requires a configured DeepSeek API key")
    if mode == "hybrid" and budget_ledger_path is None:
        raise ValueError("hybrid evaluation requires a shared budget ledger")

    manifest_path = data_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo_root = data_root.parents[2]
    commit = git_commit or _git_commit(repo_root)
    current_run_id = run_id or f"trip-nlu-{split}-{uuid4()}"
    if split == "frozen_blind":
        if blind_ledger_path is None:
            raise ValueError("frozen blind evaluation requires an external one-shot ledger")
        _claim_blind_attempt(blind_ledger_path, current_run_id, commit)

    budget = (
        BudgetLedger(
            budget_ledger_path,
            max_output_tokens_per_call=run_settings.trip_intake_max_output_tokens,
        )
        if budget_ledger_path is not None
        else None
    )
    cases = _load_cases(data_root, split, case_ids)
    if not cases:
        raise ValueError("evaluation selected no cases")

    async def invoke(case: dict[str, Any]) -> tuple[ExtractionOutcome, float]:
        if budget is not None:
            budget.reserve_call()
        source = IntakeSource(
            source_id=case["source_id"],
            source_type=IntakeSourceType.MANUAL_TEXT,
            text=case["input_text"],
            text_sha256=hashlib.sha256(case["input_text"].encode("utf-8")).hexdigest(),
        )
        started = time.perf_counter()
        outcome = await extractor.extract([source])
        elapsed_ms = (time.perf_counter() - started) * 1000
        if budget is not None:
            budget.record(outcome)
        return outcome, elapsed_ms

    for _ in range(warmup_calls):
        await invoke(cases[0])

    predictions: list[dict[str, Any]] = []
    latencies: list[float] = []
    actual_models: Counter[str] = Counter()
    error_categories: Counter[str] = Counter()
    fallback_count = 0
    parser_binding = None
    predictions_path = output_dir / "predictions.jsonl"
    with predictions_path.open("x", encoding="utf-8", newline="\n") as prediction_stream:
        for case in cases:
            outcome, elapsed_ms = await invoke(case)
            parser_binding = outcome.parser_binding
            latencies.append(elapsed_ms)
            runtime = outcome.runtime_receipt
            if runtime is not None:
                if runtime.actual_model:
                    actual_models[runtime.actual_model] += 1
                if runtime.error_category:
                    error_categories[runtime.error_category] += 1
                fallback_count += int(runtime.fallback_used)
            prediction = {
                "case_id": case["case_id"],
                "prediction": outcome.extraction.model_dump(mode="json"),
                "runtime": asdict(runtime) if runtime is not None else None,
                "status": outcome.status.value,
                "elapsed_ms": round(elapsed_ms, 3),
            }
            predictions.append(prediction)
            prediction_stream.write(
                json.dumps(prediction, ensure_ascii=False, sort_keys=True) + "\n"
            )
            prediction_stream.flush()

    predictions_sha256 = _file_sha256(predictions_path)
    if parser_binding is None:
        raise AssertionError("evaluation did not produce a parser binding")
    actual_model_version = (
        ",".join(sorted(actual_models))
        if actual_models
        else ("none" if mode == "deterministic" else "not-returned")
    )
    seal = json.loads(
        (data_root / "sealed" / "frozen_blind.validation_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    run_spec = {
        "schema_version": "trip-nlu-v2-run-spec-v1",
        "run_id": current_run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": commit,
        "split": split,
        "mode": mode,
        "dataset_manifest_sha256": _file_sha256(manifest_path),
        "dataset_inputs_sha256": _file_sha256(data_root / SPLIT_FILES[split]),
        "blind_label_sha256": seal["external_label_sha256"],
        "product_outputs_sha256": predictions_sha256,
        "validator_sha256": manifest["code_bindings"]["validator_sha256"],
        "scorer_sha256": manifest["code_bindings"]["scorer_sha256"],
        "schema_sha256": manifest["code_bindings"]["schema_sha256"],
        "model_binding": {
            "model_name": parser_binding.model_name,
            "model_version": actual_model_version,
            "prompt_sha256": hashlib.sha256(
                TRIP_INTAKE_SYSTEM_PROMPT.encode("utf-8")
            ).hexdigest(),
            "schema_sha256": manifest["code_bindings"]["schema_sha256"],
            "semantic_schema_sha256": _canonical_sha256(
                trip_intake_semantic_prompt_schema()
            ),
            "config_sha256": parser_binding.config_hash,
        },
    }
    run_spec_path = output_dir / "run_spec.json"
    _write_json(run_spec_path, run_spec)

    p95_ms = _percentile(latencies, 0.95)
    receipt = {
        "schema_version": "trip-nlu-v2-run-receipt-v1",
        "run_id": current_run_id,
        "split": split,
        "case_count": len(cases),
        "warmup_calls": warmup_calls,
        "model_call_budget": budget.snapshot() if budget is not None else None,
        "latency_ms": {
            "p50": round(_percentile(latencies, 0.50), 3),
            "p95": round(p95_ms, 3),
            "max": round(max(latencies), 3),
        },
        "fallback_count": fallback_count,
        "error_categories": dict(sorted(error_categories.items())),
        "actual_model_versions": dict(sorted(actual_models.items())),
        "predictions_sha256": predictions_sha256,
        "run_spec_sha256": _file_sha256(run_spec_path),
        "performance_gate": "PASS" if p95_ms <= 5000 else "REJECT",
        "budget_gate": (
            "PASS"
            if budget is None
            or (
                budget.value["model_calls"] <= budget.limits["max_calls"]
                and budget.value["estimated_cost_cny"] <= budget.limits["max_cost_cny"]
            )
            else "REJECT"
        ),
    }
    _write_json(output_dir / "run_receipt.json", receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--split", choices=tuple(SPLIT_FILES), required=True)
    parser.add_argument("--mode", choices=("deterministic", "hybrid"), required=True)
    parser.add_argument("--budget-ledger", type=Path)
    parser.add_argument("--blind-ledger", type=Path)
    parser.add_argument("--case-id", action="append", dest="case_ids")
    parser.add_argument("--warmup-calls", type=int, default=0)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file) if args.env_file is not None else None
    receipt = asyncio.run(
        run_evaluation(
            data_root=args.data_root,
            output_dir=args.output_dir,
            split=args.split,
            mode=args.mode,
            budget_ledger_path=args.budget_ledger,
            blind_ledger_path=args.blind_ledger,
            case_ids=set(args.case_ids) if args.case_ids else None,
            warmup_calls=args.warmup_calls,
            settings=settings,
        )
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
