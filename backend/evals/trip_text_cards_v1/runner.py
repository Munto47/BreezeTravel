from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.trip_understanding.full_text import build_full_text_pipeline
from evals.trip_text_cards_v1.contracts import (
    PredictedMention,
    TextCardInputCase,
    TextCardPrediction,
)


class BaselineRunError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stable_public_result(value: dict[str, Any]) -> dict[str, Any]:
    cloned = json.loads(json.dumps(value, ensure_ascii=False))
    for day in cloned.get("days", []):
        for activity in day.get("activities", []):
            if "activity_token" in activity:
                activity["activity_token"] = "opaque-eval-token-redacted"
    return cloned


async def predict_case(case: TextCardInputCase) -> TextCardPrediction:
    pipeline = build_full_text_pipeline()
    started = time.perf_counter()
    output = await pipeline.run(case.input_text)
    elapsed_ms = (time.perf_counter() - started) * 1000
    mentions: list[PredictedMention] = []
    for activity in output.activities:
        mention = activity.compiled.mention
        place = activity.place
        mentions.append(
            PredictedMention(
                span_start=mention.span_start,
                span_end=mention.span_end,
                raw_text=mention.raw_text,
                role=mention.role.value,
                day_index=mention.day_index,
                atomic_place_name=mention.atomic_place_name,
                eligible_for_place_search=activity.compiled.eligible_for_place_search,
                resolution_status=activity.resolution_status.value,
                canonical_place_id=place.canonical_place_id if place else None,
                canonical_city=output.proposal.destination_name if place else None,
                canonical_category=place.category if place else None,
            )
        )
    return TextCardPrediction(
        case_id=case.case_id,
        source_sha256=case.normalized_input_sha256,
        destination_name=output.proposal.destination_name,
        provider_binding=output.inference_binding,
        mentions=mentions,
        public_result=_stable_public_result(output.public_result.model_dump(mode="json")),
        measurement_scope="LOCAL_PIPELINE_ONLY",
        first_progress_ms=None,
        cards_ready_ms=elapsed_ms,
    )


async def run_cases(cases: list[TextCardInputCase]) -> list[TextCardPrediction]:
    return [await predict_case(case) for case in cases]


def write_baseline(
    *,
    split_cases: dict[str, list[TextCardInputCase]],
    output_root: Path,
    backend_root: Path,
    subject_commit: str,
) -> dict[str, Any]:
    if set(split_cases) - {"dev", "validation"}:
        raise BaselineRunError("local development runner is forbidden from reading frozen_blind")
    if not subject_commit or len(subject_commit) != 40:
        raise BaselineRunError("a full 40-character subject commit is required")
    output_root.mkdir(parents=True, exist_ok=True)
    split_receipts: dict[str, dict[str, Any]] = {}
    total_external_calls = 0
    for split, cases in split_cases.items():
        predictions = asyncio.run(run_cases(cases))
        output_path = output_root / f"{split}.predictions.jsonl"
        with output_path.open("w", encoding="utf-8", newline="\n") as handle:
            for prediction in predictions:
                handle.write(
                    json.dumps(
                        prediction.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                handle.write("\n")
        external_calls = sum(int(prediction.provider_binding.get("external_calls", 0)) for prediction in predictions)
        total_external_calls += external_calls
        split_receipts[split] = {
            "case_count": len(predictions),
            "prediction_sha256": _sha256(output_path),
            "predicted_mention_count": sum(len(item.mentions) for item in predictions),
            "eligible_mention_count": sum(
                mention.eligible_for_place_search
                for item in predictions
                for mention in item.mentions
            ),
            "auto_matched_count": sum(
                mention.resolution_status == "AUTO_MATCHED"
                for item in predictions
                for mention in item.mentions
            ),
            "external_calls": external_calls,
            "measurement_scope": "LOCAL_PIPELINE_ONLY",
        }

    receipt = {
        "schema_version": "g01-text-card-local-baseline-receipt-v1",
        "dataset_version": "g01-text-card-dataset-v1",
        "run_at": datetime.now(UTC).isoformat(),
        "subject_commit": subject_commit,
        "lane": "DETERMINISTIC_CONTROLLED_FIXTURE",
        "provider_binding": {
            "provider": "deterministic-controlled-text",
            "version": "v1",
            "place_resolver": "controlled_fixture_snapshot",
        },
        "code_bindings": {
            "pipeline_sha256": _sha256(backend_root / "app" / "trip_understanding" / "pipeline.py"),
            "provider_sha256": _sha256(backend_root / "app" / "trip_understanding" / "full_text.py"),
            "runner_sha256": _sha256(Path(__file__)),
            "dataset_contract_sha256": _sha256(
                backend_root / "eval_data" / "trip_text_cards_v1" / "dataset_contract.json"
            ),
        },
        "splits": split_receipts,
        "total_external_calls": total_external_calls,
        "human_labels_read": 0,
        "blind_inputs_read": 0,
        "blind_labels_read": 0,
        "quality_metrics": "NOT_SCORED_NO_HUMAN_GOLD",
        "gate_claim": "NOT_RUN",
    }
    receipt_path = output_root / "run_receipt.json"
    with receipt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        handle.write("\n")
    return receipt
