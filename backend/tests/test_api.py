"""
API 集成测试（使用 FastAPI TestClient）

使用 DEMO_MODE=true + AMAP_MOCK=true 运行，无需真实外部服务。
数据库连接用 monkeypatch 替换为 mock，可在 CI/无 Docker 环境运行。
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient


# ===== Fixtures =====

@pytest.fixture(scope="session", autouse=True)
def set_demo_env(monkeypatch_session=None):
    """强制 DEMO_MODE=true，AMAP_MOCK=true"""
    import os
    os.environ["DEMO_MODE"] = "true"
    os.environ["AMAP_MOCK"] = "true"
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@localhost:5432/travel_agent"
    os.environ["REDIS_URL"] = "redis://localhost:6379"


@pytest.fixture
def mock_db_pool():
    """Mock asyncpg 连接池，避免真实 DB 连接"""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_conn)

    return mock_pool


@pytest.fixture
def mock_graph():
    """
    Mock LangGraph 持久化图

    chat.py 使用 graph.astream_events(version="v2") → mock 模拟 on_chain_start/end 事件序列。
    """
    from app.schemas.place import Place, Coordinates, PlaceCategory, PlaceSource

    mock_places = [
        Place(
            place_id="B001C8SVBF",
            name="宽窄巷子",
            category=PlaceCategory.ATTRACTION,
            address="青羊区长顺上街",
            coords=Coordinates(lng=104.0534, lat=30.6711),
            city="成都",
            source=PlaceSource.AMAP_POI,
            estimated_duration=120,
        )
    ]

    async def _fake_astream_events(input_state, config=None, version="v2"):
        """模拟 LangGraph astream_events v2：依次 yield on_chain_start/end 事件"""
        # Router 节点启动
        yield {"event": "on_chain_start", "name": "router", "data": {}}
        # Router 节点完成（无 tool_calls，直接进入 synthesizer）
        yield {"event": "on_chain_end", "name": "router", "data": {"output": {"react_iterations": 1}}}
        # Synthesizer 节点启动
        yield {"event": "on_chain_start", "name": "synthesizer", "data": {}}
        # Synthesizer 节点完成
        yield {
            "event": "on_chain_end",
            "name": "synthesizer",
            "data": {
                "output": {
                    "synthesized_places": mock_places,
                    "final_response": "为您找到了 1 个相关地点，请查看地点列表。",
                }
            },
        }

    mock = MagicMock()
    mock.astream_events = _fake_astream_events
    return mock


@pytest.fixture
def client(mock_db_pool, mock_graph):
    """构建带 mock 的 TestClient"""
    with patch("app.db.connection.get_pool", AsyncMock(return_value=mock_db_pool)), \
         patch("app.agents.graph.get_graph_with_persistence", AsyncMock(return_value=mock_graph)), \
         patch("app.agents.graph.init_persistent_graph", AsyncMock()), \
         patch("app.agents.graph.close_checkpointer", AsyncMock()):

        from app.main import app
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


# ===== 测试 =====

class TestHealthCheck:
    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


class TestRoomAPI:
    def test_create_room_success(self, client, mock_db_pool):
        resp = client.post("/api/room", json={
            "room_id": "test-room-01",
            "thread_id": "test-thread-01",
            "trip_city": "成都",
            "trip_days": 3,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["room_id"] == "test-room-01"

    def test_create_room_missing_room_id(self, client):
        resp = client.post("/api/room", json={
            "thread_id": "test-thread-01",
        })
        # FastAPI Pydantic 校验失败返回 422 Unprocessable Entity
        assert resp.status_code == 422

    def test_create_room_missing_thread_id(self, client):
        resp = client.post("/api/room", json={
            "room_id": "test-room-01",
        })
        assert resp.status_code == 422

    def test_get_room_state_not_found(self, client):
        resp = client.get("/api/room/nonexistent-room/state")
        assert resp.status_code == 404


class TestChatAPI:
    def test_chat_returns_sse_stream(self, client):
        resp = client.post("/api/chat", json={
            "thread_id": "test-thread-01",
            "user_id": "user-001",
            "message": "成都有哪些好玩的地方？",
            "selected_place_ids": [],
        })
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    def test_chat_sse_contains_done_event(self, client):
        resp = client.post("/api/chat", json={
            "thread_id": "test-thread-02",
            "user_id": "user-001",
            "message": "推荐几个景点",
            "selected_place_ids": [],
        })
        content = resp.text
        # 应包含 done 事件
        assert '"event": "done"' in content or '"event":"done"' in content

    def test_chat_sse_contains_thinking_event(self, client):
        resp = client.post("/api/chat", json={
            "thread_id": "test-thread-03",
            "user_id": "user-001",
            "message": "成都美食推荐",
        })
        content = resp.text
        assert "thinking" in content

    def test_chat_sse_contains_place_event(self, client):
        resp = client.post("/api/chat", json={
            "thread_id": "test-thread-04",
            "user_id": "user-001",
            "message": "成都景点",
        })
        content = resp.text
        assert "place" in content


class TestOptimizeAPI:
    """测试排线接口（不依赖外部 API，使用直线距离估算）"""

    BASE_PLACES = [
        {
            "place_id": "P001", "name": "宽窄巷子",
            "category": "attraction", "address": "青羊区长顺上街",
            "coords": {"lng": 104.0534, "lat": 30.6711},
            "city": "成都", "source": "amap_poi",
            "amap_photos": [], "estimated_duration": 120,
        },
        {
            "place_id": "P002", "name": "武侯祠",
            "category": "attraction", "address": "武侯区武侯祠大街",
            "coords": {"lng": 104.0468, "lat": 30.6421},
            "city": "成都", "source": "amap_poi",
            "amap_photos": [], "estimated_duration": 120,
        },
        {
            "place_id": "P003", "name": "大熊猫基地",
            "category": "attraction", "address": "成华区熊猫大道",
            "coords": {"lng": 104.1496, "lat": 30.7373},
            "city": "成都", "source": "amap_poi",
            "amap_photos": [], "estimated_duration": 180,
        },
    ]

    def test_optimize_returns_itinerary(self, client):
        resp = client.post("/api/optimize", json={
            "thread_id": "test-thread-opt-01",
            "places": self.BASE_PLACES,
            "trip_days": 1,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "itinerary" in data
        assert data["itinerary"]["city"] == "成都"

    def test_optimize_day_count(self, client):
        resp = client.post("/api/optimize", json={
            "thread_id": "test-thread-opt-02",
            "places": self.BASE_PLACES,
            "trip_days": 1,
        })
        data = resp.json()
        assert len(data["itinerary"]["days"]) == 1

    def test_optimize_all_places_in_result(self, client):
        resp = client.post("/api/optimize", json={
            "thread_id": "test-thread-opt-03",
            "places": self.BASE_PLACES,
            "trip_days": 1,
        })
        data = resp.json()
        total_slots = sum(len(d["slots"]) for d in data["itinerary"]["days"])
        assert total_slots == len(self.BASE_PLACES)

    def test_optimize_empty_places_returns_400(self, client):
        resp = client.post("/api/optimize", json={
            "thread_id": "test-thread-opt-04",
            "places": [],
            "trip_days": 3,
        })
        assert resp.status_code == 400

    def test_optimize_returns_distance(self, client):
        resp = client.post("/api/optimize", json={
            "thread_id": "test-thread-opt-05",
            "places": self.BASE_PLACES,
            "trip_days": 1,
        })
        data = resp.json()
        assert "total_distance_km" in data
        assert data["total_distance_km"] >= 0

    def test_optimize_with_start_date(self, client):
        resp = client.post("/api/optimize", json={
            "thread_id": "test-thread-opt-06",
            "places": self.BASE_PLACES,
            "trip_days": 1,
            "start_date": "2026-06-01",
        })
        assert resp.status_code == 200
        data = resp.json()
        # 日期字段应被填充
        first_day = data["itinerary"]["days"][0]
        assert first_day["date"] == "2026-06-01"

    def test_optimize_transport_legs_present(self, client):
        resp = client.post("/api/optimize", json={
            "thread_id": "test-thread-opt-07",
            "places": self.BASE_PLACES,
            "trip_days": 1,
        })
        data = resp.json()
        slots = data["itinerary"]["days"][0]["slots"]
        # 除最后一个外，每个 slot 都应有交通段
        for slot in slots[:-1]:
            assert slot["transport"] is not None, f"slot {slot['place_id']} 缺少交通段"
            assert slot["transport"]["duration_mins"] > 0
        # 最后一个 slot 无交通段
        assert slots[-1]["transport"] is None

    def test_optimize_method_field(self, client):
        resp = client.post("/api/optimize", json={
            "thread_id": "test-thread-opt-08",
            "places": self.BASE_PLACES,
            "trip_days": 1,
        })
        data = resp.json()
        assert data["optimization_method"] == "kmeans_tsp"
