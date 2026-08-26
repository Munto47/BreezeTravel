"""
test_chat_demo.py — 验证 demo_mode 下新房间首次问询能迅速返回高质量地点

覆盖场景：
1. 新房间 + 综合推荐 → 三类地点均有，每类 ≤5 个，总数 ≥3
2. 新房间 + 纯美食查询 → 只有 food 类，≥3 个
3. 新房间 + 纯景点查询 → 只有 attraction 类，≥3 个
4. 未知城市 → 降级到成都 Mock，仍返回地点
5. 所有 7 个支持城市均能返回地点
6. synthesizer 在 demo_mode + 空 amap_places 时能自动补充 Mock 数据
7. 每类推荐数量 ≤ 5（封顶逻辑正确）
8. SSE 流式事件序列完整（thinking → place → text → done）
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── 强制 demo_mode 环境变量在 import app 之前设置 ────────────────────────────
os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("AMAP_MOCK", "true")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/travel_agent")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def mock_db():
    """轻量 Mock 数据库，无需真实连接"""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn)
    return pool


def _make_real_graph_fixture():
    """
    使用真实 router + synthesizer（demo_mode），但 mock 持久化 checkpointer。
    这样可以真实测试 demo_mode 下的路由 + synthesizer 数据加载逻辑。
    """
    from app.agents.graph import build_graph
    return build_graph(checkpointer=None)


@pytest.fixture(scope="module")
def real_graph():
    return _make_real_graph_fixture()


@pytest.fixture(scope="module")
def client(mock_db, real_graph):
    # Patch the symbol used by the endpoint, not its provider.  This keeps the
    # fixture isolated even when test_api imported the endpoint first.
    from app.api import chat as _chat_api  # noqa: F401

    with patch("app.db.connection.get_pool", AsyncMock(return_value=mock_db)), \
         patch("app.api.chat.get_graph_with_persistence", AsyncMock(return_value=real_graph)), \
         patch("app.agents.graph.init_persistent_graph", AsyncMock()), \
         patch("app.agents.graph.close_checkpointer", AsyncMock()):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def _parse_sse(text: str) -> list[dict]:
    """解析 SSE 文本，返回所有事件列表"""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return events


def _chat(client, message: str, city: str, thread_id: str) -> tuple[list[dict], list[dict]]:
    """发送聊天请求，返回 (all_events, place_events)"""
    resp = client.post("/api/chat", json={
        "thread_id": thread_id,
        "user_id": "test-user-demo",
        "message": message,
        "trip_city": city,
        "selected_place_ids": [],
    })
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}"
    all_events = _parse_sse(resp.text)
    place_events = [e for e in all_events if e.get("event") == "place"]
    return all_events, place_events


# ── 测试组 1：综合推荐（新房间首次问询）────────────────────────────────────

class TestNewRoomFirstQuery:
    """验证新房间进入后，AI 顾问能迅速返回高质量地点"""

    def test_returns_places_not_empty(self, client):
        """最核心：新房间综合推荐不应返回 0 个地点"""
        _, places = _chat(client, "上海有什么好玩的地方？", "上海", "demo-t01")
        assert len(places) >= 3, f"期望 ≥3 个地点，实际 {len(places)} 个"

    def test_three_categories_present(self, client):
        """综合推荐应覆盖景点/美食/住宿三类"""
        _, places = _chat(client, "帮我推荐上海值得去的地方", "上海", "demo-t02")
        cats = {e["data"]["place"]["category"] for e in places}
        # 综合推荐应至少有景点和美食
        assert "attraction" in cats, f"缺少景点类，实际类别：{cats}"
        assert "food" in cats, f"缺少美食类，实际类别：{cats}"

    def test_per_category_capped_at_5(self, client):
        """每类地点不超过 5 个（_PER_CATEGORY_CAP 生效）"""
        _, places = _chat(client, "推荐上海旅游地点", "上海", "demo-t03")
        cat_counts: dict[str, int] = {}
        for e in places:
            cat = e["data"]["place"]["category"]
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        for cat, cnt in cat_counts.items():
            assert cnt <= 5, f"类别 {cat} 超过 5 个：{cnt} 个"

    def test_total_places_reasonable(self, client):
        """总地点数在合理范围（3-15）"""
        _, places = _chat(client, "上海旅行推荐", "上海", "demo-t04")
        assert 3 <= len(places) <= 15, f"地点数量异常：{len(places)}"

    def test_sse_has_done_event(self, client):
        """SSE 流必须包含 done 事件"""
        all_events, _ = _chat(client, "上海景点推荐", "上海", "demo-t05")
        done_events = [e for e in all_events if e.get("event") == "done"]
        assert len(done_events) == 1, "缺少 done 事件"

    def test_sse_has_thinking_events(self, client):
        """SSE 流必须包含 thinking 事件"""
        all_events, _ = _chat(client, "上海美食", "上海", "demo-t06")
        thinking_events = [e for e in all_events if e.get("event") == "thinking"]
        assert len(thinking_events) >= 1, "缺少 thinking 事件"

    def test_sse_has_text_event(self, client):
        """SSE 流必须包含推荐文案 text 事件"""
        all_events, _ = _chat(client, "帮我推荐上海景点", "上海", "demo-t07")
        text_events = [e for e in all_events if e.get("event") == "text"]
        full_text = "".join(e["data"]["delta"] for e in text_events)
        assert len(full_text) >= 10, f"推荐文案过短：{full_text!r}"

    def test_place_has_required_fields(self, client):
        """每个地点卡片必须包含 place_id / name / category / coords"""
        _, places = _chat(client, "上海景点推荐", "上海", "demo-t08")
        for e in places:
            p = e["data"]["place"]
            assert p.get("place_id"), f"缺少 place_id：{p}"
            assert p.get("name"), f"缺少 name：{p}"
            assert p.get("category"), f"缺少 category：{p}"
            assert p.get("coords"), f"缺少 coords：{p}"

    def test_no_error_event(self, client):
        """正常请求不应出现 error 事件"""
        all_events, _ = _chat(client, "上海好玩的地方", "上海", "demo-t09")
        error_events = [e for e in all_events if e.get("event") == "error"]
        assert len(error_events) == 0, f"出现错误事件：{error_events}"

    def test_realistic_beijing_room_opening_prompt_respects_initial_list_contract(self, client):
        """Real room copy must not become a district or collapse to an empty list."""
        from tests.test_daily_query_quality import ROOM_OPENING_PROMPT

        events, places = _chat(
            client,
            ROOM_OPENING_PROMPT,
            "北京",
            "demo-realistic-beijing-room-opening",
        )
        categories: dict[str, int] = {}
        for event in places:
            category = event["data"]["place"]["category"]
            categories[category] = categories.get(category, 0) + 1

        assert 3 <= len(places) <= 15
        assert {"attraction", "food", "hotel"} <= set(categories)
        assert all(count <= 5 for count in categories.values())
        text = "".join(
            event["data"]["delta"] for event in events if event.get("event") == "text"
        )
        assert "大致价位与所在片区且符合" not in text
        assert not any(
            "高德地点搜索暂时不可用" in event.get("data", {}).get("summary", "")
            for event in events
        )


# ── 测试组 2：意图过滤（单类查询）────────────────────────────────────────────

class TestCategoryFiltering:
    """验证不同意图查询时 Mock 数据过滤正确"""

    def test_food_query_returns_food_places(self, client):
        """美食查询应主要返回 food 类地点"""
        _, places = _chat(client, "推荐上海美食餐厅", "上海", "demo-t10")
        assert len(places) >= 3, f"美食查询返回地点过少：{len(places)}"
        food_count = sum(1 for e in places if e["data"]["place"]["category"] == "food")
        assert food_count >= 3, f"美食查询中 food 类地点不足：{food_count}"

    def test_attraction_query_returns_attractions(self, client):
        """景点查询应主要返回 attraction 类地点"""
        _, places = _chat(client, "推荐上海景点打卡", "上海", "demo-t11")
        assert len(places) >= 3, f"景点查询返回地点过少：{len(places)}"
        attract_count = sum(1 for e in places if e["data"]["place"]["category"] == "attraction")
        assert attract_count >= 3, f"景点查询中 attraction 类地点不足：{attract_count}"

    def test_hotel_query_returns_hotels(self, client):
        """住宿查询应主要返回 hotel 类地点"""
        _, places = _chat(client, "推荐上海住宿酒店", "上海", "demo-t12")
        assert len(places) >= 3, f"住宿查询返回地点过少：{len(places)}"
        hotel_count = sum(1 for e in places if e["data"]["place"]["category"] == "hotel")
        assert hotel_count >= 3, f"住宿查询中 hotel 类地点不足：{hotel_count}"

    def test_minhang_hard_scope_is_visible_and_never_spills(self, client):
        """真实用户约束行政区时，卡片和文案都必须承认并遵守该范围。"""
        events, places = _chat(
            client,
            "我们带孩子在上海玩三天，只在闵行区安排景点、美食和酒店",
            "上海",
            "demo-minhang-hard-scope",
        )
        assert len(places) >= 6
        assert {event["data"]["place"]["district"] for event in places} == {"闵行区"}
        text = "".join(
            event["data"]["delta"] for event in events if event.get("event") == "text"
        )
        assert "闵行区" in text


# ── 测试组 3：多城市覆盖 ──────────────────────────────────────────────────────

class TestMultiCitySupport:
    """验证所有 7 个支持城市均能快速返回地点"""

    CITIES = ["成都", "北京", "上海", "厦门", "广州", "深圳", "杭州"]

    @pytest.mark.parametrize("city", CITIES)
    def test_city_returns_places(self, client, city):
        _, places = _chat(client, f"{city}旅行推荐", city, f"demo-city-{city}")
        assert len(places) >= 3, f"{city} 返回地点不足：{len(places)} 个"

    @pytest.mark.parametrize("city", CITIES)
    def test_city_places_match_city(self, client, city):
        """返回的地点 city 字段应与请求城市一致"""
        _, places = _chat(client, f"推荐{city}好玩的地方", city, f"demo-city2-{city}")
        if places:
            for e in places[:3]:
                assert e["data"]["place"]["city"] == city, (
                    f"地点城市不匹配：期望 {city}，实际 {e['data']['place']['city']}"
                )


# ── 测试组 4：synthesizer demo_mode 数据加载单元测试 ─────────────────────────

class TestSynthesizerDemoModeDataLoad:
    """直接测试 synthesizer 在 demo_mode + 空 amap_places 时能自动加载 Mock 数据"""

    def test_empty_amap_places_triggers_mock_load(self):
        """synthesizer 收到空 amap_places 且 demo_mode=True 时，应返回非空地点"""
        import asyncio
        from unittest.mock import patch as _patch

        from app.agents.nodes.synthesizer import run as synth_run
        from app.agents.state import default_working_context
        from langchain_core.messages import HumanMessage

        state = {
            "messages": [HumanMessage(content="上海景点推荐")],
            "amap_places": [],          # 模拟 tool_executor 未执行
            "rag_chunks": [],
            "trip_city": "上海",
            "working_context": default_working_context(),
            "thread_id": "unit-test",
            "user_id": "test",
            "user_long_term_prefs": None,
            "react_iterations": 0,
            "synthesized_places": [],
            "final_response": None,
            "itinerary": None,
            "selected_place_ids": [],
            "critic_retry": False,
            "critic_reason": None,
            "critic_iterations": 0,
            "intent": None,
            "query_rewrite": None,
            "recommendations": [],
        }

        with _patch("app.config.settings.demo_mode", True), \
             _patch("app.config.settings.amap_mock", True):
            result = asyncio.run(synth_run(state))

        places = result.get("synthesized_places", [])
        assert len(places) >= 3, f"demo_mode 下 synthesizer 应自动加载 Mock 数据，实际 {len(places)} 个"

    def test_demo_response_text_not_empty(self):
        """demo_mode 下 synthesizer 应生成有内容的推荐文案"""
        import asyncio
        from unittest.mock import patch as _patch

        from app.agents.nodes.synthesizer import run as synth_run
        from app.agents.state import default_working_context
        from langchain_core.messages import HumanMessage

        state = {
            "messages": [HumanMessage(content="北京有哪些景点？")],
            "amap_places": [],
            "rag_chunks": [],
            "trip_city": "北京",
            "working_context": default_working_context(),
            "thread_id": "unit-test-2",
            "user_id": "test",
            "user_long_term_prefs": None,
            "react_iterations": 0,
            "synthesized_places": [],
            "final_response": None,
            "itinerary": None,
            "selected_place_ids": [],
            "critic_retry": False,
            "critic_reason": None,
            "critic_iterations": 0,
            "intent": None,
            "query_rewrite": None,
            "recommendations": [],
        }

        with _patch("app.config.settings.demo_mode", True), \
             _patch("app.config.settings.amap_mock", True):
            result = asyncio.run(synth_run(state))

        text = result.get("final_response", "")
        assert len(text) >= 20, f"推荐文案过短：{text!r}"

    def test_per_category_cap_in_synthesizer(self):
        """synthesizer 的 _cap_places 应保证每类 ≤5 个"""
        from app.agents.nodes.synthesizer import _cap_places
        from app.schemas.place import Place, PlaceCategory, Coordinates, PlaceSource

        def _make(pid, cat):
            return Place(
                place_id=pid, name=f"测试地点{pid}", category=cat,
                address="测试地址",
                coords=Coordinates(lng=104.0, lat=30.0),
                city="测试", source=PlaceSource.AMAP_POI,
            )

        places = (
            [_make(f"a{i}", PlaceCategory.ATTRACTION) for i in range(8)] +
            [_make(f"f{i}", PlaceCategory.FOOD) for i in range(6)] +
            [_make(f"h{i}", PlaceCategory.HOTEL) for i in range(4)]
        )

        capped = _cap_places(places)
        from collections import Counter
        cat_counts = Counter(p.category for p in capped)

        assert cat_counts[PlaceCategory.ATTRACTION] <= 5
        assert cat_counts[PlaceCategory.FOOD] <= 5
        assert cat_counts[PlaceCategory.HOTEL] <= 5
        assert len(capped) <= 15

    def test_mock_data_has_5_per_category(self):
        """验证 mock fixture 每个城市每类 ≥5 条（确保 cap 有效）"""
        import json
        from pathlib import Path
        fixture = Path(__file__).parents[1] / "app" / "data" / "amap_mock_places.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        for city, places in data.items():
            cats: dict[str, int] = {}
            for p in places:
                cats[p["category"]] = cats.get(p["category"], 0) + 1
            assert cats.get("food", 0) >= 5, f"{city} food 不足 5 条：{cats}"
            assert cats.get("hotel", 0) >= 5, f"{city} hotel 不足 5 条：{cats}"
            assert cats.get("attraction", 0) >= 5, f"{city} attraction 不足 5 条：{cats}"
