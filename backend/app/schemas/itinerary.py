from typing import Optional
from pydantic import BaseModel


class TransportLeg(BaseModel):
    mode: str = "driving"  # "driving" | "walking" | "transit"
    duration_mins: int
    distance_km: float


class WeatherInfo(BaseModel):
    condition: str      # "晴" / "多云" / "小雨"
    temp_high: int
    temp_low: int
    suggestion: str     # "适合户外，建议带防晒"


class TimeSlot(BaseModel):
    place_id: str
    place: dict         # Place 对象（避免循环引用，用 dict）
    start_time: str     # "09:00"
    end_time: str       # "11:30"
    transport: Optional[TransportLeg] = None  # 与下一地点的交通（最后一个为 None）
    tips: list[str] = []  # 温馨提示（TipsGenerator 写入）


class DayPlan(BaseModel):
    day_index: int          # 0-based
    date: Optional[str] = None  # ISO 8601，可选
    cluster_id: int
    slots: list[TimeSlot]
    weather_summary: Optional[WeatherInfo] = None


class Itinerary(BaseModel):
    itinerary_id: str
    thread_id: str
    city: str
    days: list[DayPlan]
    generated_at: str       # ISO 8601
    version: int = 1        # 每次重新排线递增
