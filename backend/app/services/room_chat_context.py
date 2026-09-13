"""Read member-authorized room context without mutating selection or routes."""
from __future__ import annotations

import json

from fastapi import HTTPException

from app.db.connection import get_pool
from app.schemas.place import Place
from app.services.room_access import require_room_member
from app.services.room_current_itinerary import get_current_itinerary


async def load_room_chat_context(request, *, pool=None) -> dict:
    from app.api.places_persist import _sanitize_shared_place

    pool = pool or await get_pool()
    await require_room_member(request.room_id, request.user_id, thread_id=request.thread_id, pool=pool)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""SELECT p.place_data FROM room_places p
            JOIN rooms r ON r.room_id=p.room_id
            JOIN room_members m ON m.room_id=r.room_id AND m.user_id=$2
            WHERE p.room_id=$1 AND r.thread_id=$3 ORDER BY p.added_at, p.place_id""",
            request.room_id, request.user_id, request.thread_id)
    places = []
    room_selected = set()
    for row in rows:
        raw = row["place_data"]
        public = _sanitize_shared_place(json.loads(raw) if isinstance(raw, str) else raw)
        if public:
            if public["room_selected"]:
                room_selected.add(public["place_id"])
            # No opening hours, suggested duration or unverified scores enter the answer prompt.
            places.append(Place.model_validate({key: public[key] for key in (
                "place_id", "name", "category", "address", "coords", "city", "district", "description")}))
    requested = set(request.selected_place_ids)
    if requested != room_selected or requested - {place.place_id for place in places}:
        raise HTTPException(409, detail="已选地点已变化或尚未保存，请等待地点保存后重试。")
    current = await get_current_itinerary(request.room_id, request.user_id, pool=pool)
    same_selection = bool(current.selection_snapshot and requested
        and set(current.selection_snapshot.place_ids) == requested)
    route = [{"day_index": day.day_index + 1,
        "places": [slot.place["name"] for slot in day.slots]} for day in current.itinerary_data.days
        ] if same_selection and current.itinerary_data else []
    return {"places": places, "selected_place_ids": list(request.selected_place_ids),
        "relative_route": route}
