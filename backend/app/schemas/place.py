from enum import Enum
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field


class PlaceSource(str, Enum):
    AMAP_POI = "amap_poi"
    RAG = "rag"
    SYNTHESIZED = "synthesized"


class PlaceCategory(str, Enum):
    ATTRACTION = "attraction"
    FOOD = "food"
    HOTEL = "hotel"
    TRANSPORT = "transport"
    UNKNOWN = "unknown"


class RetrievalExecutionMode(str, Enum):
    """How a provider-backed place was obtained for this request."""

    LIVE = "live"
    FIXTURE = "fixture"
    FALLBACK = "fallback"


class EvidenceStatus(str, Enum):
    """Evidence boundary for a user-requested place attribute."""

    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"


class Coordinates(BaseModel):
    lng: float
    lat: float


class PlaceRAGMeta(BaseModel):
    tip_snippets: list[str] = Field(default_factory=list, description="从游记提取的避坑/推荐语，最多3条")
    sentiment_score: float = Field(default=0.0, ge=-1.0, le=1.0, description="游记情感倾向 -1~1")
    source_note_ids: list[str] = Field(default_factory=list, description="支撑该内容的游记文档 ID（可溯源）")


class ConstraintEvidence(BaseModel):
    constraint: str = Field(description="稳定的约束键，例如 family_room")
    label: str = Field(description="面向用户的约束名称")
    status: EvidenceStatus
    detail: str = Field(description="证据边界或下一步确认说明")
    source: str = Field(description="证据来源，例如 amap_poi / unavailable_in_poi")
    value: Optional[Any] = Field(None, description="用于判断的结构化原值")
    source_url: Optional[str] = Field(None, description="可打开的来源入口；提供方无字段级 URL 时为空")
    observed_at: Optional[datetime] = Field(None, description="本次来源观测时间")
    confidence: float = Field(default=0.0, ge=0, le=1)


class GeoEvidence(BaseModel):
    slot_id: str
    anchor_place: str
    constraint_kind: str = Field(
        default="proximity",
        description="proximity | route；半径和路线证据分开，避免一个 UNKNOWN 覆盖已验证坐标",
    )
    status: EvidenceStatus
    satisfies_constraint: Optional[bool] = None
    straight_line_distance_km: Optional[float] = None
    estimated_travel_minutes: Optional[int] = None
    transfer_count: Optional[int] = None
    transport_mode: str = "walking"
    source: str = "amap_coordinates"
    route_response_hash: Optional[str] = None
    observed_at: Optional[datetime] = None
    failure_reason: Optional[str] = None


class Place(BaseModel):
    """标准化地点对象 - 系统全局货币，贯穿 LangGraph 三个节点和前端状态树"""
    place_id: str = Field(..., description="高德 POI ID，全局唯一")
    name: str
    category: PlaceCategory
    address: str
    coords: Coordinates
    city: str
    district: Optional[str] = None
    source: PlaceSource = PlaceSource.SYNTHESIZED
    execution_mode: Optional[RetrievalExecutionMode] = Field(
        None,
        description="本次检索执行模式；与内容来源 source 分开，禁止用 amap_poi 冒充 live",
    )
    retrieval_provider: Optional[str] = Field(None, description="本次检索提供方，例如 amap")
    retrieval_request_hash: Optional[str] = Field(None, description="脱敏后提供方请求参数的 SHA256")
    retrieval_response_hash: Optional[str] = Field(None, description="提供方响应的 SHA256")
    retrieval_observed_at: Optional[datetime] = Field(None, description="本次提供方响应观测时间")
    recommendation_slot_ids: list[str] = Field(
        default_factory=list,
        description="该候选满足的 RecommendationPlan 槽位；实体去重时取并集",
    )
    canonical_entity_names: list[str] = Field(
        default_factory=list,
        description="由显式实体槽位绑定的 canonical 名称，不由 LLM 或 POI 名称猜测",
    )

    # 高德客观数据
    amap_rating: Optional[float] = Field(None, ge=0, le=5, description="高德评分 0-5")
    amap_price: Optional[float] = Field(None, description="人均消费（元）")
    opening_hours: Optional[str] = None
    phone: Optional[str] = None
    amap_photos: list[str] = Field(default_factory=list, description="高德图片 URL 列表")

    # RAG 主观数据（无游记命中则为 None）
    rag_meta: Optional[PlaceRAGMeta] = None

    # AI 生成的描述信息
    description: Optional[str] = Field(None, description="一句话特点描述，20-40字")
    tags: list[str] = Field(default_factory=list, description="适合人群/场景标签，如 ['情侣', '拍照', '亲子']")
    constraint_evidence: list[ConstraintEvidence] = Field(
        default_factory=list,
        description="本轮用户约束在该地点上的结构化证据状态；UNKNOWN 不等于满足",
    )
    selection_evidence_status: Optional[EvidenceStatus] = Field(
        None,
        description="SelectionPolicy 汇总后的候选证据等级；UNKNOWN/REQUIRES_CONFIRMATION 不视为满足高风险约束",
    )
    geo_evidence: list[GeoEvidence] = Field(
        default_factory=list,
        description="相对 RecommendationPlan 锚点的坐标距离与预计通勤证据",
    )
    confirmation_actions: list[str] = Field(
        default_factory=list,
        description="对 REQUIRES_CONFIRMATION 约束给出可执行联系动作，不把确认动作冒充已满足",
    )

    # Optimizer 节点写入
    cluster_id: Optional[int] = Field(None, description="K-Means 分配的日期簇 ID")
    visit_order: Optional[int] = Field(None, description="簇内 TSP 排序序号")
    estimated_duration: Optional[int] = Field(None, description="建议游览时长（分钟）")
    duration_basis: Optional[str] = Field(None, description="时长来源：llm/rule/user")

    # 温馨提示（TipsGenerator 写入）
    tips: list[str] = Field(default_factory=list, description="出行温馨提示，如取号/招牌菜/天气")
