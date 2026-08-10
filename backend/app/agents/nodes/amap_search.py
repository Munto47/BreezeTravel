"""
AmapSearch 节点：高德 POI 搜索

输入：state.query_rewrite（改写后的查询）, state.trip_city（房间目的地城市）
输出：state.amap_places（Place 列表）

Mock 模式（AMAP_MOCK=true）：
  从 backend/tests/fixtures/amap_mock_places.json 读取预设数据

真实模式（AMAP_MOCK=false）：
  调用高德 POI 搜索 API: https://restapi.amap.com/v3/place/text
  若真实 API 返回空结果，自动降级到 Mock 数据
"""

import json
from pathlib import Path
from typing import Optional

import aiohttp

from app.agents.state import AgentState
from app.config import settings
from app.schemas.place import Place, Coordinates, PlaceCategory, PlaceSource

# 高德 POI 大类 → 系统 category 映射
AMAP_TYPE_MAP = {
    "餐饮": PlaceCategory.FOOD,
    "美食": PlaceCategory.FOOD,
    "景区": PlaceCategory.ATTRACTION,
    "风景名胜": PlaceCategory.ATTRACTION,
    "旅游景点": PlaceCategory.ATTRACTION,
    "住宿": PlaceCategory.HOTEL,
    "酒店": PlaceCategory.HOTEL,
    "交通": PlaceCategory.TRANSPORT,
}

MOCK_DATA_PATH = Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / "amap_mock_places.json"

# 默认游览时长（分钟），按 category
DEFAULT_DURATION = {
    PlaceCategory.ATTRACTION: 120,
    PlaceCategory.FOOD: 60,
    PlaceCategory.HOTEL: 30,
    PlaceCategory.TRANSPORT: 15,
}


def _parse_amap_type(type_str: str) -> PlaceCategory:
    for key, cat in AMAP_TYPE_MAP.items():
        if key in type_str:
            return cat
    return PlaceCategory.ATTRACTION


def _parse_amap_place(raw: dict, city: str) -> Optional[Place]:
    try:
        location = raw.get("location", "")
        if not location:
            return None
        lng_str, lat_str = location.split(",")

        # biz_ext 可能是 dict 或空列表（高德 API 特性）
        biz_ext = raw.get("biz_ext")
        if not isinstance(biz_ext, dict):
            biz_ext = {}

        rating_str = biz_ext.get("rating", "")
        price_str = biz_ext.get("cost", "")

        photos = []
        if raw.get("photos"):
            photos = [p.get("url", "") for p in raw["photos"][:3] if p.get("url")]

        category = _parse_amap_type(raw.get("type", ""))
        return Place(
            place_id=raw.get("id", ""),
            name=raw.get("name", ""),
            category=category,
            address=raw.get("address", "") or "",
            coords=Coordinates(lng=float(lng_str), lat=float(lat_str)),
            city=city,
            district=raw.get("adname"),
            source=PlaceSource.AMAP_POI,
            amap_rating=float(rating_str) if rating_str and isinstance(rating_str, (int, float, str)) and str(rating_str).replace('.', '', 1).isdigit() else None,
            amap_price=float(price_str) if price_str and isinstance(price_str, (int, float, str)) and str(price_str).replace('.', '', 1).isdigit() else None,
            opening_hours=raw.get("biz_opentime") if isinstance(raw.get("biz_opentime"), str) else None,
            phone=raw.get("tel") if isinstance(raw.get("tel"), str) else None,
            amap_photos=photos,
            estimated_duration=DEFAULT_DURATION.get(category, 90),
        )
    except Exception as e:
        print(f"[AmapSearch] 解析 POI 失败：{e}，原始数据：{raw.get('name')}")
        return None


def _extract_city(state: AgentState) -> str:
    """
    从 state 中提取城市：
    1. 优先使用 state.trip_city（从房间元数据传入，最可靠）
    2. 从对话历史关键词匹配
    3. 默认成都
    """
    # 优先使用 trip_city（从 ChatRequest 传入）
    trip_city = state.get("trip_city")
    if trip_city:
        return trip_city

    # 从对话历史提取
    known_cities = ["北京", "上海", "成都", "厦门", "广州", "深圳", "杭州", "西安", "重庆"]
    for msg in reversed(state["messages"]):
        content = str(msg.content)
        for city in known_cities:
            if city in content:
                return city
    return "成都"  # 默认城市


async def _fetch_amap_poi(
    keywords: str, city: str,
    prefer_trending: bool = False,
    prefer_chain: bool = False,
) -> list[Place]:
    """调用高德 POI 搜索 API"""
    url = "https://restapi.amap.com/v3/place/text"
    params = {
        "key": settings.amap_api_key,
        "keywords": keywords,
        "city": city,
        "output": "json",
        "extensions": "all",
        "offset": 10,
    }
    # 热门排序：高德 sortrule=weight 按综合热度（评分+评论量）排序
    if prefer_trending:
        params["sortrule"] = "weight"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()
            if data.get("status") != "1" or not data.get("pois"):
                print(f"[AmapSearch] 高德 API 返回空结果：status={data.get('status')}, info={data.get('info')}")
                return []
            places = [_parse_amap_place(p, city) for p in data["pois"]]
            return [p for p in places if p is not None]


