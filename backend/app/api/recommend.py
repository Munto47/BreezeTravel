"""
POST /api/recommend - 城市候选地点推荐接口

进入规划房间后自动调用，返回分类推荐（美景/美食/美梦）。
优先级：
  1. 用户有历史行程 → 按偏好品类相似推荐
  2. 无历史 → LLM 生成搜索词，高德 API 取真实地点数据
"""

import json
import asyncio
from typing import Optional

import aiohttp
from fastapi import APIRouter
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import settings
from app.db.connection import get_pool
from app.schemas.place import Place, Coordinates, PlaceCategory, PlaceSource

router = APIRouter()

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

DEFAULT_DURATION = {
    PlaceCategory.ATTRACTION: 120,
    PlaceCategory.FOOD: 60,
    PlaceCategory.HOTEL: 30,
    PlaceCategory.TRANSPORT: 15,
}


class RecommendRequest(BaseModel):
    city: str
    trip_days: int = 3
    user_id: Optional[str] = None


class RecommendResponse(BaseModel):
    city: str
    places: list[Place]


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
            amap_rating=float(rating_str) if rating_str and str(rating_str).replace('.', '', 1).isdigit() else None,
            amap_price=float(price_str) if price_str and str(price_str).replace('.', '', 1).isdigit() else None,
            opening_hours=raw.get("biz_opentime") if isinstance(raw.get("biz_opentime"), str) else None,
            phone=raw.get("tel") if isinstance(raw.get("tel"), str) else None,
            amap_photos=photos,
            estimated_duration=DEFAULT_DURATION.get(category, 90),
        )
    except Exception as e:
        print(f"[Recommend] 解析 POI 失败：{e}")
        return None


