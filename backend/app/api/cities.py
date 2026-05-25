"""
GET /api/cities/supported — 返回有 RAG 游记语料的"深度推荐"城市列表

前端用此清单给城市选择器加角标，避免用户选了一个 RAG 不支持的城市后体验大幅退化。

数据源：travel_notes 表的 DISTINCT city，按游记篇数倒序。
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.db.connection import get_pool


router = APIRouter()


# 兜底列表：当 DB 查询失败或表空时使用，与 scripts/ingest_notes.py 的 CITIES 对齐
_FALLBACK_SUPPORTED_CITIES = [
    "成都", "北京", "上海", "广州", "深圳", "杭州", "厦门",
]


class CityInfo(BaseModel):
    city: str
    notes_count: int
    chunks_count: int = 0


class SupportedCitiesResponse(BaseModel):
    cities: list[CityInfo]
    total_supported: int


@router.get("/cities/supported", response_model=SupportedCitiesResponse)
async def get_supported_cities():
    """返回有 RAG 深度推荐能力的城市清单。

    前端逻辑：
    - city in supported → 显示 🧠 角标，AI 推荐质量高（含游记知识）
    - city not in supported → 显示 🗺️ 角标，仅高德 POI 兜底
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT city, COUNT(*)::int AS notes_count
                FROM travel_notes
                WHERE city IS NOT NULL AND city <> ''
                GROUP BY city
                ORDER BY notes_count DESC
                """
            )
        cities = [
            CityInfo(city=r["city"], notes_count=r["notes_count"])
            for r in rows
        ]
        if not cities:
            # 兜底：DB 没数据但服务可用，仍然按 ingest 脚本声明的城市返回
            cities = [CityInfo(city=c, notes_count=0) for c in _FALLBACK_SUPPORTED_CITIES]
    except Exception as exc:
        print(f"[Cities] 查询失败，降级到兜底列表：{exc}")
        cities = [CityInfo(city=c, notes_count=0) for c in _FALLBACK_SUPPORTED_CITIES]

    return SupportedCitiesResponse(cities=cities, total_supported=len(cities))
