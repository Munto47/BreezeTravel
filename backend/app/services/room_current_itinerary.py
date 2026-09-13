"""Member-authorized shared relative routes; external work happens outside transactions."""
from __future__ import annotations

import json
from dataclasses import dataclass

from fastapi import HTTPException

from app.db.connection import get_pool
from app.schemas.api import (
    ExperiencePlaceView, OptimizeRequest, OptimizeResponse,
    RoomCurrentItineraryView, RoomRouteSelectionSnapshot,
)
from app.schemas.itinerary import Itinerary


def version_conflict() -> HTTPException:
    return HTTPException(409, detail={"code": "ROOM_ROUTE_VERSION_CONFLICT",
        "message": "房间路线已更新，请查看最新路线后再操作"})


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _place(value: dict) -> dict:
    return ExperiencePlaceView.model_validate(value).model_dump(mode="json",
        exclude={"opening_hours", "estimated_duration", "tips"}, exclude_none=True)


def selection_snapshot(request: OptimizeRequest) -> dict:
    return {"trip_days": request.trip_days, "place_ids": [p.place_id for p in request.places],
        "places": [_place(p.model_dump(mode="json")) for p in request.places]}


def _relative_itinerary(itinerary: Itinerary) -> dict:
    data = itinerary.model_dump(mode="json", exclude_none=True)
    for day in data["days"]:
        day.pop("date", None)
        day.pop("weather_summary", None)
        for slot in day["slots"]:
            slot.pop("start_time", None)
            slot.pop("end_time", None)
            slot["place"] = _place(slot["place"])
            slot["tips"] = []
    return data


@dataclass(frozen=True)
class PublishedRoomRoute:
    view: RoomCurrentItineraryView
    total_distance_km: float | None
    duration_ms: int

    def optimize_response(self) -> OptimizeResponse:
        return OptimizeResponse(itinerary=self.view.itinerary_data,
            total_distance_km=self.total_distance_km, duration_ms=self.duration_ms,
            optimization_method="relative_geographic_order", backup_pool=[],
            tips_status="NOT_REQUESTED", room_route_version=self.view.version)


def _published(row) -> PublishedRoomRoute:
    return PublishedRoomRoute(RoomCurrentItineraryView(room_id=row["room_id"], version=row["version"],
        itinerary_data=Itinerary.model_validate(_json(row["itinerary_data"])),
        selection_snapshot=RoomRouteSelectionSnapshot.model_validate(_json(row["selection_snapshot"])),
        published_at=row["published_at"].isoformat()), row["total_distance_km"], row["duration_ms"])


async def _authorized_head(conn, room_id: str, user_id: str, *, thread_id: str | None = None):
    row = await conn.fetchrow("""
        SELECT r.thread_id, latest.* FROM rooms r
        JOIN room_members m ON m.room_id=r.room_id AND m.user_id=$2
        LEFT JOIN LATERAL (SELECT * FROM room_itinerary_revisions
            WHERE room_id=r.room_id ORDER BY version DESC LIMIT 1) latest ON TRUE
        WHERE r.room_id=$1
        """, room_id, user_id)
    if row is None or (thread_id is not None and row["thread_id"] != thread_id):
        raise HTTPException(403, detail="不是该房间成员")
    return row


async def get_current_itinerary(room_id: str, user_id: str, *, expected_version: int | None = None,
                                pool=None) -> RoomCurrentItineraryView:
    pool = pool or await get_pool()
    async with pool.acquire() as conn:
        row = await _authorized_head(conn, room_id, user_id)
    version = row["version"] or 0
    if expected_version is not None and version != expected_version:
        raise version_conflict()
    return _published(row).view if version else RoomCurrentItineraryView(room_id=room_id, version=0)


async def _existing_or_check(conn, request: OptimizeRequest, user_id: str, head):
    existing = await conn.fetchrow("""SELECT * FROM room_itinerary_revisions
        WHERE room_id=$1 AND published_by_user_id=$2 AND request_id=$3""",
        request.room_id, user_id, request.room_route_request_id)
    current = head["version"] or 0
    if existing is not None:
        if (existing["version"] != current or existing["version"] != request.base_room_route_version + 1
                or _json(existing["selection_snapshot"]) != selection_snapshot(request)):
            raise version_conflict()
        return _published(existing)
    if current != request.base_room_route_version:
        raise version_conflict()
    return None


