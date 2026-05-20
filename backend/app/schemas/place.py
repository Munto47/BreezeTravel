from enum import Enum
from typing import Optional
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


class Coordinates(BaseModel):
    lng: float
    lat: float


class PlaceRAGMeta(BaseModel):
    tip_snippets: list[str] = Field(default_factory=list, description="从游记提取的避坑/推荐语，最多3条")
    sentiment_score: float = Field(default=0.0, ge=-1.0, le=1.0, description="游记情感倾向 -1~1")
    source_note_ids: list[str] = Field(default_factory=list, description="支撑该内容的游记文档 ID（可溯源）")


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

    # Optimizer 节点写入
    cluster_id: Optional[int] = Field(None, description="K-Means 分配的日期簇 ID")
    visit_order: Optional[int] = Field(None, description="簇内 TSP 排序序号")
    estimated_duration: Optional[int] = Field(None, description="建议游览时长（分钟）")
    duration_basis: Optional[str] = Field(None, description="时长来源：llm/rule/user")

    # 温馨提示（TipsGenerator 写入）
    tips: list[str] = Field(default_factory=list, description="出行温馨提示，如取号/招牌菜/天气")
