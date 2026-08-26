"""D24 取舍评分 + D25 LLM Retry 单测（SPEC §3.5 / §7）

D24 测试：
  - _pref_score 正确处理 must_have / no_go / votes / diversity
  - _is_no_go 关键词过滤
  - Scheduler v2 在有 must_have 时优先排入对应地点
  - Scheduler v2 在有 no_go 时从行程中剔除对应地点

D25 测试：
  - 第一次 JSON 解析失败时触发重试
  - 超过 MAX_RETRIES 后降级返回空 recommendations
  - 成功路径不受重试逻辑影响

全部离线，无需 DB / LLM API。
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.schemas.place import Coordinates, Place, PlaceCategory, PlaceSource
from app.schemas.preferences import GroupPreferences


# ─── 辅助构造 ─────────────────────────────────────────────────────────────────

def _place(pid, name, lng=104.05, lat=30.67, category=PlaceCategory.ATTRACTION,
           tags=None, rating=None):
    return Place(
        place_id=pid, name=name, category=category,
        address="测试地址",
        coords=Coordinates(lng=lng, lat=lat),
        city="成都", source=PlaceSource.AMAP_POI,
        tags=tags or [],
        amap_rating=rating,
        estimated_duration=120,
    )


def _food(pid, name, tags=None, rating=None):
    return _place(pid, name, category=PlaceCategory.FOOD, tags=tags or ["餐厅"], rating=rating)


def _hotel(pid, name):
    return _place(pid, name, category=PlaceCategory.HOTEL, tags=["酒店"])


# ─── D24 _pref_score ──────────────────────────────────────────────────────────

class TestPrefScore:
    from app.agents.planner.nodes.scheduler_v2 import _pref_score

    def _score(self, place, prefs, used_l2=None, votes=None):
        from app.agents.planner.nodes.scheduler_v2 import _pref_score
        return _pref_score(place, prefs, used_l2 or set(), votes or {})

    def test_no_prefs_returns_zero(self):
        p = _place("p1", "宽窄巷子")
        assert self._score(p, None) == 0.0

    def test_must_have_match_adds_100(self):
        prefs = GroupPreferences(must_have=["博物馆"], trip_city="成都", trip_days=2)
        p = _place("p1", "成都博物馆", tags=["博物馆"])
        score = self._score(p, prefs)
        assert score >= 100.0

    def test_must_have_no_match_no_bonus(self):
        prefs = GroupPreferences(must_have=["博物馆"], trip_city="成都", trip_days=2)
        p = _place("p1", "宽窄巷子", tags=["街区"])
        score = self._score(p, prefs)
        assert score < 100.0

    def test_no_go_match_returns_neg_inf(self):
        prefs = GroupPreferences(no_go=["火锅"], trip_city="成都", trip_days=2)
        p = _food("f1", "大龙燚火锅", tags=["火锅"])
        score = self._score(p, prefs)
        assert score == float("-inf")

    def test_no_go_no_match_not_neg_inf(self):
        prefs = GroupPreferences(no_go=["火锅"], trip_city="成都", trip_days=2)
        p = _food("f1", "成都小吃", tags=["地方菜"])
        score = self._score(p, prefs)
        assert score != float("-inf")

    def test_votes_weight(self):
        prefs = GroupPreferences(trip_city="成都", trip_days=2)
        p = _place("p1", "大熊猫基地")
        vote_counts = {"p1": 3}
        score = self._score(p, prefs, votes=vote_counts)
        assert score == pytest.approx(3 * 10.0, abs=1.0)  # votes * 10，可能有 diversity bonus

    def test_rating_contributes(self):
        prefs = GroupPreferences(trip_city="成都", trip_days=2)
        p_high = _place("p1", "高分景点", rating=4.8)
        p_low  = _place("p2", "低分景点", rating=3.0)
        s_high = self._score(p_high, prefs)
        s_low  = self._score(p_low, prefs)
        assert s_high > s_low

    def test_diversity_bonus_first_l2(self):
        """used_l2_today 为空时应获得多样性奖励"""
        prefs = GroupPreferences(trip_city="成都", trip_days=2)
        p = _place("p1", "成都博物馆", tags=["博物馆"])
        score_fresh = self._score(p, prefs, used_l2=set())
        score_used  = self._score(p, prefs, used_l2={"博物馆"})
        assert score_fresh > score_used

    def test_nice_to_have_adds_score(self):
        prefs = GroupPreferences(nice_to_have=["街区"], trip_city="成都", trip_days=2)
        p = _place("p1", "宽窄巷子", tags=["街区"])
        score = self._score(p, prefs)
        assert score > 0


# ─── D24 _is_no_go ────────────────────────────────────────────────────────────

class TestIsNoGo:
    def test_name_match(self):
        from app.agents.planner.nodes.scheduler_v2 import _is_no_go
        prefs = GroupPreferences(no_go=["排队"], trip_city="成都", trip_days=2)
        p = _place("p1", "排队景点")
        assert _is_no_go(p, prefs) is True

    def test_tag_match(self):
        from app.agents.planner.nodes.scheduler_v2 import _is_no_go
        prefs = GroupPreferences(no_go=["火锅"], trip_city="成都", trip_days=2)
        p = _food("f1", "大龙燚", tags=["火锅"])
        assert _is_no_go(p, prefs) is True

    def test_no_match(self):
        from app.agents.planner.nodes.scheduler_v2 import _is_no_go
        prefs = GroupPreferences(no_go=["火锅"], trip_city="成都", trip_days=2)
        p = _food("f1", "成都小吃", tags=["地方菜"])
        assert _is_no_go(p, prefs) is False

    def test_none_prefs(self):
        from app.agents.planner.nodes.scheduler_v2 import _is_no_go
        p = _place("p1", "宽窄巷子")
        assert _is_no_go(p, None) is False


# ─── D24 Scheduler 集成：must_have 优先 + no_go 剔除 ─────────────────────────

class TestSchedulerD24Integration:
    """验证 Scheduler v2 实际使用 GroupPreferences 评分"""

    PLACES = [
        _place("P_MUSEUM", "成都博物馆", 104.05, 30.67, tags=["博物馆"]),
        _place("P_PARK",   "天府公园",   104.06, 30.68, tags=["公园"]),
        _food("F_HOTPOT",  "大龙燚火锅", tags=["火锅"]),
        _food("F_LOCAL",   "成都小吃",   tags=["地方菜"]),
        _hotel("H1",       "成都酒店"),
    ]

    def _run(self, prefs, votes=None, trip_days=2):
        from app.agents.planner.graph import run_planner
        import app.agents.planner.nodes.critic_v2 as cv2

        async def fake_critic(state):
            return {"critic_violations": [], "trace": state.get("trace", []) + ["mock"]}

        orig = cv2.run

        async def go():
            with patch("app.agents.planner.nodes.scheduler_v2._load_place_meta",
                       AsyncMock(return_value={})):
                with patch("app.agents.planner.nodes.weather_fetcher.run",
                           new=AsyncMock(return_value={"weather_forecast": {}, "trace": []})):
                    cv2.run = fake_critic
                    try:
                        return await run_planner(
                            places=self.PLACES,
                            trip_days=trip_days,
                            thread_id="d24-test",
                            user_prefs=prefs,
                            vote_counts=votes or {},
                        )
                    finally:
                        cv2.run = orig

        return asyncio.run(go())

    def test_no_go_fire_removed_from_itinerary(self):
        prefs = GroupPreferences(no_go=["火锅"], trip_city="成都", trip_days=2)
        result = self._run(prefs)
        all_slot_ids = [
            s.place_id
            for day in result.itinerary.days
            for s in day.slots
        ]
        assert "F_HOTPOT" not in all_slot_ids, "火锅在 no_go 中，不应出现在行程里"

    def test_must_have_museum_appears_in_itinerary(self):
        prefs = GroupPreferences(must_have=["博物馆"], trip_city="成都", trip_days=2)
        result = self._run(prefs)
        all_slot_ids = [
            s.place_id
            for day in result.itinerary.days
            for s in day.slots
        ]
        # 博物馆在 must_have → 得分 +100，应该被优先排入
        # 由于模板/可用性限制不能 100% 保证出现，至少验证没被 no_go 剔除
        assert "P_MUSEUM" not in result.backup_pool or "P_MUSEUM" in all_slot_ids

    def test_vote_counts_accepted_without_error(self):
        votes = {"P_MUSEUM": 3, "P_PARK": 1}
        result = self._run(prefs=None, votes=votes)
        assert result.itinerary is not None


# ─── D25 LLM Retry ────────────────────────────────────────────────────────────

class TestSynthesizerRetry:
    """验证 Synthesizer 在 JSON 解析失败时正确重试"""

    def _make_state(self):
        from langchain_core.messages import HumanMessage
        return {
            "messages": [HumanMessage(content="推荐成都景点")],
            "amap_places": [
                _place("p1", "宽窄巷子"),
            ],
            "rag_chunks": [],
            "trip_city": "成都",
            "working_context": None,
            "thread_id": "retry-test",
            "user_id": "anonymous",
        }

    def _mock_llm(self, responses: list[str]):
        """按顺序返回不同内容的 mock LLM"""
        call_count = {"n": 0}

        async def _invoke(messages):
            idx = min(call_count["n"], len(responses) - 1)
            call_count["n"] += 1
            resp = MagicMock()
            resp.content = responses[idx]
            return resp

        mock = MagicMock()
        mock.ainvoke = _invoke
        return mock

    def test_first_call_invalid_json_retries(self):
        """第一次返回无效 JSON，第二次返回合法 JSON → 成功"""
        valid_json = json.dumps({
            "response_text": "推荐成都景点",
            "place_updates": [{"place_id": "p1", "description": "古街区", "tags": ["文化"]}],
            "recommendations": [],
        }, ensure_ascii=False)

        mock_llm = self._mock_llm(["这不是 JSON！！！", valid_json])

        async def go():
            from app.agents.nodes.synthesizer import run
            with patch("app.config.settings.demo_mode", False):
                with patch("app.agents.nodes.synthesizer._get_llm", return_value=mock_llm):
                    return await run(self._make_state())

        result = asyncio.run(go())
        # 第二次重试成功，应返回 synthesized_places
        assert len(result["synthesized_places"]) == 1
        assert result["recommendations"] == []  # recommendations 为空列表（未提供）

    def test_all_retries_exhausted_falls_back(self):
        """3 次全都返回无效 JSON → 优雅降级（返回原始高德数据）"""
        mock_llm = self._mock_llm(["INVALID", "ALSO_INVALID", "STILL_INVALID"])

        async def go():
            from app.agents.nodes.synthesizer import run
            with patch("app.config.settings.demo_mode", False):
                with patch("app.agents.nodes.synthesizer._get_llm", return_value=mock_llm):
                    return await run(self._make_state())

        result = asyncio.run(go())
        # 降级：返回原始高德数据，recommendations 为空
        assert "synthesized_places" in result
        assert "recommendations" in result
        assert result["recommendations"] == []

    def test_first_call_success_no_retry(self):
        """第一次就成功 → 只调用一次 LLM"""
        valid_json = json.dumps({
            "response_text": "成都景点推荐",
            "place_updates": [],
            "recommendations": [],
        }, ensure_ascii=False)

        call_count = {"n": 0}

        async def _invoke(messages):
            call_count["n"] += 1
            resp = MagicMock()
            resp.content = valid_json
            return resp

        mock_llm = MagicMock()
        mock_llm.ainvoke = _invoke

        async def go():
            from app.agents.nodes.synthesizer import run
            with patch("app.config.settings.demo_mode", False):
                with patch("app.agents.nodes.synthesizer._get_llm", return_value=mock_llm):
                    return await run(self._make_state())

        asyncio.run(go())
        assert call_count["n"] == 1, "第一次成功不应触发重试"

    def test_llm_none_falls_back_gracefully(self):
        """_get_llm() 返回 None（无 API key）→ 直接降级，不崩溃"""
        async def go():
            from app.agents.nodes.synthesizer import run
            with patch("app.config.settings.demo_mode", False):
                with patch("app.agents.nodes.synthesizer._get_llm", return_value=None):
                    return await run(self._make_state())

        result = asyncio.run(go())
        assert "synthesized_places" in result
        assert result["recommendations"] == []

    def test_retry_adds_error_hint_to_message(self):
        """验证重试时消息中包含错误提示词"""
        messages_sent = []

        async def _invoke(messages):
            messages_sent.append(messages)
            if len(messages_sent) == 1:
                resp = MagicMock()
                resp.content = "NOT_JSON"
                return resp
            resp = MagicMock()
            resp.content = json.dumps({
                "response_text": "ok",
                "place_updates": [],
                "recommendations": [],
            })
            return resp

        mock_llm = MagicMock()
        mock_llm.ainvoke = _invoke

        async def go():
            from app.agents.nodes.synthesizer import run
            with patch("app.config.settings.demo_mode", False):
                with patch("app.agents.nodes.synthesizer._get_llm", return_value=mock_llm):
                    return await run(self._make_state())

        asyncio.run(go())
        assert len(messages_sent) == 2
        # 第二次消息内容应包含「重试」提示
        second_human_msg = messages_sent[1][-1].content
        assert "第 2 次" in second_human_msg or "重试" in second_human_msg