async def check_publication(request: OptimizeRequest, user_id: str, *, pool=None) -> PublishedRoomRoute | None:
    """Reserve a click, reject stale requests, or replay completion before route HTTP."""
    pool = pool or await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"trip-understanding-user:{user_id}")
            head = await _authorized_head(conn, request.room_id, user_id, thread_id=request.thread_id)
            replay = await _existing_or_check(conn, request, user_id, head)
            if replay is not None:
                return replay
            inserted = await conn.fetchval("""INSERT INTO room_itinerary_requests
                (room_id,user_id,request_id,base_version,selection_snapshot,status)
                VALUES($1,$2,$3,$4,$5::jsonb,'RUNNING') ON CONFLICT DO NOTHING RETURNING request_id""",
                request.room_id, user_id, request.room_route_request_id, request.base_room_route_version,
                json.dumps(selection_snapshot(request), ensure_ascii=False))
            if inserted is None:
                # The first request may have committed while INSERT waited on its reservation.
                head = await _authorized_head(conn, request.room_id, user_id, thread_id=request.thread_id)
                replay = await _existing_or_check(conn, request, user_id, head)
                if replay is not None:
                    return replay
                attempt = await conn.fetchrow("""SELECT * FROM room_itinerary_requests
                    WHERE room_id=$1 AND user_id=$2 AND request_id=$3""",
                    request.room_id, user_id, request.room_route_request_id)
                if (attempt["base_version"] != request.base_room_route_version
                        or _json(attempt["selection_snapshot"]) != selection_snapshot(request)):
                    raise version_conflict()
                code = "ROOM_ROUTE_REQUEST_IN_PROGRESS" if attempt["status"] == "RUNNING" else "ROOM_ROUTE_REQUEST_FAILED"
                raise HTTPException(409, detail={"code": code,
                    "message": "正在排线，请查看房间当前路线" if attempt["status"] == "RUNNING" else "上次排线未完成，请重新发起排线"})
    return None


async def fail_publication(request: OptimizeRequest, user_id: str, *, pool=None) -> None:
    pool = pool or await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""UPDATE room_itinerary_requests SET status='FAILED'
            WHERE room_id=$1 AND user_id=$2 AND request_id=$3 AND status='RUNNING'""",
            request.room_id, user_id, request.room_route_request_id)


async def publish_itinerary(request: OptimizeRequest, user_id: str, result: OptimizeResponse,
                            *, pool=None) -> PublishedRoomRoute:
    pool = pool or await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Same short privacy boundary as account travel-data deletion. An old
            # in-flight calculation cannot recreate a cleared personal request.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"trip-understanding-user:{user_id}")
            # Lock only after all external calls. This serializes versions, not route calculation.
            room = await conn.fetchrow("SELECT thread_id FROM rooms WHERE room_id=$1 FOR UPDATE", request.room_id)
            member = await conn.fetchval("""SELECT user_id FROM room_members
                WHERE room_id=$1 AND user_id=$2 FOR KEY SHARE""", request.room_id, user_id)
            if room is None or member is None or room["thread_id"] != request.thread_id:
                raise HTTPException(403, detail="不是该房间成员")
            head = await _authorized_head(conn, request.room_id, user_id, thread_id=request.thread_id)
            replay = await _existing_or_check(conn, request, user_id, head)
            if replay is not None:
                return replay
            attempt = await conn.fetchrow("""SELECT status FROM room_itinerary_requests
                WHERE room_id=$1 AND user_id=$2 AND request_id=$3 FOR UPDATE""",
                request.room_id, user_id, request.room_route_request_id)
            if attempt is None or attempt["status"] != "RUNNING":
                raise version_conflict()
            row = await conn.fetchrow("""INSERT INTO room_itinerary_revisions
                (room_id,version,published_by_user_id,request_id,selection_snapshot,itinerary_data,
                 total_distance_km,duration_ms)
                VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8) RETURNING *""",
                request.room_id, request.base_room_route_version + 1, user_id, request.room_route_request_id,
                json.dumps(selection_snapshot(request), ensure_ascii=False),
                json.dumps(_relative_itinerary(result.itinerary), ensure_ascii=False),
                result.total_distance_km, result.duration_ms)
            await conn.execute("""UPDATE room_itinerary_requests SET status='PUBLISHED'
                WHERE room_id=$1 AND user_id=$2 AND request_id=$3""",
                request.room_id, user_id, request.room_route_request_id)
    return _published(row)
