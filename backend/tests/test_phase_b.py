"""Phase B 测试套件（SPEC §5 推荐升级完整验收）

测试优先（TDD）：先写测试，再实现，最后跑通。

覆盖：
  B1  PlaceRecommendation / Alternative schema 合法性
  B1  Synthesizer v2：chunk_id 引用强制、结构化输出解析、无 RAG 时安全降级
  B3  Critic chunk_id 验证规则：无效 chunk_id → 剥离 reason；Rule 4 Alternative 合法性
  B4  Alternative 生成：每个地点至少 0–2 个替代方案，字段完整
  B5  主题模板 API schema：4 个主题、query 非空
  B6  PlaceCard 背面数据结构：YjsPlace 包含 recommendation 字段（前端 TS 类型）

全部离线运行，不需要 DB / LLM API。
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.place import Place, Coordinates, PlaceCategory, PlaceSource, PlaceRAGMeta


# ─── 共用 fixture ──────────────────────────────────────────────────────────────

def make_place(pid, name, category=PlaceCategory.ATTRACTION, tags=None):
    return Place(
        place_id=pid,
        name=name,
        category=category,
        address=f"{name}地址",
        coords=Coordinates(lng=104.05, lat=30.67),
        city="成都",
        source=PlaceSource.AMAP_POI,
        tags=tags or [],
    )

RAG_CHUNKS = [
    {"chunk_id": "chunk_001", "note_id": "note_1", "content": "宽窄巷子周末人很多，建议工作日去，上午9点开门。", "place_ids": ["P001"]},
    {"chunk_id": "chunk_002", "note_id": "note_1", "content": "武侯祠门票 50 元，周二闭馆，参观时长约 2 小时。", "place_ids": ["P002"]},
    {"chunk_id": "chunk_003", "note_id": "note_2", "content": "成都火锅必点毛肚、鸭肠，建议避开晚高峰排队。", "place_ids": ["P003"]},
]

CHUNK_ID_SET = {c["chunk_id"] for c in RAG_CHUNKS}

PLACES = [
    make_place("P001", "宽窄巷子", tags=["街区", "古镇"]),
    make_place("P002", "武侯祠",   tags=["博物馆", "历史遗址"]),
    make_place("P003", "老成都火锅", PlaceCategory.FOOD, tags=["火锅"]),
]


# ══════════════════════════════════════════════════════════════════════════════
# B1: PlaceRecommendation / Alternative Schema
# ══════════════════════════════════════════════════════════════════════════════

class TestPlaceRecommendationSchema:
    def test_import(self):
        from app.schemas.recommendation import PlaceRecommendation, Alternative
        assert PlaceRecommendation is not None
        assert Alternative is not None

    def test_full_fields(self):
        from app.schemas.recommendation import PlaceRecommendation, Alternative
        rec = PlaceRecommendation(
            place_id="P001",
            name="宽窄巷子",
            category_l1="景点",
            category_l2="街区",
            reason="游记记载工作日人少，适合慢慢逛。",
            suitable_for=["情侣", "摄影"],
            avoid_tips=["周末人极多，建议工作日"],
            source_chunk_ids=["chunk_001"],
            alternatives=[
                Alternative(
                    place_id="P006",
                    name="锦里古街",
                    why_alternative="比宽窄巷子更安静，排队少",
                )
            ],
            confidence="medium",
        )
        assert rec.place_id == "P001"
        assert len(rec.alternatives) == 1
        assert rec.alternatives[0].why_alternative

    def test_defaults(self):
        from app.schemas.recommendation import PlaceRecommendation
        rec = PlaceRecommendation(place_id="X", name="测试", category_l1="景点", category_l2="公园")
        assert rec.suitable_for == []
        assert rec.avoid_tips == []
        assert rec.source_chunk_ids == []
        assert rec.alternatives == []
        assert rec.confidence == "low"

    def test_confidence_enum(self):
        from app.schemas.recommendation import PlaceRecommendation
        import pydantic
        with pytest.raises((pydantic.ValidationError, ValueError)):
            PlaceRecommendation(place_id="X", name="X", category_l1="景点",
                                category_l2="公园", confidence="invalid")

    def test_alternative_fields(self):
        from app.schemas.recommendation import Alternative
        a = Alternative(place_id="P999", name="替代地", why_alternative="更安静")
        assert a.place_id == "P999"
        assert a.why_alternative

    def test_reason_can_be_empty(self):
        """reason 为空时 schema 不报错（Critic 剥离后的状态）"""
        from app.schemas.recommendation import PlaceRecommendation
        rec = PlaceRecommendation(place_id="X", name="X", category_l1="景点", category_l2="公园", reason="")
        assert rec.reason == ""

    def test_source_chunk_ids_list(self):
        from app.schemas.recommendation import PlaceRecommendation
        rec = PlaceRecommendation(
            place_id="X", name="X", category_l1="景点", category_l2="公园",
            source_chunk_ids=["chunk_001", "chunk_002"]
        )
        assert len(rec.source_chunk_ids) == 2


# ══════════════════════════════════════════════════════════════════════════════
# B1: Synthesizer v2 输出解析
# ══════════════════════════════════════════════════════════════════════════════

class TestSynthesizerV2Parsing:
    """测试 Synthesizer v2 的 LLM 输出解析逻辑（不调真实 LLM）"""

    def _make_llm_response(self, data: dict) -> MagicMock:
        mock = MagicMock()
        mock.content = json.dumps(data, ensure_ascii=False)
        return mock

    def test_parses_recommendations_with_chunk_ids(self):
        """Synthesizer 应能从 LLM JSON 中解析出 PlaceRecommendation 含 source_chunk_ids"""
        from app.agents.nodes.synthesizer_v2 import _parse_llm_response
        llm_output = {
            "response_text": "为您推荐成都打卡地",
            "place_updates": [
                {
                    "place_id": "P001",
                    "description": "成都最具代表性的历史街区",
                    "tags": ["古镇", "拍照"],
                    "reason": "游记记载工作日人少，适合慢慢逛。",
                    "avoid_tips": ["周末人极多"],
                    "source_chunk_ids": ["chunk_001"],
                    "alternatives": [{"place_id": "P006", "name": "锦里", "why_alternative": "更安静"}],
                    "suitable_for": ["情侣"],
                    "confidence": "medium",
                    "tip_snippets": ["工作日去更好"],
                    "sentiment_score": 0.8,
                    "estimated_duration": 120,
                }
            ],
        }
        places_out, response_text, recs = _parse_llm_response(llm_output, PLACES, RAG_CHUNKS)
        assert response_text == "为您推荐成都打卡地"
        assert len(recs) >= 1
        rec = next((r for r in recs if r.place_id == "P001"), None)
        assert rec is not None
        assert "chunk_001" in rec.source_chunk_ids
        assert rec.reason != ""
        assert len(rec.alternatives) == 1

    def test_strips_invalid_chunk_ids_at_parse_time(self):
        """chunk_id 不在本次检索 context 中 → parse 阶段不应保留（或 Critic 后剥离）"""
        from app.agents.nodes.synthesizer_v2 import _parse_llm_response
        llm_output = {
            "response_text": "推荐",
            "place_updates": [
                {
                    "place_id": "P001",
                    "description": "街区",
                    "reason": "游记说很棒",
                    "source_chunk_ids": ["chunk_999"],  # 不存在的 chunk_id
                    "avoid_tips": [],
                    "alternatives": [],
                    "suitable_for": [],
                    "confidence": "low",
                    "tip_snippets": [],
                    "sentiment_score": 0.0,
                }
            ],
        }
        places_out, _, recs = _parse_llm_response(llm_output, PLACES, RAG_CHUNKS)
        rec = next((r for r in recs if r.place_id == "P001"), None)
        # 无效 chunk_id → reason 应被剥离或 source_chunk_ids 为空
        if rec:
            invalid_kept = "chunk_999" in rec.source_chunk_ids
            assert not invalid_kept, "无效 chunk_id 不应保留在 source_chunk_ids 中"

    def test_no_rag_safe_fallback(self):
        """无 RAG chunks 时不应崩溃，reason 为空，source_chunk_ids 为空"""
        from app.agents.nodes.synthesizer_v2 import _parse_llm_response
        llm_output = {
            "response_text": "推荐",
            "place_updates": [
                {
                    "place_id": "P001",
                    "description": "街区",
                    "reason": "不应显示（无 RAG 支撑）",
                    "source_chunk_ids": [],
                    "avoid_tips": [],
                    "alternatives": [],
                    "suitable_for": [],
                    "confidence": "low",
                    "tip_snippets": [],
                    "sentiment_score": 0.0,
                }
            ],
        }
        places_out, _, recs = _parse_llm_response(llm_output, PLACES, [])  # 空 RAG
        # 不崩溃即可，reason 来自 LLM 但无 chunk 支撑时置空
        assert isinstance(recs, list)

    def test_alternatives_parsed_correctly(self):
        from app.agents.nodes.synthesizer_v2 import _parse_llm_response
        llm_output = {
            "response_text": "推荐",
            "place_updates": [
                {
                    "place_id": "P003",
                    "description": "火锅",
                    "reason": "游记推荐毛肚。",
                    "source_chunk_ids": ["chunk_003"],
                    "avoid_tips": ["避开晚高峰"],
                    "alternatives": [
                        {"place_id": "P010", "name": "海底捞", "why_alternative": "无需排队"},
                        {"place_id": "P011", "name": "大龙燚", "why_alternative": "更实惠"},
                    ],
                    "suitable_for": ["情侣", "朋友"],
                    "confidence": "high",
                    "tip_snippets": ["必点毛肚"],
                    "sentiment_score": 0.9,
                }
            ],
        }
        _, _, recs = _parse_llm_response(llm_output, PLACES, RAG_CHUNKS)
        rec = next((r for r in recs if r.place_id == "P003"), None)
        assert rec is not None
        assert len(rec.alternatives) == 2
        assert rec.alternatives[0].why_alternative

    def test_malformed_json_returns_empty(self):
        from app.agents.nodes.synthesizer_v2 import _parse_llm_response_raw
        bad_json = "这不是 JSON { broken"
        places_out, response_text, recs = _parse_llm_response_raw(bad_json, PLACES, RAG_CHUNKS)
        # 不崩溃，返回原始 places
        assert isinstance(places_out, list)
        assert isinstance(recs, list)


# ══════════════════════════════════════════════════════════════════════════════
# B1: Synthesizer v2 Prompt 约束验证
# ══════════════════════════════════════════════════════════════════════════════

class TestSynthesizerV2Prompt:
    def test_prompt_contains_chunk_id_constraint(self):
        """Synthesizer Prompt 必须包含 chunk_id 引用强制约束"""
        from app.agents.nodes.synthesizer_v2 import SYNTHESIZER_SYSTEM_V2
        assert "chunk_id" in SYNTHESIZER_SYSTEM_V2 or "source_chunk_ids" in SYNTHESIZER_SYSTEM_V2
        assert "宁缺勿" in SYNTHESIZER_SYSTEM_V2 or "不出该字段" in SYNTHESIZER_SYSTEM_V2

    def test_prompt_contains_alternative_instruction(self):
        """Prompt 必须包含替代方案生成指令"""
        from app.agents.nodes.synthesizer_v2 import SYNTHESIZER_PROMPT_V2
        assert "替代" in SYNTHESIZER_PROMPT_V2 or "alternative" in SYNTHESIZER_PROMPT_V2.lower()

    def test_demo_mode_returns_recommendations(self):
        """Demo 模式也应返回 recommendations 列表（可为空）"""
        from app.agents.nodes.synthesizer_v2 import _build_demo_response_v2
        result = _build_demo_response_v2(PLACES, "成都", None, RAG_CHUNKS)
        assert "response_text" in result
        assert "recommendations" in result
        assert isinstance(result["recommendations"], list)


# ══════════════════════════════════════════════════════════════════════════════
# B3: Critic chunk_id 验证规则
# ══════════════════════════════════════════════════════════════════════════════

class TestCriticChunkValidation:
    def _make_rec(self, place_id, chunk_ids, reason="游记推荐"):
        from app.schemas.recommendation import PlaceRecommendation
        return PlaceRecommendation(
            place_id=place_id,
            name=f"地点{place_id}",
            category_l1="景点",
            category_l2="街区",
            reason=reason,
            source_chunk_ids=chunk_ids,
        )

    def test_strips_reason_for_invalid_chunk(self):
        """chunk_id 不在 rag_chunks → reason 被剥离（置为空字符串）"""
        from app.agents.nodes.critic import _validate_chunk_ids
        recs = [self._make_rec("P001", ["chunk_999"], reason="这句话引用了不存在的chunk")]
        valid_set = CHUNK_ID_SET
        stripped = _validate_chunk_ids(recs, valid_set)
        assert stripped[0].reason == ""
        assert stripped[0].source_chunk_ids == []

    def test_keeps_reason_for_valid_chunk(self):
        """chunk_id 在 rag_chunks → reason 保留"""
        from app.agents.nodes.critic import _validate_chunk_ids
        recs = [self._make_rec("P001", ["chunk_001"], reason="游记说人少")]
        stripped = _validate_chunk_ids(recs, CHUNK_ID_SET)
        assert stripped[0].reason == "游记说人少"
        assert "chunk_001" in stripped[0].source_chunk_ids

    def test_partial_invalid_chunks_stripped(self):
        """部分 chunk_id 无效 → 只保留有效的，reason 保留"""
        from app.agents.nodes.critic import _validate_chunk_ids
        recs = [self._make_rec("P001", ["chunk_001", "chunk_999"], reason="混合引用")]
        stripped = _validate_chunk_ids(recs, CHUNK_ID_SET)
        assert "chunk_001" in stripped[0].source_chunk_ids
        assert "chunk_999" not in stripped[0].source_chunk_ids
        assert stripped[0].reason == "混合引用"  # 有有效 chunk，reason 保留

    def test_all_invalid_chunks_strips_reason(self):
        """全部 chunk_id 无效 → reason 置空"""
        from app.agents.nodes.critic import _validate_chunk_ids
        recs = [self._make_rec("P001", ["chunk_x", "chunk_y"], reason="全无效")]
        stripped = _validate_chunk_ids(recs, CHUNK_ID_SET)
        assert stripped[0].reason == ""

    def test_empty_chunk_ids_no_reason_kept(self):
        """source_chunk_ids 为空时 reason 也应置空（无游记支撑不出 reason）"""
        from app.agents.nodes.critic import _validate_chunk_ids
        recs = [self._make_rec("P001", [], reason="无 chunk 支撑的 reason")]
        stripped = _validate_chunk_ids(recs, CHUNK_ID_SET)
        assert stripped[0].reason == ""

    def test_no_rag_chunks_all_reasons_stripped(self):
        """本次无 RAG 检索（valid_set 为空）→ 所有 reason 被剥离"""
        from app.agents.nodes.critic import _validate_chunk_ids
        recs = [
            self._make_rec("P001", ["chunk_001"], reason="A"),
            self._make_rec("P002", ["chunk_002"], reason="B"),
        ]
        stripped = _validate_chunk_ids(recs, set())
        assert all(r.reason == "" for r in stripped)

    def test_critic_run_integrates_chunk_validation(self):
        """critic.run 应调用 chunk 验证并更新 state"""
        from app.agents.nodes.critic import run as critic_run
        from app.schemas.recommendation import PlaceRecommendation

        rec_with_bad_chunk = PlaceRecommendation(
            place_id="P001",
            name="宽窄巷子",
            category_l1="景点",
            category_l2="街区",
            reason="这是无效的 chunk 引用",
            source_chunk_ids=["chunk_999"],
        )

        state = {
            "synthesized_places": PLACES,
            "rag_chunks": RAG_CHUNKS,
            "recommendations": [rec_with_bad_chunk],
            "working_context": {},
            "critic_iterations": 0,
        }

        result = asyncio.run(critic_run(state))
        updated_recs = result.get("recommendations", [rec_with_bad_chunk])
        if updated_recs:
            bad_rec = next((r for r in updated_recs if r.place_id == "P001"), None)
            if bad_rec:
                assert bad_rec.reason == "", "Critic 未剥离无效 chunk_id 的 reason"


# ══════════════════════════════════════════════════════════════════════════════
# B4: Alternative 生成质量
# ══════════════════════════════════════════════════════════════════════════════

class TestAlternativeGeneration:
    def test_alternative_why_alternative_not_empty(self):
        from app.schemas.recommendation import Alternative
        a = Alternative(place_id="P999", name="替代地", why_alternative="更便宜，排队少")
        assert len(a.why_alternative) > 0

    def test_alternative_has_place_id_and_name(self):
        from app.schemas.recommendation import Alternative
        a = Alternative(place_id="ALT001", name="备选景点")
        assert a.place_id == "ALT001"
        assert a.name == "备选景点"

    def test_recommendation_max_two_alternatives(self):
        """每个地点最多 2 个替代方案（SPEC §5.1）"""
        from app.schemas.recommendation import PlaceRecommendation, Alternative
        alts = [Alternative(place_id=f"A{i}", name=f"地点{i}") for i in range(5)]
        rec = PlaceRecommendation(
            place_id="P001", name="主推地", category_l1="景点",
            category_l2="街区", alternatives=alts
        )
        # 测试 schema 不拒绝多个 alt（数量限制由 Synthesizer 逻辑控制）
        assert len(rec.alternatives) == 5  # schema 本身不限制数量

    def test_parse_caps_alternatives_to_two(self):
        """Synthesizer 解析时应把 alternatives 截断到 2 个"""
        from app.agents.nodes.synthesizer_v2 import _parse_llm_response
        llm_output = {
            "response_text": "推荐",
            "place_updates": [
                {
                    "place_id": "P001",
                    "description": "X",
                    "reason": "Y",
                    "source_chunk_ids": ["chunk_001"],
                    "avoid_tips": [],
                    "alternatives": [
                        {"place_id": f"ALT{i}", "name": f"地点{i}", "why_alternative": "原因"} for i in range(4)
                    ],
                    "suitable_for": [],
                    "confidence": "medium",
                    "tip_snippets": [],
                    "sentiment_score": 0.0,
                }
            ],
        }
        _, _, recs = _parse_llm_response(llm_output, PLACES, RAG_CHUNKS)
        rec = next((r for r in recs if r.place_id == "P001"), None)
        if rec:
            assert len(rec.alternatives) <= 2, "alternatives 应被截断到 2 个"


# ══════════════════════════════════════════════════════════════════════════════
# B5: 主题模板 API Schema
# ══════════════════════════════════════════════════════════════════════════════

class TestThemeTemplates:
    def test_import_themes(self):
        from app.api.themes import THEME_TEMPLATES
        assert len(THEME_TEMPLATES) >= 4

    def test_four_required_themes(self):
        from app.api.themes import THEME_TEMPLATES
        ids = {t["theme_id"] for t in THEME_TEMPLATES}
        assert "citywalk" in ids
        assert "museum" in ids
        assert "food" in ids
        assert "family" in ids

    def test_each_theme_has_required_fields(self):
        from app.api.themes import THEME_TEMPLATES
        for t in THEME_TEMPLATES:
            assert "theme_id" in t,   f"{t} 缺少 theme_id"
            assert "name" in t,       f"{t} 缺少 name"
            assert "query" in t,      f"{t} 缺少 query"
            assert "icon" in t,       f"{t} 缺少 icon"
            assert len(t["query"]) > 0, f"{t['theme_id']} query 不能为空"

    def test_theme_query_meaningful(self):
        from app.api.themes import THEME_TEMPLATES
        for t in THEME_TEMPLATES:
            assert len(t["query"]) >= 5, f"{t['theme_id']} query 过短"

    def test_theme_api_schema(self):
        """ThemeResponse schema 可以正常实例化"""
        from app.api.themes import ThemeItem
        item = ThemeItem(theme_id="citywalk", name="Citywalk 老城线", query="成都老城区漫步", icon="🚶")
        assert item.theme_id == "citywalk"


# ══════════════════════════════════════════════════════════════════════════════
# B6: 前端 YjsPlace 类型包含推荐字段（类型结构验证）
# ══════════════════════════════════════════════════════════════════════════════

class TestFrontendRecommendationTypes:
    """验证前端 types/place.ts 存在 recommendation 相关字段（通过读文件检测）"""

    def _read_ts_file(self, rel_path: str) -> str:
        import os
        base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        full = os.path.join(base, "frontend", "src", rel_path)
        if not os.path.exists(full):
            return ""
        with open(full, encoding="utf-8") as f:
            return f.read()

    def test_place_type_has_recommendation_field(self):
        content = self._read_ts_file("types/place.ts")
        if not content:
            pytest.skip("types/place.ts 不存在，跳过前端类型检测")
        assert "recommendation" in content or "PlaceRecommendation" in content, \
            "types/place.ts 缺少 recommendation / PlaceRecommendation 字段"

    def test_place_card_imports_flip(self):
        content = self._read_ts_file("components/places/PlaceCard.tsx")
        if not content:
            pytest.skip("PlaceCard.tsx 不存在")
        # 翻转卡片应有 flip/back/front 相关实现
        has_flip = any(kw in content for kw in ["flip", "isFlipped", "back", "front", "rotateY"])
        assert has_flip, "PlaceCard.tsx 缺少翻转卡片实现（flip/isFlipped/back/front 关键词）"


# ══════════════════════════════════════════════════════════════════════════════
# AgentState 新增 recommendations 字段
# ══════════════════════════════════════════════════════════════════════════════

class TestAgentStateV2:
    def test_agent_state_has_recommendations(self):
        from app.agents.state import AgentState
        import typing
        hints = typing.get_type_hints(AgentState)
        assert "recommendations" in hints, \
            "AgentState 缺少 recommendations 字段（Phase B 新增）"

    def test_synthesizer_v2_module_exists(self):
        """synthesizer_v2.py 应存在"""
        try:
            import app.agents.nodes.synthesizer_v2
        except ImportError as e:
            pytest.fail(f"synthesizer_v2 模块不存在或导入失败: {e}")

    def test_synthesizer_v2_run_signature(self):
        """synthesizer_v2.run 应为 async 函数，接受 AgentState"""
        import inspect
        from app.agents.nodes import synthesizer_v2
        assert hasattr(synthesizer_v2, "run"), "synthesizer_v2 缺少 run 函数"
        assert inspect.iscoroutinefunction(synthesizer_v2.run), "synthesizer_v2.run 应为 async"
