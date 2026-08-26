"""PlannerAgent 子图测试（Phase 4）

验证多智能体调度：clusterer → distance → sequencer → scheduler → tips
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.schemas.itinerary import DayPlan, Itinerary
from app.schemas.place import Place, Coordinates, PlaceCategory, PlaceSource


def make_place(pid, name, lng, lat, category=PlaceCategory.ATTRACTION):
    return Place(
        place_id=pid,
        name=name,
        category=category,
        address=f"{name}地址",
        coords=Coordinates(lng=lng, lat=lat),
        city="成都",
        source=PlaceSource.AMAP_POI,
        estimated_duration=120,
    )


CHENGDU_PLACES = [
    make_place("P001", "宽窄巷子",   104.0534, 30.6711),
    make_place("P002", "武侯祠",     104.0468, 30.6421),
    make_place("P003", "锦里古街",   104.0483, 30.6398),
    make_place("P004", "大熊猫基地", 104.1496, 30.7373),
    make_place("P005", "都江堰",     103.6171, 31.0044),
    make_place("P006", "东郊记忆",   104.1137, 30.6538),
]

CHENGDU_HOTELS = [
    make_place("H001", "成都酒店A", 104.0500, 30.6700, PlaceCategory.HOTEL),
    make_place("H002", "成都酒店B", 104.1000, 30.7000, PlaceCategory.HOTEL),
]


# ===== 子图编译 =====

class TestPlannerGraphCompile:
    def test_graph_builds(self):
        from app.agents.planner.graph import build_planner_graph
        g = build_planner_graph()
        assert g is not None

    def test_graph_singleton_exists(self):
        from app.agents.planner.graph import _planner_graph
        assert _planner_graph is not None


@pytest.mark.asyncio
async def test_tips_agent_uses_final_repaired_itinerary_instead_of_reassembling_day_plans():
    from app.agents.planner.nodes import tips_agent

    repaired = Itinerary(
        itinerary_id="final-repaired",
        thread_id="tips-final",
        city="北京",
        days=[DayPlan(day_index=0, cluster_id=0, slots=[])],
        generated_at="2026-08-20T00:00:00+00:00",
        version=3,
    )
    with patch("app.agents.planner.nodes.tips_agent.generate_tips", AsyncMock(return_value=repaired)) as mocked:
        result = await tips_agent.run({
            "itinerary": repaired,
            "day_plans": [],
            "thread_id": "tips-final",
            "preferences_text": "少走路",
            "trace": ["[Verifier] SATISFIED"],
        })

    assert mocked.await_args.args[0] == repaired
    assert result["itinerary"].itinerary_id == "final-repaired"
    assert result["itinerary"].version == 3
    assert result["trace"][-2] == "[Verifier] SATISFIED"


# ===== 子节点单元测试 =====

class TestClustererNode:
    def test_separates_hotels_and_activities(self):
        from app.agents.planner.nodes.clusterer import run

        async def _run():
            return await run({
                "places": CHENGDU_PLACES + CHENGDU_HOTELS,
                "trip_days": 3,
            })

        out = asyncio.run(_run())
        assert len(out["activities"]) == len(CHENGDU_PLACES)
        assert len(out["hotels_pool"]) == len(CHENGDU_HOTELS)
        assert len(out["clusters"]) == 3
        assert "center_lat" in out and "center_lng" in out

    def test_raises_on_no_activities(self):
        from app.agents.planner.nodes.clusterer import run

        async def _run():
            return await run({"places": CHENGDU_HOTELS, "trip_days": 2})

        with pytest.raises(ValueError, match="没有可排线的游玩地点"):
            asyncio.run(_run())


class TestSequencerNode:
    def test_orders_each_cluster(self):
        from app.agents.planner.nodes.clusterer import run as clu_run
        from app.agents.planner.nodes.sequencer import run as seq_run

        async def _run():
            s = await clu_run({"places": CHENGDU_PLACES, "trip_days": 2})
            s["time_matrices"] = {}  # 跳过 distance，用空矩阵
            s["clusters"] = s["clusters"]
            out = await seq_run(s)
            return s, out

        s, out = asyncio.run(_run())
        orderings = out["orderings"]
        # 每个簇应当被排序
        assert set(orderings.keys()) == set(s["clusters"].keys())
        # 簇内地点数不变
        for cid, places in orderings.items():
            assert len(places) == len(s["clusters"][cid])
            # visit_order 应填充
            assert all(p.visit_order is not None for p in places)


# ===== 端到端子图测试 =====

class TestPlannerGraphEndToEnd:
    """v2: run_planner 返回 PlannerResult(itinerary, backup_pool, critic_violations)"""

    def _run_planner_sync(self, places, trip_days, thread_id):
        from app.agents.planner.graph import run_planner
        from unittest.mock import AsyncMock
        import app.agents.planner.nodes.critic_v2 as cv2

        async def patched_critic(state):
            return {"critic_violations": [], "trace": state.get("trace", [])}

        async def _run():
            with patch("app.agents.planner.nodes.scheduler_v2._load_place_meta", AsyncMock(return_value={})):
                with patch("app.agents.planner.nodes.weather_fetcher._fetch_qweather_7d", AsyncMock(return_value=[])):
                    orig = cv2.run
                    cv2.run = patched_critic
                    try:
                        return await run_planner(places=places, trip_days=trip_days, thread_id=thread_id)
                    finally:
                        cv2.run = orig

        return asyncio.run(_run())

    def test_full_pipeline_returns_itinerary(self):
        result = self._run_planner_sync(CHENGDU_PLACES, trip_days=3, thread_id="planner-test-001")
        itinerary = result.itinerary
        assert itinerary is not None
        assert len(itinerary.days) == 3
        assert itinerary.thread_id == "planner-test-001"
        assert itinerary.city == "成都"

    def test_returns_planner_result_fields(self):
        result = self._run_planner_sync(CHENGDU_PLACES, trip_days=2, thread_id="planner-test-002")
        assert hasattr(result, "itinerary")
        assert hasattr(result, "backup_pool")
        assert hasattr(result, "critic_violations")
        assert isinstance(result.backup_pool, list)

    def test_hotel_anchored_per_day(self):
        result = self._run_planner_sync(
            CHENGDU_PLACES + CHENGDU_HOTELS, trip_days=2, thread_id="planner-test-003"
        )
        itinerary = result.itinerary
        any_hotel_anchored = any(
            any(s.place.get("category") == "hotel" for s in d.slots)
            for d in itinerary.days
        )
        assert any_hotel_anchored, "酒店挂载未生效"

    def test_trace_captured(self):
        """验证子 Agent 的 trace 记录了调度顺序（可观测性）"""
        from app.agents.planner.graph import _planner_graph

        async def _run():
            initial = {
                "places": CHENGDU_PLACES[:4],
                "trip_days": 2,
                "thread_id": "planner-test-004",
                "preferences_text": "",
                "trace": [],
            }
            return await _planner_graph.ainvoke(initial)

        final = asyncio.run(_run())
        trace = final.get("trace", [])
        # 至少 4 个 Agent 各写了一条（tips 在无 API key 时也会跑）
        joined = "\n".join(trace)
        assert "Clusterer" in joined
        assert "Distance" in joined
        assert "Sequencer" in joined
        # v2 图：SchedulerV2 或 WeatherFetcher 均可
        assert ("SchedulerV2" in joined or "Scheduler" in joined or "WeatherFetcher" in joined)
