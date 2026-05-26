"""群体偏好与行程约束 schema"""

from typing import Literal, Optional
from pydantic import BaseModel, Field


class GroupPreferences(BaseModel):
    """多人协同行程的群体偏好（并集策略）"""
    # 出行风格：文化深度 / 夜生活 / 亲子 / 美食 / 自由
    style: Literal["culture", "nightlife", "family", "food", "free"] = "free"
    has_kids: bool = False

    # 偏好并集（D12）
    must_have: list[str] = Field(default_factory=list, description="必须包含的类型/地点，并集")
    nice_to_have: list[str] = Field(default_factory=list, description="希望有，加分项")
    no_go: list[str] = Field(default_factory=list, description="拒绝的类型，硬剔除")

    # 行程参数
    trip_city: str = ""
    trip_days: int = 2
    arrival_time: Optional[str] = None    # "HH:MM" 抵达时间（第一天）
    departure_time: Optional[str] = None  # "HH:MM" 离开时间（最后一天）

    # 天气偏好（自动适配时使用）
    avoid_outdoor_rain: bool = True
    avoid_outdoor_heat: bool = True       # 夏季 11:30–14:00 避免户外


class WeatherDay(BaseModel):
    """单日天气摘要（供 Scheduler 使用）"""
    date: str                              # "YYYY-MM-DD"
    condition: str                         # "sunny" / "cloudy" / "rainy" / "snowy"
    precip_mm: float = 0.0                 # 降水量（毫米）
    temp_max: float = 25.0
    temp_min: float = 15.0
    sunrise: str = "06:00"
    sunset: str = "18:30"
