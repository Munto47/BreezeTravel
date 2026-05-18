"""
Optimizer 节点：K-Means 聚类 + 高德驾车时间 + TSP 排线 + 和风天气

此节点不在主 chat 图中，通过 POST /api/optimize 独立触发。

算法流程：
1. K-Means（sklearn）按经纬度聚类为 trip_days 个簇
2. 高德驾车 API 构建时间矩阵（N≤6 时调用真实 API，否则直线 × 1.3 系数估算）
3. 最近邻 TSP 在每个簇内按最短驾车时间排序
4. 时间表生成（09:00 起，累加游览时长 + 真实交通时间）
5. 和风天气（出发日期在未来 3 天内时填充 weather_summary）
"""

import asyncio
import math
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import aiohttp
import numpy as np
from sklearn.cluster import KMeans

from app.schemas.place import Place, PlaceCategory
from app.schemas.itinerary import Itinerary, DayPlan, TimeSlot, TransportLeg, WeatherInfo
from app.config import settings

# ===== 常量 =====

DEFAULT_DURATION = {
    PlaceCategory.ATTRACTION: 120,
    PlaceCategory.FOOD: 60,
    PlaceCategory.HOTEL: 30,
    PlaceCategory.TRANSPORT: 15,
}
DEFAULT_TRANSPORT_MINS = 20

WEATHER_SUGGESTIONS = {
    "晴": "天气晴好，建议带防晒霜和水",
    "多云": "天气舒适，适合全天户外游览",
    "阴": "天气阴凉，无需防晒，可放心游览",
    "小雨": "有小雨，建议携带雨伞",
    "中雨": "有中雨，注意路面防滑",
    "大雨": "有大雨，优先安排室内景点",
    "雨": "有降雨，建议携带雨具",
    "雪": "有降雪，注意保暖防滑",
    "雷": "有雷阵雨，避免开阔区域",
}

# ===== Redis 懒初始化 =====

_redis = None


async def _get_redis():
    global _redis
    if _redis is None:
        try:
            import redis.asyncio as aioredis
            _redis = aioredis.from_url(
                settings.redis_url, decode_responses=True, socket_connect_timeout=2
            )
            await _redis.ping()
        except Exception as e:
            print(f"[Optimizer] Redis 连接失败，跳过缓存：{e}")
            _redis = None
    return _redis


# ===== 距离/时间工具 =====

def _haversine_km(a: Place, b: Place) -> float:
    """球面距离（km）"""
    R = 6371.0
    lat1 = math.radians(a.coords.lat)
    lat2 = math.radians(b.coords.lat)
    dlat = math.radians(b.coords.lat - a.coords.lat)
    dlng = math.radians(b.coords.lng - a.coords.lng)
    x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(x))


def _estimate_driving(a: Place, b: Place) -> tuple[int, float]:
    """直线距离 × 1.3（道路曲折系数）估算驾车时间和距离"""
    dist_km = _haversine_km(a, b) * 1.3
    duration_mins = max(5, int(dist_km / 30 * 60))  # 30 km/h 城市均速
    return duration_mins, round(dist_km, 1)


