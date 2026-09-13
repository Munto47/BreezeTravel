"""Read the current lodging projection for one caller-owned local test trip.

Never exports authorization data, source snippets, or provider credentials.
This is a read-only measurement helper, not a replacement map provider.
"""
from __future__ import annotations

import asyncio
import json
import sys

import asyncpg
import experience

sys.path.insert(0, str(experience.ROOT / "backend"))
from app.trip_understanding.map_repository import _plan_for_result  # noqa: E402
from app.trip_understanding.models import UserFacingTripResult  # noqa: E402
from app.trip_understanding.overnight_context import overnight_segments  # noqa: E402


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def stop_fact(stop):
    return {"name": stop.name, "day": stop.day_index, "sequence": stop.sequence_index,
        "category": stop.category, "city": stop.city, "lodging_event": stop.lodging_event,
        "lodging_scope": stop.lodging_scope, "lodging_role_uncertain": stop.lodging_role_uncertain,
        "lodging_excluded_nights": stop.lodging_excluded_nights,
        "generated_overnight_endpoint": stop.is_stay_anchor,
        "resolution_status": stop.resolution_status, "canonical_place_id": stop.canonical_place_id}


def map_job_fact(row):
    binding = decoded(row["provider_binding_json"]) or {}
    failure = decoded(row["failure_json"]) or {}
    incomplete = bool(row["last_error_category"] or failure.get("category") or row["attempt"] > 1
        or row["status"] in {"QUEUED", "BUILDING"} or row["provider_binding_json"] is None)
    # Terminal worker failure snapshots currently carry placeholder zero-call
    # metadata. Neither that nor a later retry can recover prior HTTP attempts.
    metrics = {k: binding.get(k) for k in ("external_calls", "route_external_calls", "route_attempts",
        "route_cache_hits", "edge_count", "provider")}
    if failure.get("category"):
        metrics = {k: None for k in metrics}
    start, end = row["started_at"], row["finished_at"]
    return {"status": row["status"], "attempt": row["attempt"],
        "failure_category": row["last_error_category"] or failure.get("category"),
        "worker_started_at": start.isoformat() if start else None,
        "worker_finished_at": end.isoformat() if end else None,
        "worker_elapsed_ms": round((end-start).total_seconds()*1000, 2) if start and end else None,
        "metrics": metrics, "all_attempt_metrics_complete": not incomplete,
        "missing_metrics_note": "Worker retries or whole-job failures do not preserve every prior HTTP attempt; counts are incomplete, not zero." if incomplete else None}


async def read_map_jobs(conn, identifier):
    rows = await conn.fetch("""SELECT j.status,j.attempt,j.last_error_category,j.started_at,j.finished_at,
        s.provider_binding_json,s.failure_json FROM trip_map_render_jobs j
        LEFT JOIN trip_map_render_snapshots s ON s.map_job_id=j.map_job_id WHERE j.understanding_id=$1 ORDER BY j.created_at""", identifier)
    return [map_job_fact(row) for row in rows]


async def read(public_id):
    values = experience.read_env(experience.ENV_FILE)
    dsn = experience.environment(values)["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        aggregate = await conn.fetchrow("SELECT understanding_id,current_revision FROM trip_understandings WHERE public_resource_id=$1 AND state<>'DELETED'", public_id)
        if aggregate is None:
            return {"status": "NOT_FOUND"}
        identifier, revision = aggregate["understanding_id"], aggregate["current_revision"]
        row = await conn.fetchrow("""SELECT r.public_json,v.destination_json FROM trip_understanding_results r
            JOIN trip_understanding_revisions v ON v.understanding_id=r.understanding_id AND v.revision=r.revision
            WHERE r.understanding_id=$1 AND r.revision=$2""", identifier, revision)
        if row is None:
            return {"status": "NO_RESULT"}
        activities = await conn.fetch("""SELECT public_activity_token,canonical_place_id,resolution_status,resolver_receipt_json
            FROM trip_understanding_activities WHERE understanding_id=$1 AND revision=$2 AND role='PLANNED'""", identifier, revision)
        bindings = {a["public_activity_token"]: (a["canonical_place_id"], a["resolution_status"], decoded(a["resolver_receipt_json"])) for a in activities}
        destination = decoded(row["destination_json"])
        plan = _plan_for_result(identifier, revision, UserFacingTripResult.model_validate(decoded(row["public_json"])), bindings,
            city=destination.get("name") if isinstance(destination, dict) else None)
        contexts = overnight_segments(plan)
        map_jobs = await read_map_jobs(conn, identifier)
        candidates = await conn.fetch("""SELECT c.canonical_place_id,c.name,c.segment_key
            FROM trip_stay_candidates c JOIN trip_stay_recommendation_snapshots s ON s.snapshot_id=c.snapshot_id
            JOIN trip_stay_recommendation_jobs j ON j.stay_job_id=s.stay_job_id
            JOIN trip_plan_revision_refs p ON p.plan_ref_id=j.plan_ref_id
            WHERE j.understanding_id=$1 AND p.revision=$2 ORDER BY c.rank""", identifier, revision)
        return {"status": "READ", "revision": revision, "projection": "CURRENT_PERSISTED_RESULT_AND_REAL_RESOLVER_RECEIPTS",
            "stops": [stop_fact(stop) for stop in plan.stops], "lodging_constraints": [stop_fact(stop) for stop in plan.lodging_constraints],
            "stay_candidates": [dict(candidate) for candidate in candidates],
            "nights": [{"city": c.city, "overnight_days": c.overnight_days, "preserved_hotels": c.preserved_hotels,
                "pending_lodging_roles": c.pending_lodging_roles,
                "excluded_place_ids": c.excluded_place_ids, "unconfirmed_exclusions": c.unconfirmed_exclusions,
                "uncertain": c.uncertain, "expected_boundary_count": c.expected_boundary_count, "missing_boundaries": c.missing_boundaries,
                "anchors": [{"day": day, "direction": direction, **stop_fact(stop)} for day, direction, stop in c.anchors]} for c in contexts],
            "map_jobs": map_jobs}
    finally:
        await conn.close()


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("One local test resource is required")
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(asyncio.run(read(sys.argv[1])), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"status": "READ_FAILED", "error_category": type(error).__name__}))
        raise SystemExit(1) from None
