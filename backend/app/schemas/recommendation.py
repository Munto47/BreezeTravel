"""推荐结果 schema（SPEC §5.1 / Phase B）

PlaceRecommendation — 每个推荐地点的结构化输出：
  · reason           推荐理由（必须引自 RAG chunk，含 chunk_id 引用）
  · avoid_tips       避坑提示（同上要求）
  · source_chunk_ids 引用的游记 chunk ID 列表（Critic 验证真实存在）
  · alternatives     1–2 个替代方案
  · confidence       置信度：high / medium / low

Alternative — 替代方案：
  · why_alternative  为何推荐这个替代（比 A 更便宜 / 排队少 / 更适合带娃）
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field


class Alternative(BaseModel):
    """替代方案"""
    place_id: str
    name: str
    why_alternative: str = ""  # "比 A 更适合带娃 / 更便宜 / 排队少"


class PlaceRecommendation(BaseModel):
    """地点推荐结构化输出（Phase B 核心数据结构）"""
    place_id: str
    name: str
    category_l1: str = ""
    category_l2: str = ""

    # 推荐理由（必须有 chunk 支撑，否则 Critic 剥离为空字符串）
    reason: str = ""

    # 适合人群与避坑
    suitable_for: list[str] = Field(default_factory=list)
    avoid_tips: list[str] = Field(default_factory=list)

    # 游记来源 chunk（Critic 验证：chunk_id ∈ 本次 RAG context）
    source_chunk_ids: list[str] = Field(default_factory=list)

    # 替代方案（最多 2 个）
    alternatives: list[Alternative] = Field(default_factory=list)

    # 置信度
    confidence: Literal["high", "medium", "low"] = "low"