async def _fetch_amap_driving(
    session: aiohttp.ClientSession, a: Place, b: Place
) -> tuple[int, float]:
    """高德驾车路线 API；失败时降级到直线估算"""
    try:
        async with session.get(
            "https://restapi.amap.com/v3/direction/driving",
            params={
                "key": settings.amap_api_key,
                "origin": f"{a.coords.lng},{a.coords.lat}",
                "destination": f"{b.coords.lng},{b.coords.lat}",
                "output": "json",
            },
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            data = await resp.json()
            if data.get("status") == "1":
                paths = data.get("route", {}).get("paths", [])
                if paths:
                    duration_mins = int(paths[0].get("duration", 1200)) // 60
                    distance_km = round(int(paths[0].get("distance", 5000)) / 1000, 1)
                    return max(1, duration_mins), distance_km
    except Exception as e:
        print(f"[Optimizer] 高德驾车 API 失败（{a.name}→{b.name}）：{e}")
    return _estimate_driving(a, b)


async def _get_driving_cached(
    session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, a: Place, b: Place
) -> tuple[int, float]:
    """带 Redis 缓存（TTL 24h）的驾车查询"""
    ids = sorted([a.place_id, b.place_id])
    cache_key = f"dist:{ids[0]}:{ids[1]}"

    redis = await _get_redis()
    if redis:
        try:
            cached = await redis.get(cache_key)
            if cached:
                parts = cached.split(",")
                return int(parts[0]), float(parts[1])
        except Exception:
            pass

    async with semaphore:
        result = await _fetch_amap_driving(session, a, b)

    if redis:
        try:
            await redis.setex(cache_key, 86400, f"{result[0]},{result[1]}")
        except Exception:
            pass

    return result


# ===== 时间矩阵 =====

async def _build_time_matrix(
    session: aiohttp.ClientSession, places: list[Place]
) -> dict[tuple[str, str], tuple[int, float]]:
    """构建 (place_id_a, place_id_b) → (duration_mins, distance_km) 矩阵

    始终使用高德驾车 API（有 key 时），顺序请求避免并发限制。
    无 key 或 mock 模式时降级为直线距离估算。
    """
    n = len(places)
    use_amap = not settings.amap_mock and bool(settings.amap_api_key)

    matrix: dict[tuple[str, str], tuple[int, float]] = {}
    pairs = [(places[i], places[j]) for i in range(n) for j in range(i + 1, n)]

    if use_amap:
        # 并发请求（Semaphore=3），在高德 QPS 限制内提升吞吐
        # 15 个地点时耗时从 ~30s 降至 ~10s
        semaphore = asyncio.Semaphore(3)

        async def _fetch_pair(a: Place, b: Place):
            try:
                return (a, b, await _get_driving_cached(session, semaphore, a, b))
            except Exception:
                return (a, b, _estimate_driving(a, b))

        results = await asyncio.gather(*[_fetch_pair(a, b) for a, b in pairs])
        for a, b, result in results:
            matrix[(a.place_id, b.place_id)] = result
            matrix[(b.place_id, a.place_id)] = result
    else:
        for a, b in pairs:
            result = _estimate_driving(a, b)
            matrix[(a.place_id, b.place_id)] = result
            matrix[(b.place_id, a.place_id)] = result

    return matrix


# ===== K-Means 聚类 + 溢出重新分配 =====

def _centroid(places_in_cluster: list[Place]) -> tuple[float, float]:
    """计算簇的经纬度质心"""
    lngs = [p.coords.lng for p in places_in_cluster]
    lats = [p.coords.lat for p in places_in_cluster]
    return sum(lngs) / len(lngs), sum(lats) / len(lats)


def _dist2d(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """欧氏距离（聚类内部比较用，不需要球面精度）"""
    return math.sqrt((lng1 - lng2) ** 2 + (lat1 - lat2) ** 2)


def _kmeans_cluster(places: list[Place], trip_days: int) -> list[Place]:
    n = len(places)
    k = min(trip_days, n)

    # 地点数 <= 天数：每个地点单独一天
    if n <= trip_days:
        return [p.model_copy(update={"cluster_id": i}) for i, p in enumerate(places)]

    # --- 第一步：K-Means 初聚类 ---
    coords = np.array([[p.coords.lng, p.coords.lat] for p in places])
    kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = kmeans.fit_predict(coords).tolist()

    # 用 dict 维护簇成员，方便增删
    clusters: dict[int, list[int]] = {i: [] for i in range(k)}
    for idx, label in enumerate(labels):
        clusters[label].append(idx)

    cap_max = math.ceil(n / k) + 1  # 单日上限

    # --- 第二步：处理空簇（K-Means 偶尔会产生空簇）---
    # 全局质心作为空簇的临时参考点
    global_lng = float(np.mean(coords[:, 0]))
    global_lat = float(np.mean(coords[:, 1]))

    for cid in range(k):
        if len(clusters[cid]) != 0:
            continue
        # 找当前最大的簇，从中摘出距离空簇"全局质心"最近的地点
        donor = max(clusters, key=lambda c: len(clusters[c]))
        if not clusters[donor]:
            continue
        nearest_idx = min(
            clusters[donor],
            key=lambda i: _dist2d(coords[i][0], coords[i][1], global_lng, global_lat),
        )
        clusters[donor].remove(nearest_idx)
        clusters[cid].append(nearest_idx)

    # --- 第三步：溢出重新分配 ---
    max_iterations = n * k  # 防御性上限，理论上不会到达
    for _ in range(max_iterations):
        overfull = [c for c in range(k) if len(clusters[c]) > cap_max]
        if not overfull:
            break

        # 取最大的溢出簇
        src = max(overfull, key=lambda c: len(clusters[c]))
        src_places = clusters[src]

        # 计算 src 质心
        src_lng, src_lat = _centroid([places[i] for i in src_places])

        # 找出距离 src 质心最远的地点（候选迁出）
        evict_idx = max(
            src_places,
            key=lambda i: _dist2d(coords[i][0], coords[i][1], src_lng, src_lat),
        )
        evict_lng, evict_lat = coords[evict_idx][0], coords[evict_idx][1]

        # 在所有未满的簇中，找质心距离被迁出地点最近的目标簇
        underfull = [c for c in range(k) if len(clusters[c]) < cap_max and c != src]
        if not underfull:
            # 所有簇都满了，改为找最小的簇（放宽条件）
            underfull = [c for c in range(k) if c != src]
        if not underfull:
            break

        dst = min(
            underfull,
            key=lambda c: _dist2d(
                *_centroid([places[i] for i in clusters[c]]),
                evict_lng,
                evict_lat,
            ) if clusters[c] else _dist2d(global_lng, global_lat, evict_lng, evict_lat),
        )

        # 执行迁移
        clusters[src].remove(evict_idx)
        clusters[dst].append(evict_idx)

    # --- 第四步：写回 cluster_id ---
    result = list(places)  # 保持原顺序，下面逐个更新
    for cid, member_indices in clusters.items():
        for idx in member_indices:
            result[idx] = places[idx].model_copy(update={"cluster_id": cid})
    return result


# ===== 酒店挂载工具 =====

def _time_str_to_mins(t: str) -> int:
    """'HH:MM' → 分钟数"""
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _match_hotel(last_activity: Place, available_hotels: list[Place]) -> Optional[Place]:
    """从酒店池中找距 last_activity 最近的酒店，原地移除并返回；池空则返回 None"""
    if not available_hotels:
        return None
    hotel = min(available_hotels, key=lambda h: _haversine_km(last_activity, h))
    available_hotels.remove(hotel)
    return hotel


# ===== TSP =====

def _nearest_neighbor_tsp(
    places: list[Place],
    time_matrix: dict[tuple[str, str], tuple[int, float]],
) -> list[Place]:
    if len(places) <= 1:
        return [p.model_copy(update={"visit_order": i}) for i, p in enumerate(places)]

    n = len(places)
    visited = [False] * n
    path = [0]
    visited[0] = True

    for _ in range(n - 1):
        last = path[-1]
        nearest = min(
            (i for i in range(n) if not visited[i]),
            key=lambda i: time_matrix.get(
                (places[last].place_id, places[i].place_id),
                (DEFAULT_TRANSPORT_MINS, 10.0),
            )[0],
        )
        path.append(nearest)
        visited[nearest] = True

    result = [None] * n
    for order, idx in enumerate(path):
        result[idx] = places[idx].model_copy(update={"visit_order": order})
    return result  # type: ignore[return-value]


# ===== 时间表生成 =====

def _generate_time_slots(
    places: list[Place],
    time_matrix: dict[tuple[str, str], tuple[int, float]],
) -> list[TimeSlot]:
    slots = []
    current_mins = 9 * 60  # 09:00

    sorted_places = sorted(places, key=lambda p: p.visit_order or 0)

    for i, place in enumerate(sorted_places):
        duration = place.estimated_duration or DEFAULT_DURATION.get(place.category, 90)
        start_str = f"{current_mins // 60:02d}:{current_mins % 60:02d}"
        end_mins = current_mins + duration
        end_str = f"{end_mins // 60:02d}:{end_mins % 60:02d}"

        transport = None
        if i < len(sorted_places) - 1:
            next_place = sorted_places[i + 1]
            key = (place.place_id, next_place.place_id)
            dur_mins, dist_km = time_matrix.get(key, _estimate_driving(place, next_place))
            transport = TransportLeg(
                mode="driving",
                duration_mins=dur_mins,
                distance_km=dist_km,
            )
            current_mins = end_mins + dur_mins
        else:
            current_mins = end_mins

        slots.append(TimeSlot(
            place_id=place.place_id,
            place=place.model_dump(),
            start_time=start_str,
            end_time=end_str,
            transport=transport,
        ))

    return slots


# ===== 天气 =====

def _weather_suggestion(condition: str) -> str:
    for key, suggestion in WEATHER_SUGGESTIONS.items():
        if key in condition:
            return suggestion
    return "注意查看出发前天气预报"


async def _fetch_weather(
    session: aiohttp.ClientSession, lat: float, lng: float, day_offset: int
) -> Optional[WeatherInfo]:
    if not settings.qweather_api_key or day_offset > 2:
        return None
    try:
        async with session.get(
            "https://devapi.qweather.com/v7/weather/3d",
            params={"location": f"{lng},{lat}", "key": settings.qweather_api_key},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            data = await resp.json()
            if data.get("code") == "200":
                daily = data.get("daily", [])
                if day_offset < len(daily):
                    d = daily[day_offset]
                    return WeatherInfo(
                        condition=d.get("textDay", "晴"),
                        temp_high=int(d.get("tempMax", 25)),
                        temp_low=int(d.get("tempMin", 15)),
                        suggestion=_weather_suggestion(d.get("textDay", "晴")),
                    )
    except Exception as e:
        print(f"[Optimizer] 和风天气 API 失败：{e}")
    return None


# ===== 主入口 =====

async def run(
    places: list[Place],
    trip_days: int,
    thread_id: str,
    start_date: Optional[str] = None,
) -> Itinerary:
    """Optimizer 入口（直接调用，非 LangGraph 节点）"""
    async with aiohttp.ClientSession() as session:

        # ── 0. 数据分离 ──────────────────────────────────────────────────────
        hotels = [p for p in places if p.category == PlaceCategory.HOTEL]
        activities = [p for p in places if p.category != PlaceCategory.HOTEL]

        if not activities:
            raise ValueError("[Optimizer] 没有可排线的游玩地点（activities 为空）")

        # 酒店池：可变副本，每次 _match_hotel 调用后原地移除已分配酒店
        available_hotels: list[Place] = list(hotels)

        print(f"[Optimizer] 总地点={len(places)}，游玩={len(activities)}，酒店={len(hotels)}，天数={trip_days}")

        # ── 1. 仅对 activities 做 K-Means + 均匀分配 ─────────────────────────
        clustered = _kmeans_cluster(activities, trip_days)

        clusters: dict[int, list[Place]] = {}
        for p in clustered:
            cid = p.cluster_id or 0
            clusters.setdefault(cid, []).append(p)

        sorted_cluster_items = sorted(clusters.items())

        # ── 2. 全局质心（天气用）────────────────────────────────────────────
        center_lat = sum(p.coords.lat for p in activities) / len(activities)
        center_lng = sum(p.coords.lng for p in activities) / len(activities)

        today = date.today()
        weather_enabled = bool(settings.qweather_api_key) and start_date is not None

        day_plans: list[DayPlan] = []

        for day_index, (cluster_id, cluster_places) in enumerate(sorted_cluster_items):

            # ── 3a. 时间矩阵（只含游玩点）──────────────────────────────────
            time_matrix = await _build_time_matrix(session, cluster_places)

            # ── 3b. TSP 排线（只含游玩点）──────────────────────────────────
            ordered = _nearest_neighbor_tsp(cluster_places, time_matrix)

            # ── 3c. 生成游玩点时间表 ────────────────────────────────────────
            slots = _generate_time_slots(ordered, time_matrix)

            # ── 3d. 酒店挂载（Anchor Matching）─────────────────────────────
            if slots:
                sorted_ordered = sorted(ordered, key=lambda p: p.visit_order or 0)
                last_activity = sorted_ordered[-1]
                hotel = _match_hotel(last_activity, available_hotels)

                if hotel:
                    dur_mins, dist_km = _estimate_driving(last_activity, hotel)

                    # 把交通腿写入最后一个游玩 slot
                    slots[-1] = slots[-1].model_copy(update={
                        "transport": TransportLeg(
                            mode="driving",
                            duration_mins=dur_mins,
                            distance_km=dist_km,
                        )
                    })

                    # 计算酒店 check-in 时间
                    hotel_start_mins = _time_str_to_mins(slots[-1].end_time) + dur_mins
                    hotel_end_mins = hotel_start_mins + DEFAULT_DURATION[PlaceCategory.HOTEL]
                    hotel = hotel.model_copy(update={"cluster_id": day_index, "visit_order": len(slots)})

                    slots.append(TimeSlot(
                        place_id=hotel.place_id,
                        place=hotel.model_dump(),
                        start_time=f"{hotel_start_mins // 60:02d}:{hotel_start_mins % 60:02d}",
                        end_time=f"{hotel_end_mins // 60:02d}:{hotel_end_mins % 60:02d}",
                        transport=None,
                    ))

                    print(f"[Optimizer] Day {day_index + 1}：挂载酒店「{hotel.name}」")
                else:
                    print(f"[Optimizer] Day {day_index + 1}：酒店池已耗尽，跳过酒店挂载")

            # ── 3e. 天气 ────────────────────────────────────────────────────
            weather_summary: Optional[WeatherInfo] = None
            day_date_str: Optional[str] = None

            if start_date:
                try:
                    trip_start = date.fromisoformat(start_date)
                    day_date = trip_start + timedelta(days=day_index)
                    day_date_str = day_date.isoformat()
                    if weather_enabled:
                        offset = (day_date - today).days
                        if 0 <= offset <= 2:
                            weather_summary = await _fetch_weather(
                                session, center_lat, center_lng, offset
                            )
                except Exception as e:
                    print(f"[Optimizer] 日期/天气处理失败：{e}")

            day_plans.append(DayPlan(
                day_index=day_index,
                date=day_date_str,
                cluster_id=cluster_id,
                slots=slots,
                weather_summary=weather_summary,
            ))

        # 多余酒店：cluster_id=-1，不进任何 DayPlan
        if available_hotels:
            print(f"[Optimizer] 未分配酒店 {len(available_hotels)} 个：{[h.name for h in available_hotels]}")

        day_plans.sort(key=lambda d: d.day_index)
        city = activities[0].city if activities else "未知"

        return Itinerary(
            itinerary_id=str(uuid.uuid4()),
            thread_id=thread_id,
            city=city,
            days=day_plans,
            generated_at=datetime.now(timezone.utc).isoformat(),
            version=1,
        )
