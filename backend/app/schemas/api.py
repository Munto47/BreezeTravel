from typing import Optional
from pydantic import BaseModel, Field

from app.schemas.place import Coordinates, Place, PlaceCategory
from app.schemas.itinerary import Itinerary, TransportLeg, WeatherInfo
from app.schemas.task_spec import TripTaskSpec
from app.schemas.verification import VerificationReport


# ===== POST /api/chat =====

class ChatRequest(BaseModel):
    thread_id: str
    user_id: str
    room_id: Optional[str] = None
    message: str = Field(min_length=1, max_length=1200)
    selected_place_ids: list[str] = []
    trip_city: Optional[str] = None   # 房间目的地城市，用于 AmapSearch 精确检索
    use_long_term_memory: bool = False  # 协同模式默认无痕；只有未来显式授权后才可开启


# SSE 事件类型（以 text/event-stream 格式推送）
# data: {"event":"progress","data":{"phase":"UNDERSTANDING"}}
# data: {"event":"place","data":{"place":{...}}}
# data: {"event":"text","data":{"delta":"..."}}
# data: {"event":"done","data":{"status":"READY","total_places":5}}


# ===== POST /api/optimize =====

class OptimizeRequest(BaseModel):
    thread_id: str
    room_id: Optional[str] = None
    places: list[Place] = Field(..., max_length=30)
    trip_days: int = Field(..., ge=1, le=31)
    start_date: Optional[str] = None    # ISO 8601
    working_context: Optional[dict] = None  # 会话偏好，用于 TipsGenerator 个性化提示
    user_prefs: Optional[dict] = None   # GroupPreferences（含 must_have/no_go/style）
    vote_counts: dict[str, int] = {}    # D24：place_id → 票数（Yjs votedBy 长度）
    task_spec: Optional[TripTaskSpec] = None
    planning_input_hash: Optional[str] = None
    workspace_id: Optional[str] = None
    persist_workspace: bool = False


class OptimizeResponse(BaseModel):
    itinerary: Itinerary
    total_distance_km: float
    optimization_method: str = "kmeans_tsp"
    duration_ms: int
    backup_pool: list[Place] = []        # 因时间/体力不足被移出行程的备选地点（A7）
    critic_violations: list[dict] = []   # Critic 硬规则违规摘要（供前端展示警告）
    task_spec: Optional[TripTaskSpec] = None
    verification_report: Optional[VerificationReport] = None
    planning_input_hash: Optional[str] = None
    workspace_id: Optional[str] = None
    itinerary_revision: Optional[int] = None
    audit_report_id: Optional[str] = None
    audit_status: Optional[str] = None
    audit_error_code: Optional[str] = None
    tips_status: Optional[str] = None
    tips_basis_revision: Optional[int] = None
    tips_basis_report_id: Optional[str] = None


class ExperiencePlaceView(BaseModel):
    """User-facing collaboration place without provider or evidence receipts."""

    place_id: str
    name: str
    category: PlaceCategory
    address: str
    coords: Coordinates
    city: str
    district: Optional[str] = None
    amap_rating: Optional[float] = None
    amap_price: Optional[float] = None
    opening_hours: Optional[str] = None
    phone: Optional[str] = None
    amap_photos: list[str] = Field(default_factory=list)
    description: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    estimated_duration: Optional[int] = None
    tips: list[str] = Field(default_factory=list)


class ExperienceTimeSlotView(BaseModel):
    place_id: str
    place: ExperiencePlaceView
    start_time: str
    end_time: str
    transport: Optional[TransportLeg] = None
    tips: list[str] = Field(default_factory=list)


class ExperienceDayPlanView(BaseModel):
    day_index: int
    date: Optional[str] = None
    cluster_id: int
    slots: list[ExperienceTimeSlotView]
    weather_summary: Optional[WeatherInfo] = None


class ExperienceItineraryView(BaseModel):
    city: str
    days: list[ExperienceDayPlanView]
    generated_at: str


class ExperienceOptimizeResponse(BaseModel):
    """Narrow response used by the ordinary collaboration experience."""

    itinerary: ExperienceItineraryView
    backup_pool: list[ExperiencePlaceView] = Field(default_factory=list)


# ===== GET /api/room/{room_id}/state =====

class RoomStateResponse(BaseModel):
    room_id: str
    thread_id: str
    phase: str
    trip_city: Optional[str] = None
    trip_days: int = 3
    place_count: int = 0
