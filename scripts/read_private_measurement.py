"""Read minimal measurement facts for one explicitly named local test trip.

No source text, authentication data, coordinates, provider messages or unrelated
trips are exported. The browser measurement invokes this before deleting its own
anonymous test resource. Provider prices remain unknown unless configured.
"""
from __future__ import annotations

import asyncio
import json
import sys

import asyncpg
import experience
from read_private_lodging_measurement import read_map_jobs


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def job_times(row):
    start, end = row.get("started_at"), row.get("finished_at")
    return {"worker_started_at": start.isoformat() if start else None,
        "worker_finished_at": end.isoformat() if end else None,
        "worker_elapsed_ms": round((end-start).total_seconds()*1000, 2) if start and end else None}


def stay_job_fact(row):
    binding = decoded(row["provider_binding_json"]) or {}
    incomplete = bool(row["attempt"] > 1 or row["last_error_category"] or row["provider_binding_json"] is None)
    return {"status": row["status"], "attempt": row["attempt"], "failure_category": row["last_error_category"],
        **job_times(row), **{key: binding.get(key) for key in
        ("candidate_provider_calls", "administrative_calls", "route_external_calls", "route_cache_hits",
         "route_attempts", "expected_boundary_count", "missing_boundary_count")},
        "all_attempt_metrics_complete": not incomplete,
        "missing_metrics_note": "No complete HTTP history exists for failed or retried job attempts; unknown values are not zero." if incomplete else None}


async def read(public_id):
    values=experience.read_env(experience.ENV_FILE)
    dsn=experience.environment(values)["DATABASE_URL"].replace("postgresql+asyncpg://","postgresql://")
    conn=await asyncpg.connect(dsn)
    try:
        aggregate=await conn.fetchrow("SELECT understanding_id,current_revision,state FROM trip_understandings WHERE public_resource_id=$1 AND state<>'DELETED'",public_id)
        if aggregate is None:
            return {"status":"NOT_FOUND"}
        identifier=aggregate["understanding_id"]
        first_revision=await conn.fetchval("SELECT min(revision) FROM trip_understanding_results WHERE understanding_id=$1",identifier)
        measured_revision=first_revision if first_revision is not None else aggregate["current_revision"]
        activities=await conn.fetch("""SELECT day_index,sequence_index,atomic_place_name,role,canonical_place_id,resolution_status
            FROM trip_understanding_activities WHERE understanding_id=$1 AND revision=$2 ORDER BY day_index,sequence_index""",identifier,first_revision)
        inference=decoded(await conn.fetchval("SELECT inference_binding_json FROM trip_understanding_revisions WHERE understanding_id=$1 AND revision=$2",identifier,measured_revision)) or {}
        compiler=decoded(await conn.fetchval("SELECT compiler_receipt_json FROM trip_understanding_revisions WHERE understanding_id=$1 AND revision=$2",identifier,first_revision)) or {}
        place=compiler.get("place_resolution",{})
        dining=await conn.fetch("SELECT revision,status,attempts,finished_at,metrics_json FROM trip_daily_dining_jobs WHERE understanding_id=$1 ORDER BY revision",identifier)
        stay=await conn.fetch("""SELECT j.status,j.attempt,j.last_error_category,j.started_at,j.finished_at,s.provider_binding_json
            FROM trip_stay_recommendation_jobs j LEFT JOIN trip_stay_recommendation_snapshots s ON j.stay_job_id=s.stay_job_id
            WHERE j.understanding_id=$1 ORDER BY j.created_at""",identifier)
        understanding_jobs=await conn.fetch("SELECT status,attempt,last_error_category,started_at,finished_at FROM trip_understanding_jobs WHERE understanding_id=$1 ORDER BY created_at",identifier)
        return {"status":"READ", "trip_state":aggregate["state"],"initial_result_revision":first_revision,
            "failure_category":inference.get("failure_category"),"inference_outcome":inference.get("outcome"),
            "semantic_diagnostic_counts":inference.get("semantic_diagnostic_counts",{}),
            "initial_activities":[dict(row) for row in activities],
            "model_usage":{key:inference.get(key) for key in ("model","external_calls","input_tokens","output_tokens","estimated_cost_cny","latency_ms")},
            "model_calls":[{key:call.get(key) for key in ("attempt","stage","outcome","latency_ms","input_tokens","output_tokens")}
                for call in inference.get("calls",[]) if isinstance(call,dict)],
            "understanding_jobs":[{"status":row["status"],"attempt":row["attempt"],"failure_category":row["last_error_category"],
                **job_times(row),"all_attempt_metrics_complete":row["attempt"] <= 1 and bool(inference.get("calls"))
                    and all(all(call.get(key) is not None for key in ("latency_ms","input_tokens","output_tokens"))
                        for call in inference.get("calls",[]))} for row in understanding_jobs],
            "place_usage":{key:place.get(key) for key in ("eligible_count","attempted_count","auto_matched_count","needs_confirmation_count","provider_unavailable_count","unique_resolution_count","deduplicated_resolution_count","place_external_call_count")},
            "dining_jobs":[{"revision":row["revision"],"status":row["status"],"attempts":row["attempts"],
                "worker_finished_at":row["finished_at"].isoformat() if row["finished_at"] else None,
                "metrics":decoded(row["metrics_json"]) or None,
                "all_attempt_metrics_complete":row["attempts"] <= 1 and bool(decoded(row["metrics_json"]))
                    and not (decoded(row["metrics_json"]) or {}).get("route_calls_unknown") and row["status"] not in {"BUILDING","QUEUED"}}
                for row in dining],
            "stay_jobs":[stay_job_fact(row) for row in stay],
            "map_jobs":await read_map_jobs(conn,identifier)}
    finally:
        await conn.close()


if __name__ == "__main__":
    try:
        if len(sys.argv)!=2:
            raise ValueError("One local test resource is required")
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(asyncio.run(read(sys.argv[1])),ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"status":"READ_FAILED","error_category":type(error).__name__}))
        raise SystemExit(1) from None
