from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from app.trip_understanding.full_text import build_full_text_pipeline
from app.trip_understanding.qwen_provider import (
    QwenStructuredInferenceProvider,
    qwen_effective_run_config_sha256,
)
from evals.agent_gate_v1.path_security import (
    require_external_target,
    write_external_bytes_exclusive,
)
from evals.trip_text_cards_agent_v2.split_loader import load_agent_split


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DATA_ROOT = BACKEND_ROOT / "eval_data" / "trip_text_cards_v1"
PANEL_PATH = (
    BACKEND_ROOT
    / "eval_data"
    / "trip_text_cards_agent_v2"
    / "qwen_model_panel.json"
)
ROLES = (
    "QUALITY_CEILING",
    "PRODUCTION_CANDIDATE",
    "LOW_LATENCY_CANDIDATE",
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _price(candidate: dict[str, object], price_type: str) -> float | None:
    pricing = candidate.get("pricing")
    if not isinstance(pricing, list):
        return None
    for band in pricing:
        if not isinstance(band, dict):
            continue
        rows = band.get("prices")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("type") != price_type:
                continue
            try:
                return float(row.get("price"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
    return None


def _candidate(panel: dict[str, object], role: str) -> dict[str, object]:
    values = panel.get("candidates")
    if not isinstance(values, list):
        raise ValueError("Qwen model panel has no candidates")
    for value in values:
        if isinstance(value, dict) and value.get("role") == role:
            return value
    raise ValueError(f"Qwen model panel has no {role} candidate")


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _primary_binding(binding: dict[str, object]) -> dict[str, object]:
    primary = binding.get("primary_provider_binding")
    return primary if isinstance(primary, dict) else binding


def _call_rows(binding: dict[str, object]) -> list[dict[str, object]]:
    calls = binding.get("calls")
    if not isinstance(calls, list):
        return []
    return [value for value in calls if isinstance(value, dict)]


def _output_targets(
    output_dir: Path,
    *,
    candidate_commit: str,
    role: str,
) -> tuple[Path, Path]:
    stem = f"{candidate_commit[:12]}-{role.lower()}"
    predictions = output_dir / f"{stem}.predictions.jsonl"
    summary = output_dir / f"{stem}.summary.json"
    require_external_target(predictions, REPOSITORY_ROOT)
    require_external_target(summary, REPOSITORY_ROOT)
    return predictions, summary


async def _run(args: argparse.Namespace) -> tuple[dict[str, object], bytes]:
    if args.concurrency != 1:
        raise ValueError("Qwen exact evidence requires serial concurrency=1")
    if args.deadline_seconds != 7.0 or args.max_output_tokens != 2048:
        raise ValueError("Qwen exact evidence requires the frozen 7s/2048 limits")
    if _git("status", "--porcelain"):
        raise ValueError("Qwen prediction runner requires a clean candidate checkout")
    candidate_commit = _git("rev-parse", "HEAD")
    candidate_tree = _git("rev-parse", "HEAD^{tree}")
    panel_bytes = PANEL_PATH.read_bytes()
    panel = json.loads(panel_bytes)
    candidate = _candidate(panel, args.role)
    model = candidate.get("exact_model_id")
    if not isinstance(model, str) or model == "NOT_EXPOSED_BY_PROVIDER":
        raise ValueError("Qwen exact model ID is unavailable")

    cases = []
    split_receipts = {}
    for split in args.splits:
        split_cases, receipt = load_agent_split(DATA_ROOT, split)
        cases.extend(split_cases)
        split_receipts[split] = {
            "artifact_sha256": receipt.artifact_sha256,
            "case_count": len(split_cases),
            "blind_inputs_read": receipt.blind_inputs_read,
            "blind_truth_read": receipt.blind_truth_read,
        }

    provider = QwenStructuredInferenceProvider(
        api_key=os.getenv("QWEN_API_KEY", ""),
        base_url=os.getenv("QWEN_API_URL", ""),
        model=model,
        deadline_seconds=args.deadline_seconds,
        max_output_tokens=args.max_output_tokens,
        max_concurrency=args.concurrency,
        input_cny_per_million=_price(candidate, "input_token"),
        output_cny_per_million=_price(candidate, "output_token"),
    )
    pipeline = build_full_text_pipeline(primary_inference_provider=provider)
    semaphore = asyncio.Semaphore(args.concurrency)
    completed = 0
    completed_lock = asyncio.Lock()

    async def run_case(index: int, case) -> tuple[int, dict[str, object]]:
        nonlocal completed
        async with semaphore:
            try:
                output = await pipeline.run(case.input_text)
                binding = output.inference_binding
                fallback_used = binding.get("fallback_used") is True
                proposal = output.proposal.model_dump(mode="json")
                result: dict[str, object] = {
                    "schema_version": "g01-qwen-prediction-case-v1",
                    "case_id": case.case_id,
                    "split": case.split,
                    "source_sha256": case.normalized_input_sha256,
                    "status": (
                        "EDITABLE_PARTIAL_RESULT"
                        if fallback_used
                        else "VALID_MODEL_OUTPUT"
                    ),
                    "schema_valid_model_output": not fallback_used,
                    "editable_partial_result": (
                        output.public_result.status == "PARTIAL_RESULT"
                    ),
                    "proposal": proposal,
                    "provider_binding": binding,
                }
            except Exception as exc:
                result = {
                    "schema_version": "g01-qwen-prediction-case-v1",
                    "case_id": case.case_id,
                    "split": case.split,
                    "source_sha256": case.normalized_input_sha256,
                    "status": "RUNNER_ERROR",
                    "schema_valid_model_output": False,
                    "editable_partial_result": False,
                    "error_type": type(exc).__name__,
                }
        async with completed_lock:
            completed += 1
            if completed % 10 == 0 or completed == len(cases):
                print(
                    json.dumps(
                        {
                            "role": args.role,
                            "completed": completed,
                            "total": len(cases),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
        return index, result

    indexed = await asyncio.gather(
        *(run_case(index, case) for index, case in enumerate(cases))
    )
    results = [value for _index, value in sorted(indexed)]
    encoded = b"".join(
        (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        for value in results
    )

    primary_bindings = [
        _primary_binding(value["provider_binding"])
        for value in results
        if isinstance(value.get("provider_binding"), dict)
    ]
    call_rows = [row for binding in primary_bindings for row in _call_rows(binding)]
    model_valid = sum(value["schema_valid_model_output"] is True for value in results)
    partial = sum(value["status"] == "EDITABLE_PARTIAL_RESULT" for value in results)
    runner_errors = sum(value["status"] == "RUNNER_ERROR" for value in results)
    proposals = [
        value["proposal"]
        for value in results
        if isinstance(value.get("proposal"), dict)
    ]
    mentions = [
        mention
        for proposal in proposals
        for mention in proposal.get("mentions", [])
        if isinstance(mention, dict)
    ]
    atomic_values = [
        value
        for mention in mentions
        if isinstance((value := mention.get("atomic_place_name")), str)
    ]
    forbidden_markers = ("预约", "说明", "网址", "链接", "http://", "https://")
    sentence_markers = set("。！？；\n")
    failure_categories = Counter(
        str(binding.get("failure_category", "NONE"))
        for binding in primary_bindings
        if binding.get("failure_category") is not None
    )
    validation_failures = Counter(
        str(row["validation_failure"])
        for row in call_rows
        if row.get("validation_failure") is not None
    )
    latencies = [
        float(binding["latency_ms"])
        for binding in primary_bindings
        if isinstance(binding.get("latency_ms"), int | float)
    ]
    summary: dict[str, object] = {
        "schema_version": "g01-qwen-model-prediction-summary-v1",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "candidate_commit": candidate_commit,
        "candidate_tree": candidate_tree,
        "model_role": args.role,
        "exact_model_id": model,
        "effective_snapshot_id": candidate.get("effective_snapshot_id"),
        "binding_mode": candidate.get("binding_mode"),
        "region": panel.get("region", "NOT_EXPOSED_BY_PROVIDER"),
        "endpoint_sha256": panel.get("endpoint_sha256"),
        "context": candidate.get("context", "NOT_EXPOSED_BY_PROVIDER"),
        "directory_structured_output": candidate.get(
            "structured_output",
            "NOT_EXPOSED_BY_PROVIDER",
        ),
        "model_panel_sha256": hashlib.sha256(panel_bytes).hexdigest(),
        "prompt_sha256": provider.prompt_sha256,
        "schema_sha256": provider.schema_sha256,
        "schema_canonical_sha256": provider.schema_canonical_sha256,
        "config_sha256": provider.config_sha256,
        "effective_config_sha256": provider.effective_config_sha256,
        "batch_concurrency": args.concurrency,
        "provider_max_concurrency": provider.max_concurrency,
        "deadline_ms": round(args.deadline_seconds * 1000),
        "max_output_tokens": args.max_output_tokens,
        "requested_splits": list(args.splits),
        "effective_run_config_sha256": qwen_effective_run_config_sha256(
            model_role=args.role,
            splits=args.splits,
            batch_concurrency=args.concurrency,
            provider_effective_config_sha256=provider.effective_config_sha256,
        ),
        "splits": split_receipts,
        "case_count": len(results),
        "schema_valid_model_output_count": model_valid,
        "schema_valid_rate": round(model_valid / len(results), 6),
        "editable_partial_result_count": partial,
        "runner_error_count": runner_errors,
        "external_call_count": sum(
            int(binding.get("external_calls", 0)) for binding in primary_bindings
        ),
        "repair_call_count": sum(
            int(binding.get("repair_call_count", 0)) for binding in primary_bindings
        ),
        "failure_categories": dict(sorted(failure_categories.items())),
        "validation_failures": dict(sorted(validation_failures.items())),
        "accepted_mention_count": len(mentions),
        "accepted_atomic_place_count": len(atomic_values),
        "accepted_forbidden_atomic_count": sum(
            any(marker in value.casefold() for marker in forbidden_markers)
            for value in atomic_values
        ),
        "accepted_sentence_atomic_count": sum(
            any(marker in value for marker in sentence_markers)
            for value in atomic_values
        ),
        "latency_ms_p50": _percentile(latencies, 0.50),
        "latency_ms_p95": _percentile(latencies, 0.95),
        "input_tokens": sum(
            int(binding.get("input_tokens", 0)) for binding in primary_bindings
        ),
        "output_tokens": sum(
            int(binding.get("output_tokens", 0)) for binding in primary_bindings
        ),
        "estimated_cost_cny": round(
            sum(
                float(binding.get("estimated_cost_cny") or 0.0)
                for binding in primary_bindings
            ),
            8,
        ),
        "provider_reported_models": sorted(
            {
                str(row["provider_reported_model"])
                for row in call_rows
                if row.get("provider_reported_model")
                not in {None, "NOT_RECEIVED", "NOT_EXPOSED_BY_PROVIDER"}
            }
        ),
        "raw_predictions_sha256": hashlib.sha256(encoded).hexdigest(),
        "raw_predictions_size": len(encoded),
        "raw_predictions_storage": "REPOSITORY_EXTERNAL",
        "raw_request_or_response_retained": False,
        "blind_inputs_read": 0,
        "blind_truth_read": 0,
        "human_evidence": False,
        "quality_metrics_requiring_reference": "NOT_SCORED",
    }
    return summary, encoded


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument(
        "--splits",
        nargs="+",
        choices=("dev", "validation"),
        default=["dev", "validation"],
    )
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--deadline-seconds", type=float, default=7.0)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    args = parser.parse_args()
    if args.concurrency != 1:
        parser.error("concurrency must be exactly 1 for Qwen exact evidence")
    if args.deadline_seconds != 7.0 or args.max_output_tokens != 2048:
        parser.error("Qwen exact evidence requires deadline=7.0 and max tokens=2048")
    candidate_commit = _git("rev-parse", "HEAD")
    prediction_path, summary_path = _output_targets(
        args.output_dir,
        candidate_commit=candidate_commit,
        role=args.role,
    )
    summary, predictions = asyncio.run(_run(args))
    if summary["candidate_commit"] != candidate_commit:
        raise ValueError("candidate changed after Qwen output preflight")
    predictions_snapshot = write_external_bytes_exclusive(
        prediction_path,
        predictions,
        REPOSITORY_ROOT,
    )
    if predictions_snapshot.sha256 != summary["raw_predictions_sha256"]:
        raise ValueError("external prediction write hash mismatch")
    summary_bytes = (
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    summary_snapshot = write_external_bytes_exclusive(
        summary_path,
        summary_bytes,
        REPOSITORY_ROOT,
    )
    print(
        json.dumps(
            {
                "status": "COMPLETE",
                "role": args.role,
                "case_count": summary["case_count"],
                "schema_valid_rate": summary["schema_valid_rate"],
                "editable_partial_result_count": summary[
                    "editable_partial_result_count"
                ],
                "latency_ms_p95": summary["latency_ms_p95"],
                "summary_sha256": summary_snapshot.sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
