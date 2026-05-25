"""
POST /api/recommend - 城市候选地点推荐接口

进入规划房间后自动调用，返回分类推荐（美景/美食/美梦）。
优先级：
  1. 用户有历史行程 → 按偏好品类相似推荐
  2. 无历史 → LLM 生成搜索词，高德 API 取真实地点数据
"""

import json
import re
import asyncio
from typing import Optional

import aiohttp
from fastapi import APIRouter
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import settings
from app.db.connection import get_pool
from app.schemas.place import Place, Coordinates, PlaceCategory, PlaceSource


# ── 同景点多入口 / 同品牌多分店去重正则 ────────────────────────────────
# 去掉「(xxx店)」「(xxx馆)」「(xxx中心)」「-xxx号」等后缀后视为同一品牌
_BRAND_SUFFIX_RE = re.compile(
    r"[\(\（].*?[\)\）]|"        # (南门店) / （旗舰店）
    r"[\[\【].*?[\]\】]|"        # [总店] / 【新店】
    r"[\-—–]\s*\S+店$|"          # -xxx 店
    r"(?:总店|分店|旗舰店|新店|本店|店)$"
)


def _normalize_brand(name: str) -> str:
    """提取品牌主名用于去重：'陈麻婆豆腐(南门店)' → '陈麻婆豆腐'"""
    n = (name or "").strip()
    while True:
        new_n = _BRAND_SUFFIX_RE.sub("", n).strip()
        if new_n == n:
            break
        n = new_n
    return n or name


# 常见城市前缀，用于剥离生成 stem
_CITY_PREFIXES = (
    "北京", "上海", "成都", "广州", "深圳", "杭州", "西安", "重庆", "厦门", "三亚",
    "丽江", "桂林", "苏州", "南京", "武汉", "长沙", "天津", "青岛", "大连", "昆明",
)

# 常见景点/品牌尾缀，去掉后做主体名比较
_VENUE_TAIL_RE = re.compile(
    r"(?:景区|景点|风景区|风景名胜区|博物馆|纪念馆|展览馆|大剧院|剧场|动物园|植物园|"
    r"公园|广场|商业街|步行街|古镇|古城|遗址|寺|寺庙|宫|塔|楼|湖|山|岛|海滩|"
    r"游客中心|服务中心|办公楼|售票处|入口|出入口|出口|大门|东门|西门|南门|北门|"
    r"东广场|西广场|南广场|北广场|东区|西区|南区|北区|[A-Z]区|"
    r"店|总店|分店|旗舰店|新店|本店)$"
)

# 括号 / 中文括号 / 方括号注释（含分店标识）
_PAREN_RE = re.compile(r"[\(\（\[\【].*?[\)\）\]\】]")

# 分隔符 + 描述性尾段（- 观光车站、· 宽厂、- 竹道 等）
_SEPARATOR_TAIL_RE = re.compile(r"[·\-—–]\s*\S+$")


def _venue_stem(name: str) -> str:
    """提取景点/品牌的主体名词，用于跨条目去重。

    流程：① 去括号注释 ② 剥离城市前缀 ③ 去分隔符尾段 ④ 多轮去尾缀
    例：
      '成都大熊猫繁育研究基地小熊猫1号活动场' → '大熊猫繁育研究基地小熊猫1号'
      '宽窄巷子·宽厂' → '宽窄巷子'
      '武侯祠锦里中心' → '武侯祠锦里'
      '小龙坎火锅(春熙太古里店)' → '小龙坎火锅'
    """
    n = (name or "").strip()
    # ① 去括号注释
    n = _PAREN_RE.sub("", n).strip()
    # ② 剥离城市前缀
    for c in _CITY_PREFIXES:
        if n.startswith(c):
            n = n[len(c):].strip()
            break
    # ③ 去分隔符 + 描述尾段（如 -竹道 / ·宽厂）
    n = _SEPARATOR_TAIL_RE.sub("", n).strip()
    # ④ 多轮去通用尾缀
    while True:
        new_n = _VENUE_TAIL_RE.sub("", n).strip()
        if new_n == n or not new_n:
            break
        n = new_n
    return n or name


def _strip_city_and_parens(name: str) -> str:
    """去掉 city 前缀和括号注释，保留主体用于公共前缀比较"""
    n = (name or "").strip()
    n = _PAREN_RE.sub("", n).strip()
    for c in _CITY_PREFIXES:
        if n.startswith(c):
            n = n[len(c):].strip()
            break
    return n


