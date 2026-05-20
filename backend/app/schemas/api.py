from typing import Optional
from pydantic import BaseModel

from app.schemas.place import Place
from app.schemas.itinerary import Itinerary


# ===== POST /api/chat =====

class ChatRequest(BaseModel):
    thread_id: str
    user_id: str
    message: str
    selected_place_ids: list[str] = []
    trip_city: Optional[str] = None   # 房间目的地城市，用于 AmapSearch 精确检索


# SSE 事件类型（以 text/event-stream 格式推送）
# data: {"event":"thinking","data":{"node":"router","summary":"...","ms":120}}
# data: {"event":"place","data":{"place":{...}}}
# data: {"event":"text","data":{"delta":"..."}}
# data: {"event":"done","data":{"total_places":5,"total_ms":1840}}


# ===== POST /api/optimize =====

class OptimizeRequest(BaseModel):
    thread_id: str
    places: list[Place]
    trip_days: int
    start_date: Optional[str] = None    # ISO 8601
    working_context: Optional[dict] = None  # 会话偏好，用于 TipsGenerator 个性化提示


class OptimizeResponse(BaseModel):
    itinerary: Itinerary
    total_distance_km: float
    optimization_method: str = "kmeans_tsp"
    duration_ms: int


# ===== GET /api/room/{room_id}/state =====

class RoomStateResponse(BaseModel):
    room_id: str
    thread_id: str
    phase: str
    trip_city: Optional[str] = None
    trip_days: int = 3
    place_count: int = 0
