"""Read a current trip, compare outside database locks, then recheck its version."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from app.trip_understanding.errors import (
    CommandTargetChangedError, IdempotencyConflictError, IdempotencyInProgressError, RevisionConflictError,
)
from app.trip_understanding.map_render import InternalRouteModeFact, ROUTE_CONFIG_SHA256
from app.trip_understanding.models import UserFacingTripResult
from app.trip_understanding.relative_route_context import route_context
from app.trip_understanding.relative_route_options import DirectedRouteFacts, RouteEndpoint, build_relative_route_options
from app.trip_understanding.relative_route_previews import (
    PublicRelativeRouteOptions, issue_route_preview, verify_route_preview,
)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _scope(resource):
    return f"understanding:{resource.understanding_id}:relative-route-preview"


def _key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _map_pairs(plan):
    # Use the map renderer's exact confirmed-stop sequence, including selected
    # hotel endpoints. Do not zip all pending stops against saved edge indices.
    by_day = defaultdict(list)
    for stop in sorted(plan.stops, key=lambda s: (s.day_index, s.sequence_index)):
        if stop.resolution_status == "AUTO_MATCHED" and stop.canonical_place_id:
            by_day[stop.day_index].append(stop)
    return {(day, index): (a, b) for day, stops in by_day.items()
        for index, (a, b) in enumerate(zip(stops, stops[1:]))}


def _replay(value, *, resource, etag, day_index, now):
    view = PublicRelativeRouteOptions.model_validate(value)
    if view.day_index != day_index:
        raise IdempotencyConflictError("route request changed")
    for option in view.options:
        try:
            verify_route_preview(option.change_token, public_resource_id=resource.public_resource_id,
                expected_etag=etag, now=now)
        except CommandTargetChangedError:
            return PublicRelativeRouteOptions(status="NEEDS_UPDATE", day_index=day_index,
                message="这次路线比较已经过期，请重新比较。")
    return view


async def _compare(result, plan, proposal, edges, *, resource, etag, day_index, provider):
    try:
        visits, constraints = route_context(result, plan, proposal)
    except ValueError:
        return PublicRelativeRouteOptions(status="NEEDS_CONFIRMATION", day_index=day_index,
            message="这份行程的原文先后要求尚未核实，暂不生成自动改序建议。")
    if day_index > len(result.days):
        raise RevisionConflictError("requested day is not in the current trip")
    if provider is None:
        return PublicRelativeRouteOptions(status="UNAVAILABLE", day_index=day_index,
            message="真实交通服务暂不可用，未生成改序或节省量。")
    output = await build_relative_route_options(visits, day_index=day_index,
        constraints=constraints, current_edges=edges, provider=provider)
    now = datetime.now(UTC)
    options = [issue_route_preview(option, visits, public_resource_id=resource.public_resource_id,
        expected_etag=etag, now=now) for option in output.options if option.expires_at > now]
    if options:
        return PublicRelativeRouteOptions(status="AVAILABLE", day_index=day_index,
            message="已有真实交通对照，请查看变化路段，确认后才调整行程。", options=options)
    reason = output.no_options_reason
    if reason == "NO_IMPROVEMENT":
        status, message = "NO_IMPROVEMENT", "本次有限比较没有找到更省交通时间的局部调整；这不代表全局最优。"
    elif reason in {"CURRENT_ROUTES_NEED_UPDATE", "COMPARISON_EXPIRED"}:
        status, message = "NEEDS_UPDATE", "当前路线尚未核验或已经过期，请先手动更新地图路线。"
    elif reason in {"SOURCE_CONSTRAINTS_UNAVAILABLE", "INCOMPLETE_VISIT_SNAPSHOT", "NO_SAFE_CANDIDATE"}:
        status, message = "NEEDS_CONFIRMATION", "当前没有可核验的局部调整：可能受原文先后、餐宿、再访或待确认地点限制。"
    else:
        status, message = "UNAVAILABLE", "本次未完成真实交通对照，没有生成节省量；可以稍后重新比较。"
    return PublicRelativeRouteOptions(status=status, day_index=day_index, message=message)


class PostgresRelativeRouteRepositoryMixin:
    async def _read_relative_route_edges(self, conn, plan):
        snapshot = await conn.fetchrow("""SELECT s.snapshot_id,j.map_job_id,j.route_config_hash
            FROM trip_plan_revision_refs p JOIN trip_map_render_jobs j ON j.plan_ref_id=p.plan_ref_id
            JOIN trip_map_render_snapshots s ON s.map_job_id=j.map_job_id
            WHERE p.understanding_id=$1 AND p.revision_kind='UNDERSTANDING'
              AND p.aggregate_id=$1 AND p.revision=$2 AND j.route_config_hash=$3 AND p.stop_set_hash=$4
            ORDER BY s.finished_at DESC LIMIT 1""", plan.understanding_id, plan.plan_ref.revision, plan.route_config_hash, plan.plan_ref.stop_set_hash)
        if snapshot is None:
            return ()
        rows = await conn.fetch("""SELECT e.day_index,e.sequence_index,e.origin_name,e.destination_name,
            f.*,r.request_hash,r.external_call_count FROM trip_map_route_edges e
            JOIN trip_map_route_mode_facts f ON f.edge_id=e.edge_id
            JOIN trip_map_provider_effect_receipts r ON r.map_job_id=$2 AND r.mode=f.mode
              AND r.effect_key LIKE ('%:d' || e.day_index || ':e' || e.sequence_index || ':' || f.mode)
            WHERE e.snapshot_id=$1""", snapshot["snapshot_id"], snapshot["map_job_id"])
        pairs = _map_pairs(plan)
        groups = defaultdict(dict)
        for row in rows:
            pair = pairs.get((row["day_index"], row["sequence_index"]))
            if pair is None or (pair[0].name, pair[1].name) != (row["origin_name"], row["destination_name"]):
                continue
            fact = InternalRouteModeFact(mode=row["mode"], status=row["status"],
                duration_minutes=row["duration_minutes"], distance_meters=row["distance_meters"],
                transfer_count=row["transfer_count"], response_hash=row["response_hash"].strip(),
                request_hash=row["request_hash"].strip(), provider_binding=_json(row["provider_receipt_json"]),
                external_call_count=row["external_call_count"], observed_at=row["observed_at"], expires_at=row["expires_at"])
            groups[(row["day_index"], row["sequence_index"])][row["mode"]] = fact
        return tuple(DirectedRouteFacts(RouteEndpoint.from_stop(pairs[key][0]), RouteEndpoint.from_stop(pairs[key][1]),
            modes["walking"], modes["transit"], snapshot["route_config_hash"].strip())
            for key, modes in groups.items() if set(modes) == {"walking", "transit"})

    async def preview_relative_routes(self, resource, *, expected_etag, day_index, idempotency_key, request_hash, provider):
        pool = await self._get_pool()
        scope, key = _scope(resource), _key(idempotency_key)
        now = datetime.now(UTC)
        lease = now + timedelta(seconds=60)
        async with pool.acquire() as conn, conn.transaction():
            current = await self._lock_current_result(conn, resource)
            if current["opaque_etag"] != expected_etag:
                raise RevisionConflictError("route comparison version changed")
            claimed = await conn.fetchval("""INSERT INTO trip_understanding_idempotency_records
                (scope,key_hash,request_hash,state,lease_until,created_at) VALUES($1,$2,$3,'IN_PROGRESS',$4,$5)
                ON CONFLICT(scope,key_hash) DO NOTHING RETURNING scope""", scope, key, request_hash, lease, now)
            record = await conn.fetchrow("SELECT * FROM trip_understanding_idempotency_records WHERE scope=$1 AND key_hash=$2 FOR UPDATE", scope, key)
            if record["request_hash"].strip() != request_hash:
                raise IdempotencyConflictError("route request changed")
            if record["state"] == "COMPLETED":
                return _replay(_json(record["response_json"]), resource=resource, etag=expected_etag, day_index=day_index, now=now), True
            if claimed is None:
                if record["lease_until"] is not None and record["lease_until"] > now:
                    raise IdempotencyInProgressError("route comparison is in progress")
                await conn.execute("UPDATE trip_understanding_idempotency_records SET lease_until=$3 WHERE scope=$1 AND key_hash=$2", scope, key, lease)
            result = UserFacingTripResult.model_validate(_json(current["public_json"]))
            proposal = _json(current["proposal_json"])
            plan = await self._read_map_plan(conn, resource.understanding_id, int(current["current_revision"]))
            edges = await self._read_relative_route_edges(conn, plan)
        # No connection or row lock survives supplier IO.
        view = await _compare(result, plan, proposal, edges, resource=resource, etag=expected_etag,
            day_index=day_index, provider=provider)
        async with pool.acquire() as conn, conn.transaction():
            current = await self._lock_current_result(conn, resource)
            if current["opaque_etag"] != expected_etag:
                raise RevisionConflictError("trip changed during route comparison")
            updated = await conn.execute("""UPDATE trip_understanding_idempotency_records
                SET state='COMPLETED',response_status=200,response_json=$4::jsonb,response_headers_json=$5::jsonb,
                    lease_until=NULL,completed_at=clock_timestamp()
                WHERE scope=$1 AND key_hash=$2 AND state='IN_PROGRESS' AND lease_until=$3""", scope, key, lease,
                view.model_dump_json(), json.dumps({"ETag": expected_etag}))
            if updated != "UPDATE 1":
                raise IdempotencyInProgressError("route comparison lease changed")
        return view, False


class InMemoryRelativeRouteRepositoryMixin:
    async def preview_relative_routes(self, resource, *, expected_etag, day_index, idempotency_key, request_hash, provider):
        aggregate, stored = self._memory_g03_current(resource)
        if stored.opaque_etag != expected_etag:
            raise RevisionConflictError("route comparison version changed")
        scope = _scope(resource)
        existing = self._memory_g03_replay(scope=scope, idempotency_key=idempotency_key, request_hash=request_hash)
        if existing is not None:
            return _replay(existing, resource=resource, etag=expected_etag, day_index=day_index, now=datetime.now(UTC)), True
        active = getattr(self, "_relative_active", None)
        if active is None:
            self._relative_active = active = set()
        key = (scope, idempotency_key)
        if key in active:
            raise IdempotencyInProgressError("route comparison is in progress")
        active.add(key)
        try:
            revision = int(aggregate["current_revision"])
            result = stored.result.model_copy(deep=True)
            proposal = dict(self.g03_pipeline_inputs.get((resource.understanding_id, revision), {}))
            plan = self._memory_plan(resource.understanding_id, revision)
            jobs = [j for j in self.map_jobs.values() if j["understanding_id"] == resource.understanding_id
                and j["plan"].plan_ref.revision == revision and j["route_config_hash"] == ROUTE_CONFIG_SHA256
                and j["plan"].plan_ref.stop_set_hash == plan.plan_ref.stop_set_hash]
            output = self.map_snapshots.get(jobs[-1]["map_job_id"]) if jobs else None
            pairs = _map_pairs(plan)
            edges = tuple(DirectedRouteFacts(RouteEndpoint.from_stop(pairs[(e.day_index,e.sequence_index)][0]),
                RouteEndpoint.from_stop(pairs[(e.day_index,e.sequence_index)][1]), e.walking, e.transit, output.route_config_hash)
                for e in output.edges if (e.day_index,e.sequence_index) in pairs
                and (e.origin_name, e.destination_name) == tuple(s.name for s in pairs[(e.day_index,e.sequence_index)])) if output else ()
            view = await _compare(result, plan, proposal, edges, resource=resource, etag=expected_etag,
                day_index=day_index, provider=provider)
            _, current = self._memory_g03_current(resource)
            if current.opaque_etag != expected_etag:
                raise RevisionConflictError("trip changed during route comparison")
            self._remember_g03_outcome(scope=scope, idempotency_key=idempotency_key, request_hash=request_hash, outcome=view.model_dump())
            return view, False
        finally:
            active.discard(key)
