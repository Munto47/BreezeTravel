"""GET /api/themes — 主题模板入口（SPEC §5.3 / Phase B）

4 个预设主题，点击后用预设 query 走完整 AI 链路（RAG + Amap + Synthesizer v2）。
"""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class ThemeItem(BaseModel):
    theme_id: str
    name: str
    icon: str
    query: str          # 预设查询，直接注入 /api/chat
    description: str = ""


THEME_TEMPLATES: list[dict] = [
    {
        "theme_id": "citywalk",
        "name": "Citywalk 老城线",
        "icon": "🚶",
        "query": "推荐适合 Citywalk 的老城区步行路线，历史街区、胡同或老街，最好有文艺咖啡馆",
        "description": "漫步老城，感受历史烟火气",
    },
    {
        "theme_id": "museum",
        "name": "博物馆深度线",
        "icon": "🏛",
        "query": "推荐城市里值得深度参观的博物馆、美术馆、纪念馆，含营业时间和避坑提示",
        "description": "深度探索文化宝藏",
    },
    {
        "theme_id": "food",
        "name": "美食扫街线",
        "icon": "🍜",
        "query": "推荐当地最有代表性的美食街和餐厅，包含本地特色小吃、老字号和高分网红店",
        "description": "舌尖上的城市探索",
    },
    {
        "theme_id": "family",
        "name": "亲子轻松线",
        "icon": "👨‍👩‍👧",
        "query": "推荐适合带小朋友的亲子景点，动物园、科技馆、主题乐园或公园，需要儿童友好",
        "description": "适合全家出行的轻松路线",
    },
]


@router.get("/themes", response_model=list[ThemeItem])
async def get_themes():
    """返回预设主题模板列表"""
    return [ThemeItem(**t) for t in THEME_TEMPLATES]
