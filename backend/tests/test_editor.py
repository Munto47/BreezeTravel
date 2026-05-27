"""EditorAgent + Rule Fast Path 单测（SPEC §4 / C3-C4）

覆盖：
- ItineraryPatch schema 构造与校验
- fast_path.fast_apply：remove_place / swap_days
- fast_path 内置 Critic 检查（R_ZERO_FOOD_DAY / R_BUFFER_DEFICIT）
- edit API 端点（/api/edit）集成
- _try_rule_fast_path 意图识别
- EditorAgent parse_edit_intent（mock LLM）

全部离线，无需 DB / LLM API。
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from copy import deepcopy

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.schemas.patch import ItineraryPatch, PatchResult
from app.schemas.itinerary import DayPlan, Itinerary, TimeSlot
from app.schemas.place import Coordinates, Place, PlaceCategory, PlaceSource


# ─── 辅助构造 ─────────────────────────────────────────────────────────────────

def _place(pid, name, category="attraction"):
    return {
        "place_id": pid, "name": name, "category": category,
        "address": "测试地址",
        "coords": {"lng": 104.05, "lat": 30.67},
        "city": "成都", "source": "amap_poi",
    }


def _slot(pid, name, start="09:00", end="11:00", category="attraction"):
    return TimeSlot(
        place_id=pid,
        place=_place(pid, name, category),
        start_time=start,
        end_time=end,
        transport=None,
    )


def _make_itinerary(days_slots: list[list[TimeSlot]]) -> Itinerary:
    """构造测试用 Itinerary"""
    days = []
    for i, slots in enumerate(days_slots):
        days.append(DayPlan(
            day_index=i,
            date=f"2026-07-0{i+1}",
            cluster_id=i,
            slots=slots,
        ))
    return Itinerary(
        itinerary_id="test-itin-001",
        thread_id="test-thread",
        city="成都",
        days=days,
        generated_at="2026-07-01T00:00:00Z",
        version=1,
    )


# ─── ItineraryPatch Schema ────────────────────────────────────────────────────

class TestItineraryPatchSchema:
    def test_remove_place_minimal(self):
        p = ItineraryPatch(op="remove_place", day_index=0, target_place_id="p1", rationale="test")
        assert p.op == "remove_place"
        assert p.day_index == 0
        assert p.affects_global is False

    def test_swap_days(self):
        p = ItineraryPatch(op="swap_days", day_index=0, target_place_id="1", rationale="互换")
        assert p.op == "swap_days"

    def test_replace_place_with_query(self):
        p = ItineraryPatch(
            op="replace_place", day_index=1, slot_index=2,
            target_place_id="old_id", new_place_query="找个好的咖啡馆",
            rationale="换掉太贵的地方",
        )
        assert p.new_place_query == "找个好的咖啡馆"

    def test_rebuild_day_with_template(self):
        p = ItineraryPatch(
            op="rebuild_day", day_index=2,
            new_template_id="T_FAMILY_LIGHT",
            rationale="改为亲子模板",
        )
        assert p.new_template_id == "T_FAMILY_LIGHT"


# ─── Rule Fast Path: remove_place ────────────────────────────────────────────

class TestFastPathRemove:
    def _itin(self):
        day0 = [
            _slot("P_MUSEUM", "博物馆", "09:00", "11:00"),
            _slot("F_HOTPOT", "火锅",   "12:00", "13:30", "food"),
            _slot("P_PARK",   "公园",   "14:00", "15:30"),
        ]
        day1 = [
            _slot("P_BEACH", "海边", "09:00", "11:00"),
            _slot("F_REST",  "餐厅", "12:00", "13:00", "food"),
        ]
        return _make_itinerary([day0, day1])

    def test_remove_existing_place(self):
        from app.agents.editor.fast_path import fast_apply
        patch = ItineraryPatch(op="remove_place", day_index=0,
                               target_place_id="P_MUSEUM", rationale="测试")
        new_itin, violations = fast_apply(patch, self._itin())
        slot_ids = [s.place_id for s in new_itin.days[0].slots]
        assert "P_MUSEUM" not in slot_ids
        assert len(new_itin.days[0].slots) == 2

    def test_remove_keeps_other_days_intact(self):
        from app.agents.editor.fast_path import fast_apply
        patch = ItineraryPatch(op="remove_place", day_index=0,
                               target_place_id="P_MUSEUM", rationale="测试")
        new_itin, _ = fast_apply(patch, self._itin())
        assert len(new_itin.days[1].slots) == 2

    def test_remove_nonexistent_returns_patch_error(self):
        from app.agents.editor.fast_path import fast_apply
        patch = ItineraryPatch(op="remove_place", day_index=0,
                               target_place_id="GHOST_ID", rationale="测试")
        _, violations = fast_apply(patch, self._itin())
        rules = [v["rule"] for v in violations]
        assert "PATCH_ERROR" in rules

    def test_remove_invalid_day_returns_patch_error(self):
        from app.agents.editor.fast_path import fast_apply
        patch = ItineraryPatch(op="remove_place", day_index=99,
                               target_place_id="P_MUSEUM", rationale="测试")
        _, violations = fast_apply(patch, self._itin())
        rules = [v["rule"] for v in violations]
        assert "PATCH_ERROR" in rules

    def test_remove_last_food_triggers_zero_food_day(self):
        """删掉唯一餐厅 → R_ZERO_FOOD_DAY"""
        from app.agents.editor.fast_path import fast_apply
        itin = _make_itinerary([[
            _slot("P_MUSEUM", "博物馆", "09:00", "11:00"),
            _slot("F_HOTPOT", "火锅",   "12:00", "13:30", "food"),
        ]])
        patch = ItineraryPatch(op="remove_place", day_index=0,
                               target_place_id="F_HOTPOT", rationale="测试")
        _, violations = fast_apply(patch, itin)
        rules = [v["rule"] for v in violations]
        assert "R_ZERO_FOOD_DAY" in rules

    def test_remove_non_food_no_zero_food_violation(self):
        from app.agents.editor.fast_path import fast_apply
        itin = _make_itinerary([[
            _slot("P_MUSEUM", "博物馆", "09:00", "11:00"),
            _slot("F_HOTPOT", "火锅",   "12:00", "13:30", "food"),
        ]])
        patch = ItineraryPatch(op="remove_place", day_index=0,
                               target_place_id="P_MUSEUM", rationale="测试")
        _, violations = fast_apply(patch, itin)
        rules = [v["rule"] for v in violations]
        assert "R_ZERO_FOOD_DAY" not in rules

    def test_remove_does_not_mutate_original(self):
        """fast_apply 不应修改原始 itinerary（deepcopy）"""
        from app.agents.editor.fast_path import fast_apply
        itin = self._itin()
        original_len = len(itin.days[0].slots)
        patch = ItineraryPatch(op="remove_place", day_index=0,
                               target_place_id="P_MUSEUM", rationale="测试")
        fast_apply(patch, itin)
        assert len(itin.days[0].slots) == original_len


# ─── Rule Fast Path: swap_days ───────────────────────────────────────────────

class TestFastPathSwapDays:
    def _itin(self):
        day0 = [_slot("A", "地点A"), _slot("B", "地点B", "14:00", "16:00")]
        day1 = [_slot("C", "地点C"), _slot("D", "地点D", "14:00", "16:00")]
        return _make_itinerary([day0, day1])

    def test_swap_days_content(self):
        from app.agents.editor.fast_path import fast_apply
        patch = ItineraryPatch(op="swap_days", day_index=0,
                               target_place_id="1", rationale="互换")
        new_itin, _ = fast_apply(patch, self._itin())
        # 第0天现在是原第1天的内容
        assert new_itin.days[0].slots[0].place_id == "C"
        # 第1天现在是原第0天的内容
        assert new_itin.days[1].slots[0].place_id == "A"

    def test_swap_invalid_day_index(self):
        from app.agents.editor.fast_path import fast_apply
        patch = ItineraryPatch(op="swap_days", day_index=0,
                               target_place_id="99", rationale="测试")
        _, violations = fast_apply(patch, self._itin())
        assert any(v["rule"] == "PATCH_ERROR" for v in violations)

    def test_swap_no_target_id(self):
        from app.agents.editor.fast_path import fast_apply
        patch = ItineraryPatch(op="swap_days", day_index=0,
                               target_place_id=None, rationale="测试")
        _, violations = fast_apply(patch, self._itin())
        assert any(v["rule"] == "PATCH_ERROR" for v in violations)

    def test_swap_preserves_day_indices(self):
        """互换内容后 day_index 标签不变"""
        from app.agents.editor.fast_path import fast_apply
        patch = ItineraryPatch(op="swap_days", day_index=0,
                               target_place_id="1", rationale="互换")
        new_itin, _ = fast_apply(patch, self._itin())
        assert new_itin.days[0].day_index == 0
        assert new_itin.days[1].day_index == 1

    def test_swap_buffer_deficit_detected(self):
        """互换后如果时间链断裂 → R_BUFFER_DEFICIT"""
        from app.agents.editor.fast_path import fast_apply
        # day0：A 09:00-11:00, B 10:00-12:00（时间重叠）
        day0 = [_slot("A", "A", "09:00", "11:00"), _slot("B", "B", "10:00", "12:00")]
        day1 = [_slot("C", "C", "09:00", "10:00")]
        itin = _make_itinerary([day0, day1])
        patch = ItineraryPatch(op="swap_days", day_index=0,
                               target_place_id="1", rationale="互换")
        # 互换后 day1 拿到原 day0（有重叠）→ R_BUFFER_DEFICIT
        _, violations = fast_apply(patch, itin)
        rules = [v["rule"] for v in violations]
        assert "R_BUFFER_DEFICIT" in rules


# ─── Rule Fast Path: unsupported op ──────────────────────────────────────────

class TestFastPathUnsupportedOp:
    def test_add_place_returns_error(self):
        from app.agents.editor.fast_path import fast_apply
        itin = _make_itinerary([[_slot("P1", "地点1")]])
        patch = ItineraryPatch(op="add_place", day_index=0, rationale="测试")
        _, violations = fast_apply(patch, itin)
        assert any(v["rule"] == "PATCH_ERROR" for v in violations)


# ─── _try_rule_fast_path 意图识别 ─────────────────────────────────────────────

class TestRuleFastPathIntent:
    def _itin(self):
        day0 = [_slot("P_MUSEUM", "武侯祠")]
        day1 = [_slot("P_PARK", "宽窄巷子")]
        return _make_itinerary([day0, day1])

    def test_removes_by_place_name(self):
        from app.api.edit import _try_rule_fast_path
        itin = self._itin()
        patch = _try_rule_fast_path("删掉武侯祠", itin)
        assert patch is not None
        assert patch.op == "remove_place"
        assert patch.target_place_id == "P_MUSEUM"

    def test_swap_days_detected(self):
        from app.api.edit import _try_rule_fast_path
        itin = self._itin()
        patch = _try_rule_fast_path("互换第1天和第2天", itin)
        assert patch is not None
        assert patch.op == "swap_days"

    def test_no_match_returns_none(self):
        from app.api.edit import _try_rule_fast_path
        itin = self._itin()
        patch = _try_rule_fast_path("帮我把行程调得轻松一点", itin)
        assert patch is None  # 复杂意图，Fast Path 识别不了


# ─── /api/edit 端点集成测试 ───────────────────────────────────────────────────

class TestEditAPI:
    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app)

    def _itin_payload(self):
        return {
            "itinerary_id": "test-001",
            "thread_id": "thread-001",
            "city": "成都",
            "days": [{
                "day_index": 0,
                "date": "2026-07-01",
                "cluster_id": 0,
                "slots": [
                    {
                        "place_id": "P_MUSEUM",
                        "place": _place("P_MUSEUM", "武侯祠"),
                        "start_time": "09:00",
                        "end_time": "11:00",
                        "transport": None,
                    },
                    {
                        "place_id": "F_FOOD",
                        "place": _place("F_FOOD", "火锅", "food"),
                        "start_time": "12:00",
                        "end_time": "13:30",
                        "transport": None,
                    },
                ],
                "weather_summary": None,
                "tips": None,
            }],
            "generated_at": "2026-07-01T00:00:00Z",
            "version": 1,
        }

    def test_direct_patch_remove(self):
        """直接传 patch（不走 LLM）→ 返回 200"""
        client = self._client()
        resp = client.post("/api/edit", json={
            "thread_id": "t1",
            "itinerary": self._itin_payload(),
            "patch": {
                "op": "remove_place",
                "day_index": 0,
                "target_place_id": "P_MUSEUM",
                "rationale": "测试删除",
            },
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["path_used"] == "direct_patch"
        slot_ids = [s["place_id"] for s in data["itinerary"]["days"][0]["slots"]]
        assert "P_MUSEUM" not in slot_ids

    def test_no_msg_no_patch_returns_400(self):
        client = self._client()
        resp = client.post("/api/edit", json={
            "thread_id": "t2",
            "itinerary": self._itin_payload(),
        })
        assert resp.status_code == 400

    def test_rule_fast_path_triggered_by_msg(self):
        """user_msg 触发 Rule Fast Path（删除）"""
        client = self._client()
        resp = client.post("/api/edit", json={
            "thread_id": "t3",
            "user_msg": "删掉武侯祠",
            "itinerary": self._itin_payload(),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["path_used"] == "fast_path"
        slot_ids = [s["place_id"] for s in data["itinerary"]["days"][0]["slots"]]
        assert "P_MUSEUM" not in slot_ids

    def test_patch_error_returns_400(self):
        """删除不存在的地点 → 400"""
        client = self._client()
        resp = client.post("/api/edit", json={
            "thread_id": "t4",
            "itinerary": self._itin_payload(),
            "patch": {
                "op": "remove_place",
                "day_index": 0,
                "target_place_id": "NONEXISTENT",
                "rationale": "测试",
            },
        })
        assert resp.status_code == 400

    def test_response_contains_violations(self):
        """删除最后餐厅 → violations 包含 R_ZERO_FOOD_DAY（但不报 400）"""
        client = self._client()
        # 先删掉非餐厅地点，保留只有餐厅的状态 → 再删餐厅
        resp = client.post("/api/edit", json={
            "thread_id": "t5",
            "itinerary": _make_itinerary([[
                _slot("F_ONLY", "唯一餐厅", "12:00", "13:00", "food"),
            ]]).model_dump(),
            "patch": {
                "op": "remove_place",
                "day_index": 0,
                "target_place_id": "F_ONLY",
                "rationale": "测试",
            },
        })
        assert resp.status_code == 200
        data = resp.json()
        violation_rules = [v["rule"] for v in data["violations"]]
        assert "R_ZERO_FOOD_DAY" in violation_rules


# ─── EditorAgent parse_edit_intent（mock LLM）────────────────────────────────

class TestEditorAgentParseIntent:
    def _itin(self):
        return _make_itinerary([[_slot("P1", "武侯祠"), _slot("F1", "火锅", "12:00", "13:30", "food")]])

    def test_valid_llm_output_parsed(self):
        valid_json = json.dumps({
            "op": "remove_place",
            "day_index": 0,
            "target_place_id": "P1",
            "rationale": "用户想换掉武侯祠",
        })

        async def _invoke(messages):
            resp = MagicMock()
            resp.content = valid_json
            return resp

        mock_llm = MagicMock()
        mock_llm.ainvoke = _invoke

        async def go():
            from app.agents.editor.editor_agent import parse_edit_intent
            with patch("app.agents.editor.editor_agent._get_llm", return_value=mock_llm):
                return await parse_edit_intent("换掉武侯祠", self._itin())

        patch_result = asyncio.run(go())
        assert patch_result is not None
        assert patch_result.op == "remove_place"
        assert patch_result.target_place_id == "P1"

    def test_invalid_llm_output_retries_and_fails(self):
        """LLM 三次都返回无效 JSON → 返回 None"""
        async def _invoke(messages):
            resp = MagicMock()
            resp.content = "这不是 JSON"
            return resp

        mock_llm = MagicMock()
        mock_llm.ainvoke = _invoke

        async def go():
            from app.agents.editor.editor_agent import parse_edit_intent
            with patch("app.agents.editor.editor_agent._get_llm", return_value=mock_llm):
                return await parse_edit_intent("换掉武侯祠", self._itin())

        result = asyncio.run(go())
        assert result is None

    def test_no_llm_returns_none(self):
        """无 API key → _get_llm() 返回 None → 返回 None"""
        async def go():
            from app.agents.editor.editor_agent import parse_edit_intent
            with patch("app.agents.editor.editor_agent._get_llm", return_value=None):
                return await parse_edit_intent("换掉武侯祠", self._itin())

        result = asyncio.run(go())
        assert result is None
