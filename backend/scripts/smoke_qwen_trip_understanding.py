from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from app.trip_understanding.full_text import build_full_text_pipeline
from app.trip_understanding.qwen_provider import QwenStructuredInferenceProvider
from evals.agent_gate_v1.path_security import write_external_bytes_exclusive
from evals.trip_text_cards_agent_v2.split_loader import load_agent_split
from evals.trip_text_cards_v1.contracts import canonical_sha256


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DATA_ROOT = BACKEND_ROOT / "eval_data" / "trip_text_cards_v1"
PANEL_PATH = (
    BACKEND_ROOT
    / "eval_data"
    / "trip_text_cards_agent_v2"
    / "qwen_model_panel.json"
)


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
            value = row.get("price")
            try:
                return float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
    return None


def _candidate(panel: dict[str, object], role: str) -> dict[str, object]:
    candidates = panel.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Qwen model panel has no candidate list")
    for value in candidates:
        if isinstance(value, dict) and value.get("role") == role:
            return value
    raise ValueError(f"Qwen model panel has no {role} candidate")


async def _smoke(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    panel_bytes = PANEL_PATH.read_bytes()
    panel = json.loads(panel_bytes)
    candidate = _candidate(panel, args.role)
    model = candidate.get("exact_model_id")
    if not isinstance(model, str) or model == "NOT_EXPOSED_BY_PROVIDER":
        raise ValueError("Qwen exact model ID was not discovered")

    cases, split_receipt = load_agent_split(DATA_ROOT, args.split)
    case = next(
        (item for item in cases if args.case_id is None or item.case_id == args.case_id),
        None,
    )
    if case is None:
        raise ValueError("requested smoke case is not in the selected split")

    provider = QwenStructuredInferenceProvider(
        api_key=os.getenv("QWEN_API_KEY", ""),
        base_url=os.getenv("QWEN_API_URL", ""),
        model=model,
        deadline_seconds=args.deadline_seconds,
        max_output_tokens=args.max_output_tokens,
        input_cny_per_million=_price(candidate, "input_token"),
        output_cny_per_million=_price(candidate, "output_token"),
    )
    base: dict[str, object] = {
        "schema_version": "g01-qwen-structured-smoke-v1",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "case_id": case.case_id,
        "split": args.split,
        "source_sha256": case.normalized_input_sha256,
        "split_artifact_sha256": split_receipt.artifact_sha256,
        "model_panel_sha256": hashlib.sha256(panel_bytes).hexdigest(),
        "model_role": args.role,
        "exact_model_id": model,
        "effective_snapshot_id": candidate.get("effective_snapshot_id"),
        "binding_mode": candidate.get("binding_mode"),
        "directory_structured_output": candidate.get("structured_output"),
        "raw_request_or_response_retained": False,
        "blind_inputs_read": 0,
        "blind_truth_read": 0,
        "human_evidence": False,
    }
    output = await build_full_text_pipeline(
        primary_inference_provider=provider,
    ).run(case.input_text)
    proposal = output.proposal
    fallback_used = proposal.binding.get("fallback_used") is True

    role_counts = Counter(item.role.value for item in proposal.mentions)
    result = {
        **base,
        "status": "PARTIAL_RESULT" if fallback_used else "PASS",
        "strict_schema_live_probe": not fallback_used,
        "editable_partial_result": output.public_result.status == "PARTIAL_RESULT",
        "proposal_sha256": canonical_sha256(proposal.model_dump(mode="json")),
        "destination_name_sha256": hashlib.sha256(
            proposal.destination_name.encode("utf-8")
        ).hexdigest(),
        "destination_basis": proposal.destination_basis.value,
        "mention_count": len(proposal.mentions),
        "atomic_place_count": sum(
            item.atomic_place_name is not None for item in proposal.mentions
        ),
        "role_counts": dict(sorted(role_counts.items())),
        "span_integrity_passed": all(
            case.input_text[item.span_start : item.span_end] == item.raw_text
            for item in proposal.mentions
        ),
        "day_count": len(output.public_result.days),
        "editable_card_count": sum(
            len(day.activities) for day in output.public_result.days
        ),
        "provider_binding": proposal.binding,
    }
    return result, 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--split", choices=("dev", "validation"), default="dev")
    parser.add_argument("--case-id")
    parser.add_argument(
        "--role",
        choices=("QUALITY_CEILING", "PRODUCTION_CANDIDATE", "LOW_LATENCY_CANDIDATE"),
        default="PRODUCTION_CANDIDATE",
    )
    parser.add_argument("--deadline-seconds", type=float, default=7.0)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    args = parser.parse_args()
    receipt, exit_code = asyncio.run(_smoke(args))
    encoded = (
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    snapshot = write_external_bytes_exclusive(
        args.output,
        encoded,
        REPOSITORY_ROOT,
    )
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "failure_category": receipt.get("failure_category"),
                "exact_model_id": receipt["exact_model_id"],
                "external_call_count": (
                    receipt.get("provider_binding", {}).get(
                        "primary_external_call_count",
                        receipt.get("provider_binding", {}).get("external_calls", 0),
                    )
                ),
                "receipt_sha256": snapshot.sha256,
                "output": str(snapshot.path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