def _is_same_venue_branch(name_a: str, name_b: str) -> bool:
    """判断 a / b 是否同一景点的不同 POI 或同一品牌的不同分店。

    判定标准（满足任一即视为重复）：
      ① stem 互为子串
      ② 剥离 city/括号后的公共起始前缀 ≥ 3 字（覆盖"金沙遗址博物馆乌木林""小龙坎老火锅"等）
    """
    if not name_a or not name_b or name_a == name_b:
        return False
    stem_a, stem_b = _venue_stem(name_a), _venue_stem(name_b)
    if stem_a and stem_b:
        short, long = (stem_a, stem_b) if len(stem_a) <= len(stem_b) else (stem_b, stem_a)
        if len(short) >= 3 and short in long:
            return True
    # 公共前缀检查
    core_a, core_b = _strip_city_and_parens(name_a), _strip_city_and_parens(name_b)
    n = min(len(core_a), len(core_b))
    i = 0
    while i < n and core_a[i] == core_b[i]:
        i += 1
    return i >= 3


_HOTEL_KW_IN_NAME = ("酒店", "宾馆", "客栈", "民宿", "旅馆", "公寓", "山庄", "度假村")


def _hint_category_from_name(name: str) -> Optional[PlaceCategory]:
    """名称里有酒店/宾馆等关键词时强制归类 HOTEL，纠正 Amap 类型误判"""
    if not name:
        return None
    if any(kw in name for kw in _HOTEL_KW_IN_NAME):
        return PlaceCategory.HOTEL
    return None

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
        # 名称含「酒店/宾馆/客栈/民宿…」时强制归 HOTEL，纠正 Amap 类型误判
        name_hint = _hint_category_from_name(raw.get("name", ""))
        if name_hint:
            category = name_hint
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
                "你是专业旅行顾问，请为用户生成在【%s】%d天旅行的高德地图POI搜索关键词。\n\n"
                "硬性要求：\n"
                "- 景点：%d个，**每个必须指向不同的具体景点**（5A景区/地标/博物馆/公园/历史街区/网红打卡地），禁止用"
                "「热门景点」「必游景区」之类的泛指词，要直接给景点名（如「武侯祠」「锦里」「青城山」）。\n"
                "- 美食：%d个，**每个必须指向不同的菜系或品牌**（火锅/串串/小吃/茶馆/老字号），禁止两个关键词指向同一品牌的不同分店。\n"
                "- 住宿：%d个，给具体品牌（如「成都太古里东方文华」「成都金牛宾馆」），禁止用「四星酒店」泛指。\n\n"
                "再次强调：关键词之间必须互不重复、互不归属同一商家/景点。\n"
                '仅返回JSON数组，格式：[{"keyword":"...","type":"attraction"},{"keyword":"...","type":"food"},{"keyword":"...","type":"hotel"}]'
            ) % (city, trip_days, n_attract, n_food, n_hotel)
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
    seen_brands: set[tuple[str, str]] = set()   # (ptype, normalized_brand) → 同品牌只保留 1 家

    # 子类型 → 校验函数：避免酒店被当美食/餐厅被当景点
    _ptype_to_cat = {
        "attraction": PlaceCategory.ATTRACTION,
        "food": PlaceCategory.FOOD,
        "hotel": PlaceCategory.HOTEL,
    }

    async def fetch_one(kw: str, ptype: str, delay: float) -> list[Place]:
        await asyncio.sleep(delay)
        # 抓更宽松的池（remaining + 4）以便后续去重/品类过滤后仍有冗余
        results = await _fetch_amap_poi(kw, city, limit=type_limit.get(ptype, 3) + 4)
        return results

    tasks = [fetch_one(q["keyword"], q.get("type", "attraction"), i * 0.3)
             for i, q in enumerate(queries)]
    results = await asyncio.gather(*tasks)

    kept_names_by_type: dict[str, list[str]] = {"attraction": [], "food": [], "hotel": []}

    for batch, q in zip(results, queries):
        ptype = q.get("type", "attraction")
        expected_cat = _ptype_to_cat.get(ptype)
        # batch 内按评分降序，确保同景点多入口时保留评分最高的那个
        batch_sorted = sorted(batch, key=lambda p: (p.amap_rating or 0), reverse=True)
        for place in batch_sorted:
            if place.place_id in seen_ids:
                continue
            # ① 类目校验：keyword 标注是 food 但返回酒店 → 跳过
            if expected_cat is not None and place.category != expected_cat:
                continue
            # ② 品牌/景点主名去重：陈麻婆豆腐(南门店/总店/旗舰店) 只保留评分最高
            brand_key = (ptype, _normalize_brand(place.name))
            if brand_key in seen_brands:
                continue
            # ③ 同景点多入口/分馆去重：跨 batch 比对，"基地" vs "基地游客中心/办公楼/山月馆" 视为同一处
            if any(_is_same_venue_branch(kept, place.name) for kept in kept_names_by_type[ptype]):
                continue
            if type_count.get(ptype, 0) >= type_limit.get(ptype, 3):
                continue
            seen_ids.add(place.place_id)
            seen_brands.add(brand_key)
            kept_names_by_type[ptype].append(place.name)
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
