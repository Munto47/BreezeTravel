"""Rebuild frozen selections from an existing provider-candidate snapshot.

The command never calls Amap or an LLM. It keeps the original retrieval
snapshots byte-for-byte, reruns the current deterministic eligibility/ranking
pipeline, binds recorded anchor coordinates, and freezes a new selected-place
set for repeatable judging.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.nodes.synthesizer import _cap_places, delivery_per_category_cap  # noqa: E402
from app.constraints.candidate_selection import (  # noqa: E402
    _attach_delivered_attraction_evidence,
    _attach_low_transfer_core_evidence,
    _attach_shared_anchor_evidence,
    _drop_obviously_remote_meals,
    select_eligible_places,
)
from app.constraints.evidence_resolver import finalize_place_evidence  # noqa: E402
from app.constraints.location import extract_explicit_district_constraint  # noqa: E402
from app.constraints.place_identity import deduplicate_places  # noqa: E402
from app.constraints.recommendation_plan import (  # noqa: E402
    bind_geo_anchor_evidence,
    build_recommendation_plan,
    reserve_places_for_plan,
    slot_coverage,
)
from app.schemas.place import EvidenceStatus, Place  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _carry_verified_snapshot_fields(place: Place, previous: Place | None) -> Place:
    if previous is None:
        return place
    verified_routes = {
        item.slot_id: item
        for item in previous.geo_evidence
        if item.status == EvidenceStatus.VERIFIED
        and (item.constraint_kind == "route" or "route" in item.source)
    }
    merged = []
    replaced: set[str] = set()
    for item in place.geo_evidence:
        old = verified_routes.get(item.slot_id)
        if item.constraint_kind == "route" and old is not None:
            merged.append(old.model_copy(update={"constraint_kind": "route"}))
            replaced.add(item.slot_id)
        else:
            merged.append(item)
    merged.extend(
        item.model_copy(update={"constraint_kind": "route"})
        for slot_id, item in verified_routes.items()
        if slot_id not in replaced
    )
    updated = finalize_place_evidence(place.model_copy(update={"geo_evidence": merged}))
    # Preserve already-frozen copy only when the current evidence contract still
    # considers the candidate fully verified. Any newly UNKNOWN/confirmation
    # constraint drops generated prose instead of laundering it as evidence.
    if updated.selection_evidence_status == EvidenceStatus.VERIFIED:
        updated = updated.model_copy(update={
            "description": previous.description,
            "rag_meta": previous.rag_meta,
            "estimated_duration": previous.estimated_duration,
            "duration_basis": previous.duration_basis,
        })
    return updated


def refresh_snapshot(source: Path, dataset: Path) -> dict[str, Any]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not (payload.get("integrity") or {}).get("passed"):
        raise RuntimeError("source snapshot integrity failed")
    dataset_payload = json.loads(dataset.read_text(encoding="utf-8"))
    cases = dataset_payload.get("cases") if isinstance(dataset_payload, dict) else dataset_payload
    cases_by_id = {str(case["id"]): case for case in cases}

    refreshed_cases: list[dict[str, Any]] = []
    for frozen in payload.get("cases") or []:
        case = cases_by_id[str(frozen["id"])]
        snapshots = list(frozen.get("retrieval_snapshots") or [])
        audits = [audit for item in snapshots for audit in item.get("audits") or []]
        raw_places = [
            Place.model_validate(place)
            for item in snapshots
            for place in item.get("places") or []
        ]
        previous = {
            place.place_id: place
            for place in (
                Place.model_validate(value)
                for value in frozen.get("selected_places") or []
            )
        }
        district = extract_explicit_district_constraint(case["query"]) or ""
        plan = bind_geo_anchor_evidence(
            build_recommendation_plan(case["query"], case["city"], district),
            audits,
        )
        selected = select_eligible_places(
            deduplicate_places(raw_places),
            case["query"],
            district,
            plan,
        )
        selected = [
            _carry_verified_snapshot_fields(place, previous.get(place.place_id))
            for place in selected
        ]
        selected = reserve_places_for_plan(selected, plan)
        selected = _cap_places(
            selected,
            preserve_input_order=True,
            per_category_cap=delivery_per_category_cap(case["query"]),
        )
        selected = _attach_shared_anchor_evidence(selected, case["query"])
        selected = _attach_delivered_attraction_evidence(selected, case["query"])
        selected = _attach_low_transfer_core_evidence(selected, case["query"])
        selected = _drop_obviously_remote_meals(selected, case["query"])
        refreshed_cases.append({
            **frozen,
            "selected_places": [place.model_dump(mode="json") for place in selected],
            "selection": {
                "plan": plan.model_dump(mode="json"),
                "slot_coverage": slot_coverage(plan, selected),
                "candidate_count": len(raw_places),
                "selected_count": len(selected),
            },
        })

    return {
        **payload,
        "schema_version": "1.2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parent_snapshot": {
            "path": str(source),
            "sha256": _sha256(source),
        },
        "selection_refresh": {
            "provider_calls": 0,
            "llm_calls": 0,
            "case_count": len(refreshed_cases),
        },
        "cases": refreshed_cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=Path("backend/eval_data/daily_queries/cases.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    refreshed = refresh_snapshot(args.source, args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(refreshed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output),
        "case_count": len(refreshed["cases"]),
        "sha256": _sha256(args.output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
