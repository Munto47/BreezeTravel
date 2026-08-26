"""
测试 Optimizer 核心算法

验证 K-Means 聚类 + TSP 排线的正确性。
不依赖外部 API（Amap/天气），完全离线可运行。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.schemas.place import Place, Coordinates, PlaceCategory, PlaceSource


# ===== 测试地点 Fixture =====

def make_place(place_id: str, name: str, lng: float, lat: float,
               category: PlaceCategory = PlaceCategory.ATTRACTION) -> Place:
    return Place(
        place_id=place_id,
        name=name,
        category=category,
        address=f"{name}地址",
        coords=Coordinates(lng=lng, lat=lat),
        city="成都",
        source=PlaceSource.AMAP_POI,
        estimated_duration=120,
    )


# 成都真实坐标（6 个地点）
CHENGDU_PLACES = [
    make_place("P001", "宽窄巷子",   104.0534, 30.6711),
    make_place("P002", "武侯祠",     104.0468, 30.6421),
    make_place("P003", "锦里古街",   104.0483, 30.6398),
    make_place("P004", "大熊猫基地", 104.1496, 30.7373),
    make_place("P005", "都江堰",     103.6171, 31.0044),
    make_place("P006", "东郊记忆",   104.1137, 30.6538),
]


# ===== 算法单元测试 =====

class TestHaversine:
    def test_same_point_distance_zero(self):
        from app.agents.nodes.optimizer import _haversine_km
        place = make_place("X", "X", 104.0, 30.0)
        assert _haversine_km(place, place) == pytest.approx(0.0, abs=0.01)

    def test_known_distance(self):
        """成都市中心到大熊猫基地约 12-15 km"""
        from app.agents.nodes.optimizer import _haversine_km
        center = make_place("A", "市中心", 104.0534, 30.6711)
        panda  = make_place("B", "熊猫基地", 104.1496, 30.7373)
        dist = _haversine_km(center, panda)
        assert 10 < dist < 20, f"距离估算异常：{dist:.2f} km"

    def test_distance_symmetry(self):
        from app.agents.nodes.optimizer import _haversine_km
        a = make_place("A", "A", 104.0, 30.0)
        b = make_place("B", "B", 104.1, 30.1)
        assert _haversine_km(a, b) == pytest.approx(_haversine_km(b, a), rel=1e-6)


class TestEstimateDriving:
    def test_returns_positive_values(self):
        from app.agents.nodes.optimizer import _estimate_driving
        a = make_place("A", "A", 104.0, 30.0)
        b = make_place("B", "B", 104.1, 30.1)
        mins, km = _estimate_driving(a, b)
        assert mins > 0
        assert km > 0

    def test_minimum_duration_is_5_minutes(self):
        """极近距离最少5分钟（交通时间下界）"""
        from app.agents.nodes.optimizer import _estimate_driving
        a = make_place("A", "A", 104.0000, 30.0000)
        b = make_place("B", "B", 104.0001, 30.0001)
        mins, _ = _estimate_driving(a, b)
        assert mins >= 5


class TestKMeansClustering:
    def test_clusters_equal_to_trip_days(self):
        from app.agents.nodes.optimizer import _kmeans_cluster
        clustered = _kmeans_cluster(CHENGDU_PLACES, trip_days=3)
        cluster_ids = set(p.cluster_id for p in clustered)
        assert len(cluster_ids) == 3, f"期望 3 个簇，实际 {len(cluster_ids)} 个"

    def test_all_places_assigned(self):
        from app.agents.nodes.optimizer import _kmeans_cluster
        clustered = _kmeans_cluster(CHENGDU_PLACES, trip_days=3)
        assert all(p.cluster_id is not None for p in clustered)
        assert len(clustered) == len(CHENGDU_PLACES)

    def test_single_day_cluster(self):
        from app.agents.nodes.optimizer import _kmeans_cluster
        places = CHENGDU_PLACES[:3]
        clustered = _kmeans_cluster(places, trip_days=1)
        cluster_ids = set(p.cluster_id for p in clustered)
        assert len(cluster_ids) == 1

    def test_fewer_places_than_days(self):
        """地点数 < 天数时，簇数应等于地点数"""
        from app.agents.nodes.optimizer import _kmeans_cluster
        two_places = CHENGDU_PLACES[:2]
        clustered = _kmeans_cluster(two_places, trip_days=5)
        cluster_ids = set(p.cluster_id for p in clustered)
        assert len(cluster_ids) == 2


class TestNearestNeighborTSP:
    def test_all_places_in_result(self):
        from app.agents.nodes.optimizer import _nearest_neighbor_tsp
        matrix = {}  # 空矩阵，使用默认时间
        ordered = _nearest_neighbor_tsp(CHENGDU_PLACES[:4], matrix)
        assert len(ordered) == 4

    def test_visit_orders_unique_and_complete(self):
        from app.agents.nodes.optimizer import _nearest_neighbor_tsp
        matrix = {}
        ordered = _nearest_neighbor_tsp(CHENGDU_PLACES[:4], matrix)
        orders = [p.visit_order for p in ordered]
        assert sorted(orders) == list(range(4)), f"visit_order 不连续: {orders}"

    def test_single_place(self):
        from app.agents.nodes.optimizer import _nearest_neighbor_tsp
        result = _nearest_neighbor_tsp([CHENGDU_PLACES[0]], {})
        assert len(result) == 1
        assert result[0].visit_order == 0


class TestTimeSlotGeneration:
    """验证时间表生成逻辑"""

    def test_slots_start_at_9am(self):
        from app.agents.nodes.optimizer import _generate_time_slots, _nearest_neighbor_tsp
        ordered = _nearest_neighbor_tsp(CHENGDU_PLACES[:3], {})
        slots = _generate_time_slots(ordered, {})
        # 第一个 slot 应从 09:00 开始
        first_slot = min(slots, key=lambda s: s.start_time)
        assert first_slot.start_time == "09:00"

    def test_slot_count_matches_places(self):
        from app.agents.nodes.optimizer import _generate_time_slots, _nearest_neighbor_tsp
        ordered = _nearest_neighbor_tsp(CHENGDU_PLACES[:4], {})
        slots = _generate_time_slots(ordered, {})
        # 过滤 optimizer 自动插入的虚拟用餐占位槽（__meal_*__）后计数
        real_slots = [s for s in slots if not s.place_id.startswith("__meal_")]
        assert len(real_slots) == 4

    def test_last_slot_has_no_transport(self):
        from app.agents.nodes.optimizer import _generate_time_slots, _nearest_neighbor_tsp
        ordered = _nearest_neighbor_tsp(CHENGDU_PLACES[:3], {})
        slots = _generate_time_slots(ordered, {})
        last_slot = max(slots, key=lambda s: s.start_time)
        assert last_slot.transport is None, "最后一个地点不应有交通段"

    def test_intermediate_slots_have_transport(self):
        from app.agents.nodes.optimizer import _generate_time_slots, _nearest_neighbor_tsp
        ordered = _nearest_neighbor_tsp(CHENGDU_PLACES[:3], {})
        slots = _generate_time_slots(ordered, {})
        # 虚拟用餐占位槽（__meal_*__）没有出行段，只检查真实地点
        real_slots = [s for s in slots if not s.place_id.startswith("__meal_")]
        intermediate = sorted(real_slots, key=lambda s: s.start_time)[:-1]
        for slot in intermediate:
            assert slot.transport is not None, f"中间站 {slot.place_id} 缺少交通段"
            assert slot.transport.duration_mins > 0


class TestOptimizerFullRun:
    """端到端测试（离线，使用估算距离）"""

    def test_optimizer_returns_correct_day_count(self):
        async def _run():
            from app.agents.nodes.optimizer import run
            itinerary = await run(
                places=CHENGDU_PLACES,
                trip_days=3,
                thread_id="test-thread-001",
            )
            return itinerary

        itinerary = asyncio.run(_run())
        assert len(itinerary.days) == 3, f"期望 3 天，实际 {len(itinerary.days)} 天"

    def test_optimizer_all_places_scheduled(self):
        async def _run():
            from app.agents.nodes.optimizer import run
            return await run(
                places=CHENGDU_PLACES,
                trip_days=2,
                thread_id="test-thread-002",
            )

        itinerary = asyncio.run(_run())
        # 过滤虚拟用餐占位槽（__meal_*__），只统计真实地点 slot
        total_slots = sum(
            len([s for s in d.slots if not s.place_id.startswith("__meal_")])
            for d in itinerary.days
        )
        assert total_slots == len(CHENGDU_PLACES), (
            f"期望 {len(CHENGDU_PLACES)} 个真实地点 slot，实际 {total_slots}"
        )

    def test_optimizer_itinerary_fields(self):
        async def _run():
            from app.agents.nodes.optimizer import run
            return await run(
                places=CHENGDU_PLACES[:3],
                trip_days=1,
                thread_id="test-thread-003",
            )

        itinerary = asyncio.run(_run())
        assert itinerary.itinerary_id
        assert itinerary.thread_id == "test-thread-003"
        assert itinerary.city == "成都"
        assert itinerary.version == 1
        assert itinerary.generated_at

    def test_optimizer_with_start_date(self):
        """带 start_date 参数时应正常运行（即使天气 API 无 key）"""
        async def _run():
            from app.agents.nodes.optimizer import run
            return await run(
                places=CHENGDU_PLACES[:4],
                trip_days=2,
                thread_id="test-thread-004",
                start_date="2026-06-01",
            )

        itinerary = asyncio.run(_run())
        assert itinerary is not None
        # 日期字段应该被填充
        for day in itinerary.days:
            assert day.date is not None


class TestFoodPlaceDuration:
    """餐饮地点的默认游览时长应为 60 分钟"""

    def test_food_duration(self):
        from app.agents.nodes.optimizer import DEFAULT_DURATION
        from app.schemas.place import PlaceCategory
        assert DEFAULT_DURATION[PlaceCategory.FOOD] == 60

    def test_attraction_duration(self):
        from app.agents.nodes.optimizer import DEFAULT_DURATION
        from app.schemas.place import PlaceCategory
        assert DEFAULT_DURATION[PlaceCategory.ATTRACTION] == 120