async def _fetch_amap_poi(keywords: str, city: str, limit: int = 5) -> list[Place]:
    """调用高德 POI 文本搜索 API，返回最多 limit 个地点"""
    url = "https://restapi.amap.com/v3/place/text"
    params = {
        "key": settings.amap_api_key,
        "keywords": keywords,
        "city": city,
        "output": "json",
        "extensions": "all",
        "offset": min(limit, 10),
        "sortrule": "weight",   # 按综合权重（热度）排序
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
                if data.get("status") != "1" or not data.get("pois"):
                    return []
                places = [_parse_amap_place(p, city) for p in data["pois"]]
                return [p for p in places if p is not None][:limit]
    except Exception as e:
        print(f"[Recommend] 高德请求失败 keywords={keywords}: {e}")
        return []


def _calc_counts(trip_days: int) -> tuple[int, int, int]:
    """根据旅行天数计算景点/餐厅/酒店推荐数量"""
    if trip_days <= 2:
        return 4, 2, 1
    elif trip_days <= 3:
        return 6, 3, 1
    elif trip_days <= 5:
        return 8, 4, 2
    else:
        return 10, 5, 2


async def _recommend_smart(city: str, trip_days: int) -> list[Place]:
    """
    无历史数据时：LLM 生成搜索关键词 → 高德 API 取真实地点。
    LLM 失败时降级为固定关键词策略。
    """
    n_attract, n_food, n_hotel = _calc_counts(trip_days)

    # ── Step 1：LLM 生成搜索关键词 ──────────────────────────────────────
    queries: list[dict] = []
    if settings.deepseek_api_key:
        try:
            client = AsyncOpenAI(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_api_url,
            )
            prompt = (
                f"你是专业旅行顾问，请为用户生成在【{city}】{trip_days}天旅行的高德地图POI搜索关键词。\n\n"
                f"要求：\n"
                f"- 景点：{n_attract}个关键词，优先5A/4A景区、城市地标、热门网红打卡地\n"
                f"- 美食：{n_food}个关键词，优先当地知名连锁品牌、必吃老字号、网红餐厅\n"
                f"- 住宿：{n_hotel}个关键词，优先四星/五星连锁品牌酒店\n\n"
                f"每个关键词用于高德POI搜索，应简短精准（如"宽窄巷子"、"海底捞火锅"、"万豪酒店"）。\n"
                f'仅返回JSON数组，格式：[{{"keyword":"...","type":"attraction"}},{{"keyword":"...","type":"food"}},{{"keyword":"...","type":"hotel"}}]'
            )
            resp = await client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=512,
            )
            raw = resp.choices[0].message.content.strip()
            # 提取 JSON 数组
            start, end = raw.find("["), raw.rfind("]")
            if start != -1 and end != -1:
                queries = json.loads(raw[start:end + 1])
        except Exception as e:
            print(f"[Recommend] LLM 生成关键词失败，降级固定策略: {e}")

    # LLM 失败 → 固定关键词
    if not queries:
        queries = (
            [{"keyword": f"{city}热门景点", "type": "attraction"},
             {"keyword": f"{city}必游景区", "type": "attraction"}] * ((n_attract + 1) // 2)
            + [{"keyword": f"{city}知名餐厅", "type": "food"},
               {"keyword": f"{city}特色美食", "type": "food"}] * ((n_food + 1) // 2)
            + [{"keyword": f"{city}四星酒店", "type": "hotel"}] * n_hotel
        )

    # ── Step 2：并发调用高德 API（错开 300ms 防触发 QPS 限制）───────────
    type_limit = {"attraction": n_attract, "food": n_food, "hotel": n_hotel}
    type_count: dict[str, int] = {"attraction": 0, "food": 0, "hotel": 0}
    all_places: list[Place] = []
    seen_ids: set[str] = set()

    async def fetch_one(kw: str, ptype: str, delay: float) -> list[Place]:
        await asyncio.sleep(delay)
        remaining = type_limit.get(ptype, 3) - type_count.get(ptype, 0)
        if remaining <= 0:
            return []
        results = await _fetch_amap_poi(kw, city, limit=remaining + 1)
        return results

    tasks = [fetch_one(q["keyword"], q.get("type", "attraction"), i * 0.3)
             for i, q in enumerate(queries)]
    results = await asyncio.gather(*tasks)

    for batch, q in zip(results, queries):
        ptype = q.get("type", "attraction")
        for place in batch:
            if place.place_id in seen_ids:
                continue
            if type_count.get(ptype, 0) >= type_limit.get(ptype, 3):
                continue
            seen_ids.add(place.place_id)
            all_places.append(place)
            type_count[ptype] = type_count.get(ptype, 0) + 1

    print(f"[Recommend] 智能推荐 city={city} days={trip_days}: "
          f"景点{type_count.get('attraction',0)} 美食{type_count.get('food',0)} 酒店{type_count.get('hotel',0)}")
    return all_places


async def _recommend_from_history(user_id: str, city: str, trip_days: int) -> list[Place]:
    """
    有历史行程时：提取用户偏好品类比例，按比例在新城市搜索相似地点。
    若历史不足，返回空列表，调用方降级到 _recommend_smart。
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT itinerary_data
                FROM saved_itineraries
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT 5
                """,
                user_id,
            )
        if not rows:
            return []

        # 统计历史投票地点品类
        cat_count = {"attraction": 0, "food": 0, "hotel": 0}
        for row in rows:
            itin = json.loads(row["itinerary_data"])
            for day in itin.get("days", []):
                for slot in day.get("slots", []):
                    cat = slot.get("place", {}).get("category", "")
                    if cat in cat_count:
                        cat_count[cat] += 1

        total = sum(cat_count.values())
        if total == 0:
            return []

        n_attract, n_food, n_hotel = _calc_counts(trip_days)
        # 按历史偏好调整比例（偏好景点多则多推景点）
        ratio = {k: v / total for k, v in cat_count.items()}
        total_slots = n_attract + n_food + n_hotel
        n_attract = max(1, round(total_slots * ratio["attraction"]))
        n_food    = max(1, round(total_slots * ratio["food"]))
        n_hotel   = max(1, total_slots - n_attract - n_food)

        all_places: list[Place] = []
        seen_ids: set[str] = set()

        async def fetch_cat(kw: str, limit: int, delay: float) -> list[Place]:
            await asyncio.sleep(delay)
            return await _fetch_amap_poi(kw, city, limit=limit)

        results = await asyncio.gather(
            fetch_cat(f"{city}热门景点", n_attract, 0),
            fetch_cat(f"{city}知名餐厅", n_food, 0.3),
            fetch_cat(f"{city}连锁酒店", n_hotel, 0.6),
        )
        for batch in results:
            for place in batch:
                if place.place_id not in seen_ids:
                    seen_ids.add(place.place_id)
                    all_places.append(place)

        print(f"[Recommend] 历史偏好推荐 user={user_id} city={city}: {len(all_places)} 个地点")
        return all_places
    except Exception as e:
        print(f"[Recommend] 历史推荐失败，降级: {e}")
        return []


@router.post("/recommend", response_model=RecommendResponse)
async def recommend(request: RecommendRequest):
    """
    城市候选地点推荐。
    有历史行程 → 按偏好推荐；无历史 → LLM 生成关键词 + 高德真实数据。
    """
    city = request.city.strip() or "成都"

    if not settings.amap_api_key or settings.demo_mode:
        print(f"[Recommend] 未配置 AMAP_API_KEY 或 Demo 模式，返回空列表")
        return RecommendResponse(city=city, places=[])

    # ① 有历史数据时优先走历史推荐
    if request.user_id:
        history_places = await _recommend_from_history(request.user_id, city, request.trip_days)
        if history_places:
            return RecommendResponse(city=city, places=history_places)

    # ② 无历史 / 历史为空 → LLM + 高德智能推荐
    places = await _recommend_smart(city, request.trip_days)
    return RecommendResponse(city=city, places=places)
