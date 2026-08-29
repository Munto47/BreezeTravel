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
from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.map_render import (
    ROUTE_CONFIG_SHA256,
    MapRenderPlan,
    MapRenderer,
    MapStop,
    PlanRevisionRef,
)
from app.trip_understanding.pipeline import canonical_sha256
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


def _stop(outcome, *, city: str, sequence_index: int) -> MapStop:
    if outcome.place is None:
        raise ValueError("route smoke place did not auto-match")
    coordinates = outcome.place.provider_binding.get("coordinates")
    if not isinstance(coordinates, dict):
        raise ValueError("route smoke place has no coordinates")
    return MapStop(
        day_index=1,
        day_label="Day 1",
        sequence_index=sequence_index,
        name=outcome.place.name,
        canonical_place_id=outcome.place.canonical_place_id,
        resolution_status="AUTO_MATCHED",
        city=city,
        longitude=float(coordinates["longitude"]),
        latitude=float(coordinates["latitude"]),
    )


async def _run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    if _git("status", "--porcelain"):
        raise ValueError("Amap map smoke requires a clean candidate checkout")
    api_key = os.getenv("AMAP_API_KEY", "")
    place_resolver = AmapPlaceResolver(api_key=api_key)
    origin_outcome, destination_outcome = await asyncio.gather(
        place_resolver.resolve(
            city=args.city,
            atomic_place_name=args.origin,
            category_hint=args.origin_category,
        ),
        place_resolver.resolve(
            city=args.city,
            atomic_place_name=args.destination,
            category_hint=args.destination_category,
        ),
    )
    origin = _stop(origin_outcome, city=args.city, sequence_index=0)
    destination = _stop(destination_outcome, city=args.city, sequence_index=1)
    stop_payload = [origin.model_dump(mode="json"), destination.model_dump(mode="json")]
    plan = MapRenderPlan(
        understanding_id="g01-live-map-smoke",
        plan_ref=PlanRevisionRef(
            kind="UNDERSTANDING",
            aggregate_id="g01-live-map-smoke",
            revision=1,
            stop_set_hash=canonical_sha256(stop_payload),
        ),
        route_config_hash=ROUTE_CONFIG_SHA256,
        stops=[origin, destination],
    )
    output = await MapRenderer(AmapRouteProvider(api_key=api_key)).render(plan)
    edge = output.edges[0] if output.edges else None
    receipt: dict[str, object] = {
        "schema_version": "g01-amap-map-render-smoke-v1",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "candidate_commit": _git("rev-parse", "HEAD"),
        "candidate_tree": _git("rev-parse", "HEAD^{tree}"),
        "city": args.city,
        "origin_sha256": _sha256_text(args.origin),
        "destination_sha256": _sha256_text(args.destination),
        "origin_place_receipt": origin_outcome.receipt,
        "destination_place_receipt": destination_outcome.receipt,
        "map_status": output.status,
        "snapshot_sha256": output.snapshot_sha256,
        "route_config_sha256": output.route_config_hash,
        "selected_mode": edge.selected_mode if edge is not None else None,
        "walking": (
            {
                "status": edge.walking.status,
                "duration_minutes": edge.walking.duration_minutes,
                "distance_meters": edge.walking.distance_meters,
                "transfer_count": edge.walking.transfer_count,
                "request_sha256": edge.walking.request_hash,
                "response_sha256": edge.walking.response_hash,
                "provider_binding": edge.walking.provider_binding,
                "external_calls": edge.walking.external_call_count,
            }
            if edge is not None
            else None
        ),
        "transit": (
            {
                "status": edge.transit.status,
                "duration_minutes": edge.transit.duration_minutes,
                "distance_meters": edge.transit.distance_meters,
                "transfer_count": edge.transit.transfer_count,
                "request_sha256": edge.transit.request_hash,
                "response_sha256": edge.transit.response_hash,
                "provider_binding": edge.transit.provider_binding,
                "external_calls": edge.transit.external_call_count,
            }
            if edge is not None
            else None
        ),
        "provider_binding": output.provider_binding,
        "raw_provider_response_retained": False,
        "blind_inputs_read": 0,
        "blind_truth_read": 0,
        "human_evidence": False,
    }
    return receipt, int(args.require_available and output.status != "READY")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--city", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--origin-category")
    parser.add_argument("--destination-category")
    parser.add_argument("--require-available", action="store_true")
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
                "status": receipt["map_status"],
                "selected_mode": receipt["selected_mode"],
                "output": str(snapshot.path),
                "receipt_sha256": snapshot.sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