_FOOD_KW    = {"美食", "吃", "餐", "火锅", "饭", "菜", "小吃", "饮食", "餐厅", "饭馆", "美味", "料理"}
_HOTEL_KW   = {"酒店", "住宿", "民宿", "旅馆", "客栈", "住", "入住", "床位", "宾馆"}
_ATTRACT_KW = {"景点", "景区", "参观", "游览", "打卡", "观光", "博物馆", "公园", "古迹", "名胜", "寺庙"}

# Mock 数据没有的类目：酒吧/娱乐/购物等。查询这些时返回空，
# 宁可让上层兜底搜索真实 API，也不能返回驴唇不对马嘴的数据。
_ENTERTAIN_KW = {
    "酒吧", "酒馆", "小酒馆", "夜店", "夜生活", "夜酒",
    "精酿", "酒精", "清吧", "livehouse", "live house", "live bar",
    "ktv", "KTV", "卡拉ok", "卡拉OK",
    "酒吧推荐", "蹦迪", "夜场",
    "购物", "商场", "超市", "买",
    "spa", "SPA", "按摩", "足疗",
}


def _load_mock_places(city: str, query: str = "") -> list[Place]:
    """从本地 fixture 文件加载 Mock 数据，按查询意图过滤品类后返回。

    重要：Mock 数据只包含 attraction / food / hotel 三类。
    如果查询明确指向 Mock 数据不存在的类目（酒吧/娱乐/购物等），
    直接返回空列表，让调用方走真实 API 或给出明确提示，
    避免把不相关的地点（如早餐店）当作酒吧返回。
    """
    if not MOCK_DATA_PATH.exists():
        print(f"[AmapSearch] Mock 文件不存在：{MOCK_DATA_PATH}")
        return []
    with open(MOCK_DATA_PATH, "r", encoding="utf-8") as f:
        mock_data = json.load(f)

    city_places = mock_data.get(city, mock_data.get("成都", []))
    all_places = [Place(**p) for p in city_places]

    if not query:
        return all_places

    q = query.lower()

    # 娱乐/酒吧等 Mock 数据不存在的类目 → 直接返回空，拒绝返回无关数据
    if any(kw in q for kw in _ENTERTAIN_KW):
        print(f"[AmapSearch] Mock 不含娱乐/酒吧类目，返回空（query={query[:40]}）")
        return []

    want_food    = any(kw in q for kw in _FOOD_KW)
    want_hotel   = any(kw in q for kw in _HOTEL_KW)
    want_attract = any(kw in q for kw in _ATTRACT_KW)

    # 精确意图：只返回对应品类（不混入其他）
    if want_food and not want_hotel and not want_attract:
        matched = [p for p in all_places if p.category == PlaceCategory.FOOD]
        return matched if matched else all_places

    if want_hotel and not want_food and not want_attract:
        matched = [p for p in all_places if p.category == PlaceCategory.HOTEL]
        return matched if matched else all_places

    if want_attract and not want_food and not want_hotel:
        matched = [p for p in all_places if p.category == PlaceCategory.ATTRACTION]
        return matched if matched else all_places

    # 混合意图或无明确意图：按品类优先排序返回全部
    priority = PlaceCategory.FOOD if want_food else (
        PlaceCategory.HOTEL if want_hotel else PlaceCategory.ATTRACTION
    )
    prioritized = [p for p in all_places if p.category == priority]
    others = [p for p in all_places if p.category != priority]
    return prioritized + others


async def run(state: AgentState) -> dict:
    """AmapSearch 节点入口函数"""
    query = state.get("query_rewrite") or ""
    city = _extract_city(state)
    ctx = state.get("working_context") or {}
    prefer_trending: bool = bool(ctx.get("prefer_trending", False))
    prefer_chain: bool = bool(ctx.get("prefer_chain", False))

    # 若偏好连锁品牌且关键词中未含"连锁"，追加修饰词
    if prefer_chain and "连锁" not in query:
        query = f"{query} 连锁".strip()

    if settings.amap_mock or settings.demo_mode:
        places = _load_mock_places(city, query)
        if prefer_trending:
            places = sorted(places, key=lambda p: p.amap_rating or 0, reverse=True)
        print(f"[AmapSearch] Mock 模式，city={city}，query={query!r}，返回 {len(places)} 个地点")
        return {"amap_places": places}

    # 真实高德 API 模式
    if not settings.amap_api_key:
        print("[AmapSearch] 未配置 AMAP_API_KEY，降级到 Mock")
        return {"amap_places": _load_mock_places(city, query)}

    try:
        places = await _fetch_amap_poi(query, city, prefer_trending=prefer_trending, prefer_chain=prefer_chain)
    except Exception as e:
        print(f"[AmapSearch] 高德 API 调用异常：{e}，降级到 Mock")
        places = _load_mock_places(city, query)
        return {"amap_places": places}

    # 真实 API 返回空结果时降级到 Mock（避免 query 不匹配导致空列表）
    if not places:
        print(f"[AmapSearch] 真实 API 返回空，降级到 Mock，city={city}, query={query}")
        places = _load_mock_places(city, query)

    print(f"[AmapSearch] city={city}, query={query}, prefer_trending={prefer_trending}, 返回 {len(places)} 个地点")
    return {"amap_places": places}
