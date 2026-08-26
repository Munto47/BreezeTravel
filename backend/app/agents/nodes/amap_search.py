"""
AmapSearch 节点：高德 POI 搜索

输入：state.query_rewrite（改写后的查询）, state.trip_city（房间目的地城市）
输出：state.amap_places（Place 列表）

Mock 模式（AMAP_MOCK=true）：
  从 backend/app/data/amap_mock_places.json 读取受控快照数据

真实模式（AMAP_MOCK=false）：
  调用高德搜索 POI 2.0 API: https://restapi.amap.com/v5/place/text
  缺少配置、提供方异常或空结果均显式返回失败/空结果，不读取 fixture
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from app.agents.state import AgentState
from app.config import settings
from app.schemas.place import (
    Coordinates,
    Place,
    PlaceCategory,
    PlaceSource,
    RetrievalExecutionMode,
)
from app.schemas.retrieval import RetrievalAudit
from app.constraints.location import (
    extract_district_constraint,
    extract_explicit_district_from_messages,
    filter_human_suitable_places,
    filter_places_by_district,
)
from app.constraints.recommendation_intent import (
    filter_places_for_request,
    rank_places_for_request,
)
from app.constraints.amap_types import classify_amap_type
from app.constraints.city_knowledge import provider_query_for_geo_anchor

MOCK_DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "amap_mock_places.json"

# 默认游览时长（分钟），按 category
DEFAULT_DURATION = {
    PlaceCategory.ATTRACTION: 120,
    PlaceCategory.FOOD: 60,
    PlaceCategory.HOTEL: 30,
    PlaceCategory.TRANSPORT: 15,
    PlaceCategory.UNKNOWN: 90,
}

_PRECISE_FOOD_KEYWORDS = (
    "北京菜", "烤鸭", "清真", "素食", "川菜", "湘菜", "粤菜",
    "本帮菜", "杭帮菜", "生煎", "小笼", "片儿川", "火锅", "烧烤",
    "日料", "咖啡", "茶馆", "甜品", "早餐", "夜宵",
)


class AmapSearchError(RuntimeError):
    """Fail-closed Amap error carrying a display-safe retrieval receipt."""

    def __init__(self, message: str, audit: RetrievalAudit):
        super().__init__(message)
        self.audit = audit


def _response_hash(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_amap_type(type_str: str, typecode: str = "") -> PlaceCategory:
    return classify_amap_type(typecode, type_str)


def _parse_amap_place(raw: dict, city: str) -> Optional[Place]:
    try:
        location = raw.get("location", "")
        if not location:
            return None
        lng_str, lat_str = location.split(",")

        # v5 把动态商业字段放在 business；保留 v3 biz_ext 兼容已有快照。
        business = raw.get("business")
        if not isinstance(business, dict):
            business = {}
        biz_ext = raw.get("biz_ext")
        if not isinstance(biz_ext, dict):
            biz_ext = {}

        rating_str = business.get("rating") or biz_ext.get("rating", "")
        price_str = business.get("cost") or biz_ext.get("cost", "")
        opening_hours = (
            business.get("opentime_today")
            or business.get("opentime_week")
            or raw.get("biz_opentime")
        )
        phone = business.get("tel") or raw.get("tel")
        provider_tags: list[str] = []
        for field in ("keytag", "rectag", "tag"):
            value = business.get(field)
            if not isinstance(value, str):
                continue
            provider_tags.extend(
                part.strip()
                for part in re.split(r"[,，;；|]", value)
                if part.strip()
            )

        photos = []
        raw_photos = raw.get("photos")
        if isinstance(raw_photos, list):
            photos = [p.get("url", "") for p in raw_photos[:3] if isinstance(p, dict) and p.get("url")]

        category = _parse_amap_type(raw.get("type", ""), raw.get("typecode", ""))
        return Place(
            place_id=raw.get("id", ""),
            name=raw.get("name", ""),
            category=category,
            address=raw.get("address", "") or "",
            coords=Coordinates(lng=float(lng_str), lat=float(lat_str)),
            city=city,
            district=raw.get("adname"),
            source=PlaceSource.AMAP_POI,
            tags=list(dict.fromkeys(provider_tags))[:20],
            execution_mode=RetrievalExecutionMode.LIVE,
            retrieval_provider="amap",
            amap_rating=float(rating_str) if rating_str and isinstance(rating_str, (int, float, str)) and str(rating_str).replace('.', '', 1).isdigit() else None,
            amap_price=float(price_str) if price_str and isinstance(price_str, (int, float, str)) and str(price_str).replace('.', '', 1).isdigit() else None,
            opening_hours=opening_hours if isinstance(opening_hours, str) else None,
            phone=phone if isinstance(phone, str) else None,
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
    location: str = "",
    radius_m: int = 0,
    typecodes: list[str] | None = None,
    administrative_area: str = "",
) -> tuple[list[Place], RetrievalAudit]:
    """调用高德 POI 搜索 API"""
    url = "https://restapi.amap.com/v5/place/around" if location else "https://restapi.amap.com/v5/place/text"
    params = {
        "key": settings.amap_api_key,
        "keywords": keywords,
        "region": administrative_area or city,
        "output": "json",
        "show_fields": "business,photos",
        "page_size": 10,
        "city_limit": "true",
    }
    # In Amap v5 around-search, a broad food type can dominate an exact cuisine
    # keyword (for example 湘菜) and rank generic nearby restaurants first.
    # Keep the precise keyword and rely on the closed parser/post-filter for
    # this narrow case; generic nearby searches still carry their typecode.
    effective_typecodes = _effective_typecodes(keywords, location, typecodes)
    if effective_typecodes:
        params["types"] = "|".join(effective_typecodes)
    if location:
        params["location"] = location
        params["radius"] = max(1, min(radius_m or 3000, 50000))
    # v5 文本检索默认按综合权重排序；周边检索才接受 sortrule。
    if prefer_trending and location:
        params["sortrule"] = "weight"
    request_hash = _response_hash({
        "method": "GET",
        "url": url,
        "params": {key: value for key, value in params.items() if key != "key"},
    })

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            response_hash = _response_hash(data)
            retrieved_at = datetime.now(timezone.utc)
            if data.get("status") != "1":
                audit = RetrievalAudit(
                    query=keywords,
                    city=city,
                    location=location or None,
                    radius_m=params.get("radius") if location else None,
                    typecodes=list(typecodes or []),
                    provider="amap",
                    execution_mode=RetrievalExecutionMode.LIVE,
                    retrieved_at=retrieved_at,
                    response_hash=response_hash,
                    result_count=0,
                    status="error",
                    fallback_reason=str(data.get("info") or "provider_status_not_ok"),
                )
                raise AmapSearchError("高德 POI 服务返回失败状态", audit)
            places = [_parse_amap_place(p, city) for p in (data.get("pois") or [])]
            parsed = [
                place.model_copy(update={
                    "retrieval_request_hash": request_hash,
                    "retrieval_response_hash": response_hash,
                    "retrieval_observed_at": retrieved_at,
                })
                for place in places
                if place is not None and place.category != PlaceCategory.UNKNOWN
            ]
            audit = RetrievalAudit(
                query=keywords,
                city=city,
                location=location or None,
                radius_m=params.get("radius") if location else None,
                typecodes=list(typecodes or []),
                provider="amap",
                execution_mode=RetrievalExecutionMode.LIVE,
                retrieved_at=retrieved_at,
                response_hash=response_hash,
                result_count=len(parsed),
                status="ok" if parsed else "empty",
            )
            return parsed, audit


def _effective_typecodes(
    keywords: str,
    location: str,
    typecodes: list[str] | None,
) -> list[str]:
    """Keep precise nearby cuisine searches keyword-led in Amap v5."""
    precise_food_around = (
        bool(location)
        and list(typecodes or []) == ["050000"]
        and any(term in keywords for term in _PRECISE_FOOD_KEYWORDS)
    )
    return [] if precise_food_around else list(typecodes or [])


_FOOD_KW    = {"美食", "吃", "餐", "火锅", "饭", "菜", "小吃", "饮食", "餐厅", "饭馆", "美味", "料理"}
_HOTEL_KW   = {"酒店", "住宿", "民宿", "旅馆", "客栈", "入住", "床位", "宾馆", "住一晚", "住哪里"}
_ATTRACT_KW = {"景点", "景区", "参观", "游览", "打卡", "观光", "博物馆", "公园", "古迹", "名胜", "寺庙", "好玩", "玩的地方"}

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


def _load_mock_places(city: str, query: str = "", district: str = "") -> list[Place]:
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
    fixture_hash = _response_hash(city_places)
    request_hash = _response_hash({
        "provider": "amap_fixture",
        "fixture_hash": fixture_hash,
        "city": city,
        "query": query,
        "district": district,
    })
    all_places = [
        Place(**p).model_copy(update={
            "execution_mode": RetrievalExecutionMode.FIXTURE,
            "retrieval_provider": "amap_fixture",
            "retrieval_request_hash": request_hash,
            "retrieval_response_hash": fixture_hash,
            "retrieval_observed_at": datetime.fromtimestamp(
                MOCK_DATA_PATH.stat().st_mtime, tz=timezone.utc,
            ),
        })
        for p in city_places
    ]

    # An explicit area is a hard constraint.  Empty is honest; falling back to
    # another district would produce a fluent but unusable recommendation.
    district = district or extract_district_constraint(query) or ""
    all_places = filter_human_suitable_places(filter_places_by_district(all_places, district))

    if not query:
        return rank_places_for_request(filter_places_for_request(all_places, query), query)

    q = query.lower()

    # 娱乐/酒吧等 Mock 数据不存在的类目 → 直接返回空，拒绝返回无关数据
    if any(kw in q for kw in _ENTERTAIN_KW):
        print(f"[AmapSearch] Mock 不含娱乐/酒吧类目，返回空（query={query[:40]}）")
        return []

    all_places = filter_places_for_request(all_places, query)

    want_food    = any(kw in q for kw in _FOOD_KW)
    want_hotel   = any(kw in q for kw in _HOTEL_KW)
    want_attract = any(kw in q for kw in _ATTRACT_KW)

    # 精确意图：只返回对应品类（不混入其他）
    if want_food and not want_hotel and not want_attract:
        matched = [p for p in all_places if p.category == PlaceCategory.FOOD]
        return rank_places_for_request(matched, query)

    if want_hotel and not want_food and not want_attract:
        matched = [p for p in all_places if p.category == PlaceCategory.HOTEL]
        return rank_places_for_request(matched, query)

    if want_attract and not want_food and not want_hotel:
        matched = [p for p in all_places if p.category == PlaceCategory.ATTRACTION]
        return rank_places_for_request(matched, query)

    # 混合意图或无明确意图：按品类优先排序返回全部
    priority = PlaceCategory.FOOD if want_food else (
        PlaceCategory.HOTEL if want_hotel else PlaceCategory.ATTRACTION
    )
    prioritized = [p for p in all_places if p.category == priority]
    others = [p for p in all_places if p.category != priority]
    return rank_places_for_request(
        filter_places_for_request(prioritized + others, query),
        query,
    )


def _normalize_entity_fixture_name(value: str) -> str:
    """Normalize only presentation punctuation for fixture identity lookup."""
    return re.sub(r"[\s·•\-—_（）()]+", "", (value or "").casefold())


def _load_mock_entity_candidates(city: str, query: str) -> list[Place]:
    """Replay the controlled POI fixture as an entity-search response.

    Entity resolution and recommendation ranking have different contracts.  A
    recommendation query may intentionally prefer a category or popularity,
    while an imported POI name must first surface identity-compatible rows.
    This adapter therefore selects rows solely from the explicit ``city`` and
    ``query`` input bytes.  It also retains strong name hits from other fixture
    cities so the resolver can reject them with an immutable wrong-city
    receipt; it never fabricates a cross-city candidate when the fixture has no
    matching row.
    """
    normalized_query = _normalize_entity_fixture_name(query)
    if not normalized_query or not MOCK_DATA_PATH.exists():
        return []

    with open(MOCK_DATA_PATH, "r", encoding="utf-8") as fixture_file:
        mock_data = json.load(fixture_file)

    matched_rows: list[dict] = []
    for fixture_city, rows in mock_data.items():
        if not isinstance(rows, list):
            continue
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            normalized_name = _normalize_entity_fixture_name(str(raw.get("name") or ""))
            if not normalized_name:
                continue
            if normalized_query not in normalized_name and normalized_name not in normalized_query:
                continue
            # Keep the city embedded in the fixture row authoritative.  The
            # top-level key is used only when an older fixture omitted it.
            matched_rows.append({**raw, "city": str(raw.get("city") or fixture_city)})

    if not matched_rows:
        return []

    matched_rows.sort(
        key=lambda raw: (
            0 if str(raw.get("city") or "") == city else 1,
            0
            if _normalize_entity_fixture_name(str(raw.get("name") or "")) == normalized_query
            else 1,
            str(raw.get("place_id") or ""),
        )
    )
    fixture_hash = _response_hash(mock_data)
    request_hash = _response_hash(
        {
            "provider": "amap_fixture_entity",
            "fixture_hash": fixture_hash,
            "target_city": city,
            "query": query,
        }
    )
    response_hash = _response_hash(
        {
            "fixture_hash": fixture_hash,
            "matches": matched_rows,
        }
    )
    observed_at = datetime.fromtimestamp(MOCK_DATA_PATH.stat().st_mtime, tz=timezone.utc)
    return [
        Place(**raw).model_copy(
            update={
                "execution_mode": RetrievalExecutionMode.FIXTURE,
                "retrieval_provider": "amap_fixture",
                "retrieval_request_hash": request_hash,
                "retrieval_response_hash": response_hash,
                "retrieval_observed_at": observed_at,
            }
        )
        for raw in matched_rows
    ]


async def run(state: AgentState) -> dict:
    """AmapSearch 节点入口函数"""
    query = state.get("query_rewrite") or ""
    city = _extract_city(state)
    district = (
        state.get("trip_district")
        or extract_district_constraint(query)
        or extract_explicit_district_from_messages(state.get("messages", []))
        or ""
    )
    ctx = state.get("working_context") or {}
    prefer_trending: bool = bool(ctx.get("prefer_trending", False))
    prefer_chain: bool = bool(ctx.get("prefer_chain", False))

    # 若偏好连锁品牌且关键词中未含"连锁"，追加修饰词
    if prefer_chain and "连锁" not in query:
        query = f"{query} 连锁".strip()

    # ``local_fixture`` is the compose-only development profile.  It permits
    # deterministic fixture reads while remaining distinct from ``local_real``
    # (which must use a configured live provider) and ``public``.
    fixture_allowed = settings.runtime_profile in {"demo", "test", "local_fixture"}
    if settings.amap_mock or settings.demo_mode:
        if not fixture_allowed:
            audit = RetrievalAudit(
                query=query,
                city=city,
                district=district or None,
                provider="amap_fixture",
                execution_mode=RetrievalExecutionMode.FIXTURE,
                retrieved_at=datetime.now(timezone.utc),
                result_count=0,
                fallback_reason="fixture_forbidden_for_runtime_profile",
                status="configuration_error",
            )
            raise AmapSearchError(
                f"runtime_profile={settings.runtime_profile} 禁止使用 AMAP_MOCK/DEMO fixture",
                audit,
            )
        places = _load_mock_places(city, query, district)
        if prefer_trending:
            places = sorted(places, key=lambda p: p.amap_rating or 0, reverse=True)
        print(f"[AmapSearch] Mock 模式，city={city}，district={district or '-'}，query={query!r}，返回 {len(places)} 个地点")
        audit = RetrievalAudit(
            query=query,
            city=city,
            district=district or None,
            provider="amap_fixture",
            execution_mode=RetrievalExecutionMode.FIXTURE,
            retrieved_at=datetime.now(timezone.utc),
            response_hash=places[0].retrieval_response_hash if places else _response_hash([]),
            result_count=len(places),
            status="ok" if places else "empty",
        )
        return {"amap_places": places, "retrieval_audits": [audit.model_dump(mode="json")]}

    # 真实高德 API 模式
    if not settings.amap_api_key:
        audit = RetrievalAudit(
            query=query,
            city=city,
            district=district or None,
            provider="amap",
            execution_mode=RetrievalExecutionMode.LIVE,
            retrieved_at=datetime.now(timezone.utc),
            response_hash=_response_hash({"error": "missing_api_key", "query": query, "city": city}),
            result_count=0,
            fallback_reason="missing_api_key",
            status="configuration_error",
        )
        raise AmapSearchError("未配置 AMAP_API_KEY，真实模式拒绝读取 fixture", audit)

    search_query = query
    search_anchor = str(state.get("search_anchor") or "").strip()
    search_radius_m = int(state.get("search_radius_m") or 0)
    search_typecodes = list(state.get("search_typecodes") or [])
    audits: list[RetrievalAudit] = []
    try:
        location = ""
        anchor_place_id = ""
        anchor_response_hash = None
        anchor_observed_at = None
        if search_anchor:
            anchor_query = provider_query_for_geo_anchor(city, search_anchor)
            anchor_places, anchor_audit = await _fetch_amap_poi(anchor_query, city)
            anchor_audit = anchor_audit.model_copy(update={"query": f"anchor:{search_anchor}"})
            audits.append(anchor_audit)
            if not anchor_places:
                raise AmapSearchError(
                    f"无法解析地理锚点：{search_anchor}",
                    anchor_audit.model_copy(update={"status": "empty", "fallback_reason": "anchor_not_found"}),
                )
            anchor_place_id = anchor_places[0].place_id
            location = f"{anchor_places[0].coords.lng},{anchor_places[0].coords.lat}"
            anchor_response_hash = anchor_audit.response_hash
            anchor_observed_at = anchor_audit.retrieved_at
        places, audit = await _fetch_amap_poi(
            search_query,
            city,
            prefer_trending=prefer_trending,
            prefer_chain=prefer_chain,
            location=location,
            radius_m=search_radius_m,
            typecodes=search_typecodes,
            administrative_area=district,
        )
        if search_anchor:
            audit = audit.model_copy(update={
                "anchor_place": search_anchor,
                "anchor_place_id": anchor_place_id or None,
                "anchor_location": location or None,
                "anchor_response_hash": anchor_response_hash,
                "anchor_observed_at": anchor_observed_at,
            })
        audits.append(audit)
    except AmapSearchError:
        raise
    except Exception as exc:
        audit = RetrievalAudit(
            query=search_query,
            city=city,
            district=district or None,
            provider="amap",
            execution_mode=RetrievalExecutionMode.LIVE,
            retrieved_at=datetime.now(timezone.utc),
            response_hash=_response_hash({
                "error": type(exc).__name__, "query": search_query, "city": city,
            }),
            result_count=0,
            fallback_reason=type(exc).__name__,
            status="error",
        )
        raise AmapSearchError("高德 API 调用异常，真实模式拒绝读取 fixture", audit) from exc

    places = filter_human_suitable_places(filter_places_by_district(places, district))
    places = rank_places_for_request(filter_places_for_request(places, query), query)
    audit = audit.model_copy(update={"district": district or None, "result_count": len(places)})

    print(f"[AmapSearch] city={city}, district={district or '-'}, query={query}, prefer_trending={prefer_trending}, 返回 {len(places)} 个地点")
    return {
        "amap_places": places,
        "retrieval_audits": [item.model_dump(mode="json") for item in audits],
    }
