from __future__ import annotations

import hashlib
import hmac
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from app.trip_understanding.errors import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    JobLeaseLostError,
    ResourceAccessDeniedError,
    ResourceGoneError,
    ResourceNotFoundError,
    ResourceNotReadyError,
    RevisionConflictError,
)
from app.trip_understanding.map_render import (
    ROUTE_CONFIG_SHA256,
    MapRenderAcceptedView,
    MapRenderJobRecord,
    MapRenderOutput,
    MapRenderPlan,
    MapRenderRequestOutcome,
    MapRenderView,
    MapStop,
    PlanRevisionRef,
    PublicMapDayView,
    PublicMapEdgeView,
    PublicRouteModeView,
    RouteGeometryPoint,
)
from app.trip_understanding.models import (
    MapReadinessView,
    PublicResourceRecord,
    UserFacingTripResult,
)
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.route_geometry import InMemoryRouteGeometryCache


_CONTROLLED_PLACE_PATH = Path(__file__).resolve().parents[1] / "data" / "amap_mock_places.json"
_CONTROLLED_PLACE_PAYLOAD = json.loads(_CONTROLLED_PLACE_PATH.read_text(encoding="utf-8"))


def _controlled_coordinates(name: str, city: str | None) -> dict[str, float] | None:
    if not city:
        return None
    matches = [
        item
        for item in _CONTROLLED_PLACE_PAYLOAD.get(city, [])
        if isinstance(item, dict) and item.get("name") == name and isinstance(item.get("coords"), dict)
    ]
    if len(matches) != 1:
        manual = {
            "前门大街": {"longitude": 116.3936, "latitude": 39.8992},
            "圆明园": {"longitude": 116.3039, "latitude": 40.0081},
        }
        return manual.get(name) if city == "北京" else None
    return {
        "longitude": float(matches[0]["coords"]["lng"]),
        "latitude": float(matches[0]["coords"]["lat"]),
    }


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _coordinates_from_receipt(
    receipt: dict[str, Any],
) -> tuple[float | None, float | None]:
    coordinates = receipt.get("coordinates")
    if not isinstance(coordinates, dict):
        return None, None
    try:
        longitude = float(coordinates["longitude"])
        latitude = float(coordinates["latitude"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None, None
    return longitude, latitude


def _plan_for_result(
    understanding_id: str,
    revision: int,
    result: UserFacingTripResult,
    activity_bindings: dict[str, tuple[str | None, str, dict[str, Any]]],
    *,
    city: str | None = None,
) -> MapRenderPlan:
    stops: list[MapStop] = []
    for day_index, day in enumerate(result.days, start=1):
        for sequence_index, card in enumerate(day.activities):
            canonical_place_id, stored_status, resolver_receipt = activity_bindings.get(
                card.activity_token,
                (None, "NEEDS_CONFIRMATION", {}),
            )
            longitude, latitude = _coordinates_from_receipt(resolver_receipt)
            if stored_status == "AUTO_MATCHED" and canonical_place_id:
                resolution_status = "AUTO_MATCHED"
            elif stored_status == "UNRESOLVED":
                resolution_status = "UNRESOLVED"
            else:
                resolution_status = "NEEDS_CONFIRMATION"
            stops.append(
                MapStop(
                    day_index=day_index,
                    day_label=day.label,
                    sequence_index=sequence_index,
                    name=card.name,
                    canonical_place_id=canonical_place_id,
                    resolution_status=resolution_status,
                    city=city,
                    longitude=longitude,
                    latitude=latitude,
                )
            )
    stop_set_hash = canonical_sha256(
        [
            {
                "day_index": stop.day_index,
                "sequence_index": stop.sequence_index,
                "name": stop.name,
                "canonical_place_id": stop.canonical_place_id,
                "resolution_status": stop.resolution_status,
                "city": stop.city,
                "longitude": stop.longitude,
                "latitude": stop.latitude,
            }
            for stop in stops
        ]
    )
    return MapRenderPlan(
        understanding_id=understanding_id,
        plan_ref=PlanRevisionRef(
            kind="UNDERSTANDING",
            aggregate_id=understanding_id,
            revision=revision,
            stop_set_hash=stop_set_hash,
        ),
        route_config_hash=ROUTE_CONFIG_SHA256,
        stops=stops,
    )


def plan_with_stay_anchor(
    plan: MapRenderPlan,
    *,
    selected_place_id: str,
    selected_name: str,
    selected_city: str,
    longitude: float,
    latitude: float,
    overnight_days: list[int],
) -> MapRenderPlan:
    by_day: dict[int, list[MapStop]] = defaultdict(list)
    for stop in sorted(plan.stops, key=lambda item: (item.day_index, item.sequence_index)):
        by_day[stop.day_index].append(stop)
    expanded: list[MapStop] = []
    overnight = set(overnight_days)
    for day_index in sorted(by_day):
        day_stops = by_day[day_index]
        if day_index in overnight and day_stops:
            hotel = MapStop(
                day_index=day_index,
                day_label=day_stops[0].day_label,
                sequence_index=0,
                name=selected_name,
                canonical_place_id=selected_place_id,
                resolution_status="AUTO_MATCHED",
                city=selected_city,
                longitude=longitude,
                latitude=latitude,
            )
            expanded.append(hotel)
            expanded.extend(
                stop.model_copy(update={"sequence_index": index})
                for index, stop in enumerate(day_stops, start=1)
            )
            expanded.append(hotel.model_copy(update={"sequence_index": len(day_stops) + 1}))
        else:
            expanded.extend(
                stop.model_copy(update={"sequence_index": index})
                for index, stop in enumerate(day_stops)
            )
    stop_set_hash = canonical_sha256(
        [
            {
                "day_index": stop.day_index,
                "sequence_index": stop.sequence_index,
                "name": stop.name,
                "canonical_place_id": stop.canonical_place_id,
                "resolution_status": stop.resolution_status,
                "city": stop.city,
                "longitude": stop.longitude,
                "latitude": stop.latitude,
            }
            for stop in expanded
        ]
    )
    return plan.model_copy(
        update={
            "plan_ref": plan.plan_ref.model_copy(update={"stop_set_hash": stop_set_hash}),
            "stops": expanded,
        }
    )


def _logical_key(plan: MapRenderPlan) -> str:
    return canonical_sha256(
        {
            "understanding_id": plan.understanding_id,
            "revision_kind": plan.plan_ref.kind,
            "aggregate_id": plan.plan_ref.aggregate_id,
            "revision": plan.plan_ref.revision,
            "stop_set_hash": plan.plan_ref.stop_set_hash,
            "route_config_hash": plan.route_config_hash,
        }
    )


def _accepted_for_job_status(status: str) -> MapRenderAcceptedView:
    if status in {"QUEUED", "BUILDING"}:
        return MapRenderAcceptedView(status="PREPARING", message="路线正在后台准备")
    if status == "READY":
        return MapRenderAcceptedView(status="AVAILABLE", message="步行和公交路线已准备")
    if status == "PARTIAL":
        return MapRenderAcceptedView(status="LIMITED", message="部分路线暂不可用")
    return MapRenderAcceptedView(status="UNAVAILABLE", message="路线暂不可用，不影响查看卡片")


def _mode_view_from_row(
    row: Any | None,
    *,
    geometry: list[RouteGeometryPoint] | None = None,
) -> PublicRouteModeView:
    if row is None or row["status"] != "AVAILABLE":
        return PublicRouteModeView(status="UNAVAILABLE")
    return PublicRouteModeView(
        status="AVAILABLE",
        duration_minutes=row["duration_minutes"],
        distance_meters=row["distance_meters"],
        transfer_count=row["transfer_count"],
        geometry=geometry or [],
    )


def _edge_message(selected_mode: str | None, walking: Any | None, transit: Any | None) -> str:
    if selected_mode == "walking" and walking is not None:
        return f"建议步行约 {walking['duration_minutes']} 分钟"
    if selected_mode == "transit" and transit is not None:
        return f"建议公交约 {transit['duration_minutes']} 分钟"
    return "路线暂不可用"


class MapRenderRepository(Protocol):
    async def get_map_view(
        self,
        resource: PublicResourceRecord,
        *,
        now: datetime | None = None,
    ) -> MapRenderView: ...

    async def request_map_render(
        self,
        resource: PublicResourceRecord,
        *,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> MapRenderRequestOutcome: ...

    async def claim_next_map(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> MapRenderJobRecord | None: ...

    async def renew_map_lease(
        self,
        job: MapRenderJobRecord,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool: ...

    async def load_map_plan(self, job: MapRenderJobRecord) -> MapRenderPlan: ...

    async def complete_map_job(
        self,
        job: MapRenderJobRecord,
        output: MapRenderOutput,
        *,
        now: datetime,
    ) -> bool: ...

    async def fail_map_job(
        self,
        job: MapRenderJobRecord,
        *,
        category: str,
        now: datetime,
    ) -> None: ...


class PostgresMapRenderRepositoryMixin:
    def _get_geometry_cache(self):
        cache = getattr(self, "_geometry_cache", None)
        if cache is None:
            cache = InMemoryRouteGeometryCache()
            self._geometry_cache = cache
        return cache

    async def _mode_view_with_geometry(
        self,
        row: Any | None,
    ) -> tuple[PublicRouteModeView, bool]:
        if row is None or row["status"] != "AVAILABLE":
            return PublicRouteModeView(status="UNAVAILABLE"), False
        reference = row.get("geometry_ref") if hasattr(row, "get") else row["geometry_ref"]
        raw_points = await self._get_geometry_cache().get(reference) if reference else None
        points = [RouteGeometryPoint.model_validate(point) for point in (raw_points or [])]
        return _mode_view_from_row(row, geometry=points), len(points) < 2

    async def _read_map_plan(
        self,
        conn: Any,
        understanding_id: str,
        revision: int,
    ) -> MapRenderPlan:
        result_row = await conn.fetchrow(
            """
            SELECT result.public_json, revision.destination_json
            FROM trip_understanding_results result
            JOIN trip_understanding_revisions revision
              ON revision.understanding_id = result.understanding_id
             AND revision.revision = result.revision
            WHERE result.understanding_id = $1 AND result.revision = $2
            """,
            understanding_id,
            revision,
        )
        if result_row is None:
            raise ResourceNotReadyError("trip result is unavailable for map rendering")
        activities = await conn.fetch(
            """
            SELECT public_activity_token, canonical_place_id, resolution_status,
                   resolver_receipt_json
            FROM trip_understanding_activities
            WHERE understanding_id = $1 AND revision = $2 AND role = 'PLANNED'
            """,
            understanding_id,
            revision,
        )
        bindings = {
            row["public_activity_token"]: (
                row["canonical_place_id"],
                row["resolution_status"],
                _json_value(row["resolver_receipt_json"]),
            )
            for row in activities
        }
        destination = _json_value(result_row["destination_json"])
        city = destination.get("name") if isinstance(destination, dict) else None
        plan = _plan_for_result(
            understanding_id,
            revision,
            UserFacingTripResult.model_validate(_json_value(result_row["public_json"])),
            bindings,
            city=city if isinstance(city, str) else None,
        )
        selection = await conn.fetchrow(
            """
            SELECT s.* FROM trip_stay_selections s
            JOIN trip_plan_revision_refs p ON p.plan_ref_id = s.target_plan_ref_id
            WHERE p.understanding_id = $1 AND p.revision_kind = 'UNDERSTANDING'
              AND p.aggregate_id = $1 AND p.revision = $2
            """,
            understanding_id,
            revision,
        )
        if selection is None:
            return plan
        return plan_with_stay_anchor(
            plan,
            selected_place_id=selection["selected_place_id"],
            selected_name=selection["selected_name"],
            selected_city=selection["selected_city"],
            longitude=float(selection["longitude"]),
            latitude=float(selection["latitude"]),
            overnight_days=list(selection["overnight_days"]),
        )

    async def _ensure_map_job(
        self,
        conn: Any,
        understanding_id: str,
        revision: int,
        *,
        request_origin: str,
        now: datetime,
    ) -> Any:
        plan = await self._read_map_plan(conn, understanding_id, revision)
        plan_ref_id = str(uuid4())
        await conn.execute(
            """
            INSERT INTO trip_plan_revision_refs (
                plan_ref_id, understanding_id, revision_kind, aggregate_id,
                revision, stop_set_hash, created_at
            ) VALUES ($1, $2, 'UNDERSTANDING', $2, $3, $4, $5)
            ON CONFLICT (understanding_id, revision_kind, aggregate_id, revision)
            DO NOTHING
            """,
            plan_ref_id,
            understanding_id,
            revision,
            plan.plan_ref.stop_set_hash,
            now,
        )
        plan_ref = await conn.fetchrow(
            """
            SELECT plan_ref_id, stop_set_hash FROM trip_plan_revision_refs
            WHERE understanding_id = $1 AND revision_kind = 'UNDERSTANDING'
              AND aggregate_id = $1 AND revision = $2
            """,
            understanding_id,
            revision,
        )
        if plan_ref is None or plan_ref["stop_set_hash"].strip() != plan.plan_ref.stop_set_hash:
            raise IdempotencyConflictError("plan revision stop binding changed")
        map_job_id = str(uuid4())
        logical_key_hash = _logical_key(plan)
        await conn.execute(
            """
            INSERT INTO trip_map_render_jobs (
                map_job_id, plan_ref_id, understanding_id, route_config_hash,
                logical_key_hash, request_origin, status, available_at,
                created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, 'QUEUED', $7, $7, $7)
            ON CONFLICT (plan_ref_id, route_config_hash) DO NOTHING
            """,
            map_job_id,
            plan_ref["plan_ref_id"],
            understanding_id,
            plan.route_config_hash,
            logical_key_hash,
            request_origin,
            now,
        )
        job = await conn.fetchrow(
            """
            SELECT * FROM trip_map_render_jobs
            WHERE plan_ref_id = $1 AND route_config_hash = $2
            """,
            plan_ref["plan_ref_id"],
            plan.route_config_hash,
        )
        await conn.execute(
            """
            INSERT INTO trip_map_render_events (map_job_id, event_key, status, observed_at)
            VALUES ($1, 'queued', 'QUEUED', $2)
            ON CONFLICT (map_job_id, event_key) DO NOTHING
            """,
            job["map_job_id"],
            now,
        )
        return job

    async def _enqueue_initial_map_job(
        self,
        conn: Any,
        understanding_id: str,
        revision: int,
        *,
        now: datetime,
    ) -> None:
        await self._ensure_map_job(
            conn,
            understanding_id,
            revision,
            request_origin="INITIAL",
            now=now,
        )

    async def _snapshot_view(self, conn: Any, snapshot: Any) -> MapRenderView:
        edge_rows = await conn.fetch(
            """
            SELECT e.*, f.mode, f.status AS mode_status, f.duration_minutes,
                   f.distance_meters, f.transfer_count, f.geometry_ref
            FROM trip_map_route_edges e
            LEFT JOIN trip_map_route_mode_facts f ON f.edge_id = e.edge_id
            WHERE e.snapshot_id = $1
            ORDER BY e.day_index, e.sequence_index, f.mode
            """,
            snapshot["snapshot_id"],
        )
        grouped: dict[str, dict[str, Any]] = {}
        for row in edge_rows:
            item = grouped.setdefault(
                row["edge_id"],
                {
                    "day_index": row["day_index"],
                    "sequence_index": row["sequence_index"],
                    "origin_name": row["origin_name"],
                    "destination_name": row["destination_name"],
                    "selected_mode": row["selected_mode"],
                    "modes": {},
                },
            )
            if row["mode"] is not None:
                item["modes"][row["mode"]] = {
                    "status": row["mode_status"],
                    "duration_minutes": row["duration_minutes"],
                    "distance_meters": row["distance_meters"],
                    "transfer_count": row["transfer_count"],
                    "geometry_ref": row["geometry_ref"],
                }
        by_day: dict[int, list[PublicMapEdgeView]] = defaultdict(list)
        geometry_limited = False
        for item in sorted(
            grouped.values(), key=lambda value: (value["day_index"], value["sequence_index"])
        ):
            walking = item["modes"].get("walking")
            transit = item["modes"].get("transit")
            walking_view, walking_missing = await self._mode_view_with_geometry(walking)
            transit_view, transit_missing = await self._mode_view_with_geometry(transit)
            geometry_limited = geometry_limited or walking_missing or transit_missing
            by_day[item["day_index"]].append(
                PublicMapEdgeView(
                    from_name=item["origin_name"],
                    to_name=item["destination_name"],
                    selected_mode=item["selected_mode"],
                    message=_edge_message(item["selected_mode"], walking, transit),
                    walking=walking_view,
                    transit=transit_view,
                )
            )
        days = [
            PublicMapDayView(label=f"Day {day_index}", routes=by_day[day_index])
            for day_index in sorted(by_day)
        ]
        if snapshot["status"] == "READY" and not geometry_limited:
            return MapRenderView(
                status="AVAILABLE",
                message="步行和公交路线已准备，出发前请再核对实时情况",
                days=days,
                available_actions=["VIEW_MAP"],
            )
        if snapshot["status"] == "READY":
            return MapRenderView(
                status="LIMITED",
                message="路线摘要仍可查看，地图线条需要重新准备",
                days=days,
                available_actions=["VIEW_MAP", "RENDER_MAP"],
            )
        if snapshot["status"] == "PARTIAL":
            return MapRenderView(
                status="LIMITED",
                message="部分路线暂不可用，已准备的路线仍可查看",
                days=days,
                available_actions=["VIEW_MAP"],
            )
        return MapRenderView(
            status="UNAVAILABLE",
            message="路线暂不可用，不影响查看和调整卡片",
            available_actions=["RENDER_MAP"],
        )

    async def _project_map_view(
        self,
        conn: Any,
        understanding_id: str,
        current_revision: int,
        *,
        now: datetime,
    ) -> MapRenderView:
        current = await conn.fetchrow(
            """
            SELECT j.status AS job_status, s.*
            FROM trip_plan_revision_refs p
            JOIN trip_map_render_jobs j ON j.plan_ref_id = p.plan_ref_id
            LEFT JOIN trip_map_render_snapshots s ON s.map_job_id = j.map_job_id
            WHERE p.understanding_id = $1 AND p.revision_kind = 'UNDERSTANDING'
              AND p.aggregate_id = $1 AND p.revision = $2
              AND j.route_config_hash = $3
            ORDER BY j.created_at DESC LIMIT 1
            """,
            understanding_id,
            current_revision,
            ROUTE_CONFIG_SHA256,
        )
        if current is not None:
            if current["job_status"] in {"QUEUED", "BUILDING"}:
                return MapRenderView(
                    status="PREPARING",
                    message="路线正在后台准备，卡片可以先查看和调整",
                )
            if current["snapshot_id"] is not None:
                return await self._snapshot_view(conn, current)
        older_exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM trip_map_render_jobs WHERE understanding_id = $1)",
            understanding_id,
        )
        if older_exists:
            return MapRenderView(
                status="NEEDS_UPDATE",
                message="行程已修改，路线尚未更新",
                available_actions=["RENDER_MAP"],
            )
        return MapRenderView(
            status="UNAVAILABLE",
            message="路线暂不可用，不影响查看和调整卡片",
            available_actions=["RENDER_MAP"],
        )

    async def _project_map_readiness(
        self,
        conn: Any,
        understanding_id: str,
        revision: int,
        *,
        now: datetime | None = None,
    ) -> MapReadinessView:
        view = await self._project_map_view(
            conn,
            understanding_id,
            revision,
            now=now or datetime.now(timezone.utc),
        )
        return view.readiness()

    async def get_map_view(
        self,
        resource: PublicResourceRecord,
        *,
        now: datetime | None = None,
    ) -> MapRenderView:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            aggregate = await conn.fetchrow(
                "SELECT state, current_revision FROM trip_understandings WHERE understanding_id = $1",
                resource.understanding_id,
            )
            if aggregate is None:
                raise ResourceNotFoundError("trip resource does not exist")
            if aggregate["state"] == "DELETED":
                raise ResourceGoneError("trip resource is no longer available")
            return await self._project_map_view(
                conn,
                resource.understanding_id,
                int(aggregate["current_revision"]),
                now=now or datetime.now(timezone.utc),
            )

    async def request_map_render(
        self,
        resource: PublicResourceRecord,
        *,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> MapRenderRequestOutcome:
        if len(idempotency_key) > 200:
            raise ValueError("idempotency key is too long")
        scope = f"understanding:{resource.understanding_id}:map-renders"
        key_hash = _sha256_text(idempotency_key)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            aggregate = await conn.fetchrow(
                """
                SELECT u.public_resource_id, u.state, u.current_revision, r.opaque_etag
                FROM trip_understandings u
                LEFT JOIN trip_understanding_results r ON r.result_id = u.current_result_id
                WHERE u.understanding_id = $1 FOR UPDATE OF u
                """,
                resource.understanding_id,
            )
            if aggregate is None:
                raise ResourceNotFoundError("trip resource does not exist")
            if aggregate["state"] == "DELETED":
                raise ResourceGoneError("trip resource is no longer available")
            if aggregate["public_resource_id"] != resource.public_resource_id:
                raise ResourceAccessDeniedError("trip resource binding changed")
            if aggregate["opaque_etag"] is None:
                raise ResourceNotReadyError("trip cards are not ready for map rendering")
            claimed = await conn.fetchval(
                """
                INSERT INTO trip_understanding_idempotency_records (
                    scope, key_hash, request_hash, state, lease_until, created_at
                ) VALUES ($1, $2, $3, 'IN_PROGRESS', $4, $5)
                ON CONFLICT (scope, key_hash) DO NOTHING RETURNING scope
                """,
                scope,
                key_hash,
                request_hash,
                now + timedelta(seconds=30),
                now,
            )
            if claimed is None:
                existing = await conn.fetchrow(
                    """
                    SELECT request_hash, state, response_json
                    FROM trip_understanding_idempotency_records
                    WHERE scope = $1 AND key_hash = $2
                    """,
                    scope,
                    key_hash,
                )
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyConflictError("map render idempotency key was reused")
                if existing["state"] != "COMPLETED":
                    raise IdempotencyInProgressError("map render request is still in progress")
                return MapRenderRequestOutcome(
                    accepted=MapRenderAcceptedView.model_validate(
                        _json_value(existing["response_json"])
                    ),
                    replayed=True,
                )
            if not hmac.compare_digest(aggregate["opaque_etag"], expected_etag):
                raise RevisionConflictError("map render precondition does not match current result")
            job = await self._ensure_map_job(
                conn,
                resource.understanding_id,
                int(aggregate["current_revision"]),
                request_origin="MANUAL",
                now=now,
            )
            accepted = _accepted_for_job_status(job["status"])
            await conn.execute(
                """
                UPDATE trip_understanding_idempotency_records
                SET state = 'COMPLETED', response_status = 202,
                    response_json = $3::jsonb, response_headers_json = '{}'::jsonb,
                    lease_until = NULL, completed_at = $4
                WHERE scope = $1 AND key_hash = $2
                """,
                scope,
                key_hash,
                json.dumps(accepted.model_dump(mode="json"), ensure_ascii=False),
                now,
            )
        return MapRenderRequestOutcome(accepted=accepted)

    async def claim_next_map(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> MapRenderJobRecord | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                WITH candidate AS (
                    SELECT map_job_id FROM trip_map_render_jobs
                    WHERE available_at <= $1
                      AND (
                        status = 'QUEUED'
                        OR (status = 'BUILDING' AND lease_until <= $1)
                      )
                      AND attempt < max_attempts
                    ORDER BY available_at, created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE trip_map_render_jobs j
                SET status = 'BUILDING', lease_owner = $2, lease_until = $3,
                    attempt = j.attempt + 1, started_at = COALESCE(j.started_at, $1),
                    updated_at = $1
                FROM candidate c
                WHERE j.map_job_id = c.map_job_id
                RETURNING j.*
                """,
                now,
                worker_id,
                now + timedelta(seconds=lease_seconds),
            )
            if row is None:
                return None
            plan_ref = await conn.fetchrow(
                "SELECT * FROM trip_plan_revision_refs WHERE plan_ref_id = $1",
                row["plan_ref_id"],
            )
            await conn.execute(
                """
                INSERT INTO trip_map_render_events (map_job_id, event_key, status, observed_at)
                VALUES ($1, $2, 'BUILDING', $3)
                ON CONFLICT (map_job_id, event_key) DO NOTHING
                """,
                row["map_job_id"],
                f"building:{row['attempt']}",
                now,
            )
        return MapRenderJobRecord(
            map_job_id=row["map_job_id"],
            understanding_id=row["understanding_id"],
            plan_ref_id=row["plan_ref_id"],
            plan_ref=PlanRevisionRef(
                kind=plan_ref["revision_kind"],
                aggregate_id=plan_ref["aggregate_id"],
                revision=plan_ref["revision"],
                stop_set_hash=plan_ref["stop_set_hash"].strip(),
            ),
            route_config_hash=row["route_config_hash"].strip(),
            status="BUILDING",
            lease_owner=row["lease_owner"],
            lease_until=row["lease_until"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            started_at=row["started_at"],
        )

    async def load_map_plan(self, job: MapRenderJobRecord) -> MapRenderPlan:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            plan = await self._read_map_plan(
                conn,
                job.understanding_id,
                job.plan_ref.revision,
            )
        if plan.plan_ref != job.plan_ref or plan.route_config_hash != job.route_config_hash:
            raise ValueError("map job plan binding changed")
        return plan

    async def renew_map_lease(
        self,
        job: MapRenderJobRecord,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            renewed = await conn.fetchval(
                """
                UPDATE trip_map_render_jobs
                SET lease_until = $4, updated_at = $3
                WHERE map_job_id = $1 AND status = 'BUILDING'
                  AND lease_owner = $2 AND attempt = $5 AND lease_until > $3
                RETURNING lease_until
                """,
                job.map_job_id,
                job.lease_owner,
                now,
                now + timedelta(seconds=lease_seconds),
                job.attempt,
            )
        return renewed is not None

    async def complete_map_job(
        self,
        job: MapRenderJobRecord,
        output: MapRenderOutput,
        *,
        now: datetime,
    ) -> bool:
        if output.plan_ref != job.plan_ref or output.route_config_hash != job.route_config_hash:
            raise ValueError("map output is not bound to the claimed plan")
        geometry_cache = self._get_geometry_cache()
        for edge in output.edges:
            for fact in (edge.walking, edge.transit):
                if fact.geometry:
                    fact.geometry_ref = await geometry_cache.put(
                        [point.model_dump(mode="json") for point in fact.geometry]
                    )
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow(
                "SELECT * FROM trip_map_render_jobs WHERE map_job_id = $1 FOR UPDATE",
                job.map_job_id,
            )
            if current is None:
                raise ResourceNotFoundError("map job does not exist")
            existing = await conn.fetchrow(
                "SELECT snapshot_sha256 FROM trip_map_render_snapshots WHERE map_job_id = $1",
                job.map_job_id,
            )
            if existing is not None:
                if existing["snapshot_sha256"].strip() != output.snapshot_sha256:
                    raise IdempotencyConflictError("map snapshot binding mismatch")
                return True
            if (
                current["status"] != "BUILDING"
                or current["lease_owner"] != job.lease_owner
                or current["lease_until"] <= now
            ):
                raise JobLeaseLostError("map job lease was lost before completion")
            snapshot_id = str(uuid4())
            available_count = sum(edge.available for edge in output.edges)
            await conn.execute(
                """
                INSERT INTO trip_map_render_snapshots (
                    snapshot_id, map_job_id, plan_ref_id, status, stop_count,
                    edge_count, available_edge_count, snapshot_sha256,
                    provider_binding_json, failure_json, started_at, finished_at,
                    observed_at, expires_at, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb,
                    $11, $12, $13, $14, $15
                )
                """,
                snapshot_id,
                job.map_job_id,
                job.plan_ref_id,
                output.status,
                output.stop_count,
                len(output.edges),
                available_count,
                output.snapshot_sha256,
                json.dumps(output.provider_binding, ensure_ascii=False),
                json.dumps(output.failure, ensure_ascii=False),
                output.started_at,
                output.finished_at,
                output.observed_at,
                output.expires_at,
                now,
            )
            for edge in output.edges:
                edge_id = str(uuid4())
                await conn.execute(
                    """
                    INSERT INTO trip_map_route_edges (
                        edge_id, snapshot_id, day_index, sequence_index,
                        origin_name, destination_name, selected_mode, status,
                        unavailable_reason, created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    edge_id,
                    snapshot_id,
                    edge.day_index,
                    edge.sequence_index,
                    edge.origin_name,
                    edge.destination_name,
                    edge.selected_mode,
                    "AVAILABLE" if edge.available else "UNAVAILABLE",
                    edge.unavailable_reason,
                    now,
                )
                for fact in (edge.walking, edge.transit):
                    await conn.execute(
                        """
                        INSERT INTO trip_map_route_mode_facts (
                            edge_id, mode, status, duration_minutes, distance_meters,
                            transfer_count, response_hash, geometry_ref,
                            provider_receipt_json, observed_at, expires_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
                        """,
                        edge_id,
                        fact.mode,
                        fact.status,
                        fact.duration_minutes,
                        fact.distance_meters,
                        fact.transfer_count,
                        fact.response_hash,
                        fact.geometry_ref,
                        json.dumps(fact.provider_binding, ensure_ascii=False),
                        fact.observed_at,
                        fact.expires_at,
                    )
                    effect_key = (
                        f"map:{_logical_key(MapRenderPlan(understanding_id=job.understanding_id, plan_ref=job.plan_ref, route_config_hash=job.route_config_hash, stops=[]))}:"
                        f"d{edge.day_index}:e{edge.sequence_index}:{fact.mode}"
                    )
                    await conn.execute(
                        """
                        INSERT INTO trip_map_provider_effect_receipts (
                            receipt_id, map_job_id, effect_key, mode, request_hash,
                            response_hash, provider_binding_json, external_call_count,
                            created_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
                        """,
                        str(uuid4()),
                        job.map_job_id,
                        effect_key,
                        fact.mode,
                        fact.request_hash,
                        fact.response_hash,
                        json.dumps(fact.provider_binding, ensure_ascii=False),
                        fact.external_call_count,
                        now,
                    )
            await conn.execute(
                """
                UPDATE trip_map_render_jobs
                SET status = $2, lease_owner = NULL, lease_until = NULL,
                    finished_at = $3, updated_at = $3
                WHERE map_job_id = $1
                """,
                job.map_job_id,
                output.status,
                now,
            )
            await conn.execute(
                """
                INSERT INTO trip_map_render_events (map_job_id, event_key, status, observed_at)
                VALUES ($1, 'terminal', $2, $3)
                ON CONFLICT (map_job_id, event_key) DO NOTHING
                """,
                job.map_job_id,
                output.status,
                now,
            )
        return False

    async def fail_map_job(
        self,
        job: MapRenderJobRecord,
        *,
        category: str,
        now: datetime,
    ) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT status, lease_owner, attempt, max_attempts
                FROM trip_map_render_jobs WHERE map_job_id = $1 FOR UPDATE
                """,
                job.map_job_id,
            )
            if row is None or row["status"] != "BUILDING" or row["lease_owner"] != job.lease_owner:
                return
            retryable = row["attempt"] < row["max_attempts"]
            if retryable:
                await conn.execute(
                    """
                    UPDATE trip_map_render_jobs
                    SET status = 'QUEUED', lease_owner = NULL, lease_until = NULL,
                        last_error_category = $2, available_at = $3, updated_at = $4
                    WHERE map_job_id = $1
                    """,
                    job.map_job_id,
                    category,
                    now + timedelta(seconds=2),
                    now,
                )
                return
            snapshot_hash = canonical_sha256(
                {
                    "plan_ref": job.plan_ref.model_dump(mode="json"),
                    "route_config_hash": job.route_config_hash,
                    "status": "UNAVAILABLE",
                    "failure_category": category,
                }
            )
            await conn.execute(
                """
                INSERT INTO trip_map_render_snapshots (
                    snapshot_id, map_job_id, plan_ref_id, status, stop_count,
                    edge_count, available_edge_count, snapshot_sha256,
                    provider_binding_json, failure_json, started_at, finished_at,
                    observed_at, expires_at, created_at
                ) VALUES ($1, $2, $3, 'UNAVAILABLE', 0, 0, 0, $4,
                    $5::jsonb, $6::jsonb, $7, $8, $8, $9, $8)
                ON CONFLICT (map_job_id) DO NOTHING
                """,
                str(uuid4()),
                job.map_job_id,
                job.plan_ref_id,
                snapshot_hash,
                json.dumps({"execution_mode": "controlled_fixture", "external_calls": 0}),
                json.dumps({"category": category}),
                job.started_at,
                now,
                now + timedelta(hours=24),
            )
            await conn.execute(
                """
                UPDATE trip_map_render_jobs
                SET status = 'UNAVAILABLE', lease_owner = NULL, lease_until = NULL,
                    last_error_category = $2, finished_at = $3, updated_at = $3
                WHERE map_job_id = $1
                """,
                job.map_job_id,
                category,
                now,
            )
            await conn.execute(
                """
                INSERT INTO trip_map_render_events (map_job_id, event_key, status, observed_at)
                VALUES ($1, 'terminal', 'UNAVAILABLE', $2)
                ON CONFLICT (map_job_id, event_key) DO NOTHING
                """,
                job.map_job_id,
                now,
            )


class InMemoryMapRenderRepositoryMixin:
    def _init_map_store(self) -> None:
        self.map_jobs: dict[str, dict[str, Any]] = {}
        self.map_jobs_by_logical_key: dict[str, str] = {}
        self.map_snapshots: dict[str, MapRenderOutput] = {}
        self.map_request_idempotency: dict[tuple[str, str], tuple[str, MapRenderAcceptedView]] = {}
        self.map_provider_effects: set[str] = set()

    def _memory_plan(self, understanding_id: str, revision: int) -> MapRenderPlan:
        result_id = next(
            (
                candidate_id
                for candidate_id, owner in self.result_owners.items()
                if owner == understanding_id
                and self.result_revisions.get(candidate_id) == revision
            ),
            None,
        )
        stored = self.results.get(result_id or "")
        if stored is None:
            raise ResourceNotReadyError("trip result is unavailable for map rendering")
        destination = next(
            (
                assumption.value.removeprefix("暂按 ")
                for assumption in stored.result.assumptions
                if assumption.key == "destination"
            ),
            None,
        )
        bindings: dict[str, tuple[str | None, str, dict[str, Any]]] = {}
        for day in stored.result.days:
            for card in day.activities:
                if card.status == "READY":
                    coordinates = _controlled_coordinates(card.name, destination)
                    bindings[card.activity_token] = (
                        f"fixture:{card.name}",
                        "AUTO_MATCHED",
                        {"coordinates": coordinates} if coordinates else {},
                    )
                else:
                    bindings[card.activity_token] = (None, "NEEDS_CONFIRMATION", {})
        plan = _plan_for_result(
            understanding_id,
            revision,
            stored.result,
            bindings,
            city=destination,
        )
        selection = getattr(self, "stay_selections", {}).get((understanding_id, revision))
        if selection is None:
            return plan
        view = selection["view"]
        return plan_with_stay_anchor(
            plan,
            selected_place_id=selection["selected_place_id"],
            selected_name=view.name,
            selected_city=selection["selected_city"],
            longitude=selection["longitude"],
            latitude=selection["latitude"],
            overnight_days=selection["overnight_days"],
        )

    def _ensure_memory_map_job(
        self,
        understanding_id: str,
        revision: int,
        *,
        request_origin: str,
        now: datetime,
    ) -> dict[str, Any]:
        plan = self._memory_plan(understanding_id, revision)
        logical_key = _logical_key(plan)
        existing_id = self.map_jobs_by_logical_key.get(logical_key)
        if existing_id is not None:
            return self.map_jobs[existing_id]
        map_job_id = str(uuid4())
        item = {
            "map_job_id": map_job_id,
            "understanding_id": understanding_id,
            "plan_ref_id": str(uuid4()),
            "plan": plan,
            "route_config_hash": plan.route_config_hash,
            "request_origin": request_origin,
            "status": "QUEUED",
            "lease_owner": None,
            "lease_until": None,
            "attempt": 0,
            "max_attempts": 3,
            "available_at": now,
            "started_at": None,
        }
        self.map_jobs[map_job_id] = item
        self.map_jobs_by_logical_key[logical_key] = map_job_id
        return item

    def _enqueue_initial_map_job_memory(
        self,
        understanding_id: str,
        revision: int,
        *,
        now: datetime,
    ) -> None:
        self._ensure_memory_map_job(
            understanding_id,
            revision,
            request_origin="INITIAL",
            now=now,
        )

    def _memory_snapshot_view(self, output: MapRenderOutput) -> MapRenderView:
        by_day: dict[int, list[PublicMapEdgeView]] = defaultdict(list)
        for edge in output.edges:
            walking = {
                "status": edge.walking.status,
                "duration_minutes": edge.walking.duration_minutes,
                "distance_meters": edge.walking.distance_meters,
                "transfer_count": edge.walking.transfer_count,
            }
            transit = {
                "status": edge.transit.status,
                "duration_minutes": edge.transit.duration_minutes,
                "distance_meters": edge.transit.distance_meters,
                "transfer_count": edge.transit.transfer_count,
            }
            by_day[edge.day_index].append(
                PublicMapEdgeView(
                    from_name=edge.origin_name,
                    to_name=edge.destination_name,
                    selected_mode=edge.selected_mode,
                    message=_edge_message(edge.selected_mode, walking, transit),
                    walking=_mode_view_from_row(walking, geometry=edge.walking.geometry),
                    transit=_mode_view_from_row(transit, geometry=edge.transit.geometry),
                )
            )
        days = [
            PublicMapDayView(label=f"Day {day_index}", routes=by_day[day_index])
            for day_index in sorted(by_day)
        ]
        if output.status == "READY":
            return MapRenderView(
                status="AVAILABLE",
                message="步行和公交路线已准备，出发前请再核对实时情况",
                days=days,
                available_actions=["VIEW_MAP"],
            )
        if output.status == "PARTIAL":
            return MapRenderView(
                status="LIMITED",
                message="部分路线暂不可用，已准备的路线仍可查看",
                days=days,
                available_actions=["VIEW_MAP"],
            )
        return MapRenderView(
            status="UNAVAILABLE",
            message="路线暂不可用，不影响查看和调整卡片",
            available_actions=["RENDER_MAP"],
        )

    def _memory_map_view(
        self,
        understanding_id: str,
        revision: int,
        *,
        now: datetime,
    ) -> MapRenderView:
        matching = [
            item
            for item in self.map_jobs.values()
            if item["understanding_id"] == understanding_id
            and item["plan"].plan_ref.revision == revision
            and item["route_config_hash"] == ROUTE_CONFIG_SHA256
        ]
        if matching:
            job = matching[-1]
            if job["status"] in {"QUEUED", "BUILDING"}:
                return MapRenderView(
                    status="PREPARING",
                    message="路线正在后台准备，卡片可以先查看和调整",
                )
            output = self.map_snapshots.get(job["map_job_id"])
            if output is not None:
                return self._memory_snapshot_view(output)
        if any(
            item["understanding_id"] == understanding_id for item in self.map_jobs.values()
        ):
            return MapRenderView(
                status="NEEDS_UPDATE",
                message="行程已修改，路线尚未更新",
                available_actions=["RENDER_MAP"],
            )
        return MapRenderView(
            status="UNAVAILABLE",
            message="路线暂不可用，不影响查看和调整卡片",
            available_actions=["RENDER_MAP"],
        )

    def _project_map_readiness_memory(
        self,
        understanding_id: str,
        revision: int,
        *,
        now: datetime | None = None,
    ) -> MapReadinessView:
        return self._memory_map_view(
            understanding_id,
            revision,
            now=now or datetime.now(timezone.utc),
        ).readiness()

    async def get_map_view(
        self,
        resource: PublicResourceRecord,
        *,
        now: datetime | None = None,
    ) -> MapRenderView:
        public_id = self.resources_by_understanding.get(resource.understanding_id)
        if public_id is None:
            raise ResourceNotFoundError("trip resource does not exist")
        aggregate = self.resources[public_id]
        return self._memory_map_view(
            resource.understanding_id,
            int(aggregate["current_revision"]),
            now=now or datetime.now(timezone.utc),
        )

    async def request_map_render(
        self,
        resource: PublicResourceRecord,
        *,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> MapRenderRequestOutcome:
        public_id = self.resources_by_understanding[resource.understanding_id]
        if public_id != resource.public_resource_id:
            raise ResourceAccessDeniedError("trip resource binding changed")
        scope = f"understanding:{resource.understanding_id}:map-renders"
        key = (scope, _sha256_text(idempotency_key))
        existing = self.map_request_idempotency.get(key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyConflictError("map render idempotency key was reused")
            return MapRenderRequestOutcome(accepted=existing[1], replayed=True)
        aggregate = self.resources[public_id]
        stored = self.results.get(aggregate["current_result_id"] or "")
        if stored is None:
            raise ResourceNotReadyError("trip cards are not ready for map rendering")
        if not hmac.compare_digest(stored.opaque_etag, expected_etag):
            raise RevisionConflictError("map render precondition does not match current result")
        job = self._ensure_memory_map_job(
            resource.understanding_id,
            int(aggregate["current_revision"]),
            request_origin="MANUAL",
            now=now,
        )
        accepted = _accepted_for_job_status(job["status"])
        self.map_request_idempotency[key] = (request_hash, accepted)
        return MapRenderRequestOutcome(accepted=accepted)

    async def claim_next_map(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> MapRenderJobRecord | None:
        eligible = [
            item
            for item in self.map_jobs.values()
            if item["available_at"] <= now
            and (
                item["status"] == "QUEUED"
                or (
                    item["status"] == "BUILDING"
                    and item["lease_until"] is not None
                    and item["lease_until"] <= now
                )
            )
            and item["attempt"] < item["max_attempts"]
        ]
        if not eligible:
            return None
        item = sorted(eligible, key=lambda row: (row["available_at"], row["map_job_id"]))[0]
        item.update(
            {
                "status": "BUILDING",
                "lease_owner": worker_id,
                "lease_until": now + timedelta(seconds=lease_seconds),
                "attempt": item["attempt"] + 1,
                "started_at": item["started_at"] or now,
            }
        )
        return MapRenderJobRecord(
            map_job_id=item["map_job_id"],
            understanding_id=item["understanding_id"],
            plan_ref_id=item["plan_ref_id"],
            plan_ref=item["plan"].plan_ref,
            route_config_hash=item["route_config_hash"],
            status="BUILDING",
            lease_owner=worker_id,
            lease_until=item["lease_until"],
            attempt=item["attempt"],
            max_attempts=item["max_attempts"],
            started_at=item["started_at"],
        )

    async def load_map_plan(self, job: MapRenderJobRecord) -> MapRenderPlan:
        item = self.map_jobs.get(job.map_job_id)
        if item is None or item["plan"].plan_ref != job.plan_ref:
            raise ResourceNotFoundError("map plan does not exist")
        return item["plan"]

    async def renew_map_lease(
        self,
        job: MapRenderJobRecord,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        item = self.map_jobs.get(job.map_job_id)
        if (
            item is None
            or item["status"] != "BUILDING"
            or item["lease_owner"] != job.lease_owner
            or item["attempt"] != job.attempt
            or item["lease_until"] is None
            or item["lease_until"] <= now
        ):
            return False
        item["lease_until"] = now + timedelta(seconds=lease_seconds)
        return True

    async def complete_map_job(
        self,
        job: MapRenderJobRecord,
        output: MapRenderOutput,
        *,
        now: datetime,
    ) -> bool:
        item = self.map_jobs.get(job.map_job_id)
        if item is None:
            raise ResourceNotFoundError("map job does not exist")
        existing = self.map_snapshots.get(job.map_job_id)
        if existing is not None:
            if existing.snapshot_sha256 != output.snapshot_sha256:
                raise IdempotencyConflictError("map snapshot binding mismatch")
            return True
        if (
            item["status"] != "BUILDING"
            or item["lease_owner"] != job.lease_owner
            or item["lease_until"] <= now
        ):
            raise JobLeaseLostError("map job lease was lost before completion")
        if output.plan_ref != job.plan_ref or output.route_config_hash != job.route_config_hash:
            raise ValueError("map output is not bound to the claimed plan")
        self.map_snapshots[job.map_job_id] = output
        logical_key = _logical_key(item["plan"])
        for edge in output.edges:
            for fact in (edge.walking, edge.transit):
                self.map_provider_effects.add(
                    f"map:{logical_key}:d{edge.day_index}:e{edge.sequence_index}:{fact.mode}"
                )
        item.update(
            {
                "status": output.status,
                "lease_owner": None,
                "lease_until": None,
                "finished_at": now,
            }
        )
        return False

    async def fail_map_job(
        self,
        job: MapRenderJobRecord,
        *,
        category: str,
        now: datetime,
    ) -> None:
        item = self.map_jobs.get(job.map_job_id)
        if item is None or item["status"] != "BUILDING" or item["lease_owner"] != job.lease_owner:
            return
        if item["attempt"] < item["max_attempts"]:
            item.update(
                {
                    "status": "QUEUED",
                    "lease_owner": None,
                    "lease_until": None,
                    "available_at": now + timedelta(seconds=2),
                    "last_error_category": category,
                }
            )
            return
        plan = item["plan"]
        output = MapRenderOutput(
            plan_ref=plan.plan_ref,
            route_config_hash=plan.route_config_hash,
            status="UNAVAILABLE",
            stop_count=len(plan.stops),
            edges=[],
            snapshot_sha256=canonical_sha256(
                {
                    "plan_ref": plan.plan_ref.model_dump(mode="json"),
                    "status": "UNAVAILABLE",
                    "category": category,
                }
            ),
            provider_binding={"execution_mode": "controlled_fixture", "external_calls": 0},
            failure={"category": category},
            started_at=item["started_at"],
            finished_at=now,
            observed_at=now,
            expires_at=now + timedelta(hours=24),
        )
        self.map_snapshots[job.map_job_id] = output
        item.update(
            {
                "status": "UNAVAILABLE",
                "lease_owner": None,
                "lease_until": None,
                "last_error_category": category,
                "finished_at": now,
            }
        )

    def _delete_map_memory(self, understanding_id: str) -> None:
        removed_ids = [
            job_id
            for job_id, item in self.map_jobs.items()
            if item["understanding_id"] == understanding_id
        ]
        for job_id in removed_ids:
            plan = self.map_jobs[job_id]["plan"]
            self.map_jobs_by_logical_key.pop(_logical_key(plan), None)
            self.map_jobs.pop(job_id, None)
            self.map_snapshots.pop(job_id, None)
        prefix = f"understanding:{understanding_id}:map-renders"
        for key in list(self.map_request_idempotency):
            if key[0] == prefix:
                self.map_request_idempotency.pop(key, None)
        active_keys = {
            _logical_key(item["plan"])
            for item in self.map_jobs.values()
        }
        self.map_provider_effects = {
            effect
            for effect in self.map_provider_effects
            if any(effect.startswith(f"map:{key}:") for key in active_keys)
        }

    @property
    def map_job_count(self) -> int:
        return len(self.map_jobs)

    @property
    def map_provider_effect_count(self) -> int:
        return len(self.map_provider_effects)
