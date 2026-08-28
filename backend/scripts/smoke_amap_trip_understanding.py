from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from app.trip_understanding.amap_place import AmapPlaceResolver
from evals.agent_gate_v1.path_security import write_external_bytes_exclusive


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    if _git("status", "--porcelain"):
        raise ValueError("Amap smoke requires a clean candidate checkout")
    resolver = AmapPlaceResolver(
        api_key=os.getenv("AMAP_API_KEY", ""),
        deadline_seconds=args.deadline_seconds,
    )
    outcome = await resolver.resolve(
        city=args.city,
        atomic_place_name=args.atomic_place,
        category_hint=args.category_hint,
    )
    place = outcome.place
    status = "AUTO_MATCHED" if place is not None else "NEEDS_CONFIRMATION"
    receipt: dict[str, object] = {
        "schema_version": "g01-amap-place-smoke-v1",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "candidate_commit": _git("rev-parse", "HEAD"),
        "candidate_tree": _git("rev-parse", "HEAD^{tree}"),
        "city": args.city,
        "atomic_place_sha256": _sha256_text(args.atomic_place),
        "category_hint_sha256": (
            _sha256_text(args.category_hint)
            if args.category_hint
            else "NOT_PROVIDED"
        ),
        "status": status,
        "canonical_place_id_sha256": (
            _sha256_text(place.canonical_place_id)
            if place is not None
            else "NOT_MATCHED"
        ),
        "matched_name_sha256": (
            _sha256_text(place.name) if place is not None else "NOT_MATCHED"
        ),
        "matched_category": place.category if place is not None else "NOT_MATCHED",
        "provider_receipt": outcome.receipt,
        "raw_provider_response_retained": False,
        "blind_inputs_read": 0,
        "blind_truth_read": 0,
        "human_evidence": False,
    }
    exit_code = int(args.require_auto_match and place is None)
    return receipt, exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--city", required=True)
    parser.add_argument("--atomic-place", required=True)
    parser.add_argument("--category-hint")
    parser.add_argument("--deadline-seconds", type=float, default=3.0)
    parser.add_argument("--require-auto-match", action="store_true")
    args = parser.parse_args()
    receipt, exit_code = asyncio.run(_run(args))
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
                "output": str(snapshot.path),
                "receipt_sha256": snapshot.sha256,
                "external_calls": receipt["provider_receipt"]["external_calls"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
