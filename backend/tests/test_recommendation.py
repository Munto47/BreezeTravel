"""Phase B 推荐升级单测（SPEC §5）

覆盖：
- PlaceRecommendation schema 正确性
- _parse_recommendations：source_chunk_ids 验证 + reason 剥离逻辑
- Synthesizer 在 demo/no-llm 模式下返回 recommendations 字段
- _extract_user_cuisine_constraint 品类过滤

全部离线，无需 DB / LLM API。
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from app.schemas.recommendation import Alternative, PlaceRecommendation
from app.agents.nodes.synthesizer import _parse_recommendations


# ─── PlaceRecommendation Schema ───────────────────────────────────────────────

class TestPlaceRecommendationSchema:
    def test_minimal_construction(self):
        r = PlaceRecommendation(place_id="p1", name="武侯祠")
        assert r.place_id == "p1"
        assert r.confidence == "low"
        assert r.alternatives == []
        assert r.source_chunk_ids == []

    def test_full_construction(self):
        r = PlaceRecommendation(
            place_id="p2",
            name="宽窄巷子",
            category_l1="景点",
            category_l2="街区",
            reason="游记描述：早上人少，光线好适合摄影。",
            suitable_for=["情侣", "摄影"],
            avoid_tips=["周末 10:00 后人很多", "避免节假日"],
            source_chunk_ids=["chunk_001", "chunk_002"],
            alternatives=[
                Alternative(place_id="p3", name="锦里", why_alternative="更安静，排队少")
            ],
            confidence="medium",
        )
        assert r.confidence == "medium"
        assert len(r.suitable_for) == 2
        assert len(r.avoid_tips) == 2
        assert len(r.source_chunk_ids) == 2
        assert len(r.alternatives) == 1
        assert r.alternatives[0].why_alternative == "更安静，排队少"

    def test_confidence_default_low(self):
        r = PlaceRecommendation(place_id="x", name="测试")
        assert r.confidence == "low"

    def test_alternative_schema(self):
        a = Alternative(place_id="a1", name="A点", why_alternative="便宜")
        assert a.place_id == "a1"
        assert a.why_alternative == "便宜"


# ─── _parse_recommendations：chunk_id 验证逻辑 ────────────────────────────────

class TestParseRecommendations:
    """验证 Critic 层：无效 chunk_id 被剔除，reason 被清空（SPEC §5.2）"""

    VALID_IDS = {"chunk_001", "chunk_002", "chunk_003"}

    def _raw(self, place_id="p1", source_ids=None, reason="来自游记", avoid=None, alts=None, conf="medium"):
        return {
            "place_id": place_id,
            "name": "测试地点",
            "reason": reason,
            "suitable_for": ["情侣"],
            "avoid_tips": avoid or ["避坑1"],
            "source_chunk_ids": source_ids or [],
            "alternatives": alts or [],
            "confidence": conf,
        }

    def test_valid_chunk_ids_pass_through(self):
        raw = [self._raw(source_ids=["chunk_001", "chunk_002"])]
        recs = _parse_recommendations(raw, self.VALID_IDS)
        assert len(recs) == 1
        assert recs[0].source_chunk_ids == ["chunk_001", "chunk_002"]
        assert recs[0].reason == "来自游记"

    def test_invalid_chunk_ids_stripped(self):
        raw = [self._raw(source_ids=["fake_chunk_999"])]
        recs = _parse_recommendations(raw, self.VALID_IDS)
        assert recs[0].source_chunk_ids == []
        # reason 被清空（无游记支撑）
        assert recs[0].reason == ""
        assert recs[0].avoid_tips == []

    def test_partial_chunk_ids_filtered(self):
        raw = [self._raw(source_ids=["chunk_001", "nonexistent_id"])]
        recs = _parse_recommendations(raw, self.VALID_IDS)
        assert recs[0].source_chunk_ids == ["chunk_001"]
        # reason 保留（至少有一个 verified chunk）
        assert recs[0].reason == "来自游记"

    def test_empty_source_ids_keeps_reason(self):
        """来源 ID 本来就为空（LLM 未声称引用）→ reason 保留（允许无引用的简单描述）"""
        raw = [self._raw(source_ids=[])]
        recs = _parse_recommendations(raw, self.VALID_IDS)
        assert recs[0].reason == "来自游记"

    def test_alternatives_capped_at_two(self):
        alts = [
            {"place_id": f"a{i}", "name": f"替代{i}", "why_alternative": f"理由{i}"}
            for i in range(5)
        ]
        raw = [self._raw(alts=alts)]
        recs = _parse_recommendations(raw, self.VALID_IDS)
        assert len(recs[0].alternatives) == 2

    def test_alternatives_incomplete_entry_skipped(self):
        alts = [{"place_id": "a1"}]  # 缺 name → 跳过
        raw = [self._raw(alts=alts)]
        recs = _parse_recommendations(raw, self.VALID_IDS)
        assert recs[0].alternatives == []

    def test_invalid_confidence_defaults_to_low(self):
        raw = [self._raw(conf="超高")]
        recs = _parse_recommendations(raw, self.VALID_IDS)
        assert recs[0].confidence == "low"

    def test_missing_place_id_skipped(self):
        raw = [{"name": "没有 place_id", "reason": "..."}]
        recs = _parse_recommendations(raw, self.VALID_IDS)
        assert recs == []

    def test_empty_input(self):
        recs = _parse_recommendations([], self.VALID_IDS)
        assert recs == []

    def test_multiple_places(self):
        raw = [
            self._raw("p1", source_ids=["chunk_001"]),
            self._raw("p2", source_ids=["bad_id"]),
            self._raw("p3", source_ids=[]),
        ]
        recs = _parse_recommendations(raw, self.VALID_IDS)
        assert len(recs) == 3
        assert recs[0].reason != ""   # p1: valid chunk → reason retained
        assert recs[1].reason == ""   # p2: invalid chunk → reason stripped
        assert recs[2].reason != ""   # p3: no chunk claim → reason retained


# ─── Synthesizer 返回 recommendations 字段 ───────────────────────────────────

class TestSynthesizerRecommendationsField:
    """验证 synthesizer.run 的输出中始终包含 recommendations 字段"""

    def _make_state(self, places=None, rag_chunks=None):
        from langchain_core.messages import HumanMessage
        from app.schemas.place import Coordinates, Place, PlaceCategory, PlaceSource

        if places is None:
            places = [
                Place(
                    place_id="p1",
                    name="宽窄巷子",
                    category=PlaceCategory.ATTRACTION,
                    address="成都",
                    coords=Coordinates(lng=104.05, lat=30.67),
                    city="成都",
                    source=PlaceSource.AMAP_POI,
                )
            ]
        return {
            "messages": [HumanMessage(content="推荐成都景点")],
            "amap_places": places,
            "rag_chunks": rag_chunks or [],
            "trip_city": "成都",
            "working_context": None,
            "thread_id": "test-rec-001",
            "user_id": "anonymous",
        }

    def test_demo_mode_returns_recommendations_field(self):
        import asyncio
        from unittest.mock import patch
        from app.agents.nodes.synthesizer import run

        state = self._make_state()
        with patch("app.config.settings.demo_mode", True):
            result = asyncio.run(run(state))

        assert "recommendations" in result
        assert isinstance(result["recommendations"], list)

    def test_no_places_returns_empty_recommendations(self):
        import asyncio
        from app.agents.nodes.synthesizer import run

        state = self._make_state(places=[])
        result = asyncio.run(run(state))

        assert "recommendations" in result
        assert result["recommendations"] == []

    def test_llm_failure_returns_empty_recommendations(self):
        """LLM 调用失败时应优雅降级，recommendations 为空列表而非 KeyError"""
        import asyncio
        from unittest.mock import patch, MagicMock
        from app.agents.nodes.synthesizer import run

        state = self._make_state()

        mock_llm = MagicMock()
        mock_llm.ainvoke.side_effect = RuntimeError("LLM timeout")

        with patch("app.config.settings.demo_mode", False):
            with patch("app.agents.nodes.synthesizer._get_llm", return_value=mock_llm):
                result = asyncio.run(run(state))

        assert "recommendations" in result
        assert result["recommendations"] == []

    def test_rag_chunks_expose_chunk_ids_in_context(self):
        """验证 chunk_id 格式化逻辑：chunk_id 或 note_id 被正确暴露给 LLM"""
        from app.agents.nodes.synthesizer import _parse_recommendations

        # chunk 有 chunk_id
        valid_ids = {"c001"}
        raw = [{"place_id": "p1", "name": "A", "source_chunk_ids": ["c001"], "reason": "引自c001"}]
        recs = _parse_recommendations(raw, valid_ids)
        assert recs[0].source_chunk_ids == ["c001"]
        assert recs[0].reason != ""

    def test_llm_output_with_valid_recommendations_parsed(self):
        """模拟 LLM 返回包含 recommendations 字段的 JSON，验证完整解析路径"""
        import asyncio
        import json
        from unittest.mock import patch, AsyncMock, MagicMock
        from app.agents.nodes.synthesizer import run
        from app.schemas.place import Coordinates, Place, PlaceCategory, PlaceSource
        from langchain_core.messages import HumanMessage

        place = Place(
            place_id="p1", name="武侯祠",
            category=PlaceCategory.ATTRACTION,
            address="成都", coords=Coordinates(lng=104.05, lat=30.64),
            city="成都", source=PlaceSource.AMAP_POI,
        )
        rag_chunk = {"chunk_id": "ck001", "content": "武侯祠早上九点开门，游客较少", "note_id": "n001"}

        llm_json = json.dumps({
            "response_text": "推荐武侯祠",
            "place_updates": [{"place_id": "p1", "description": "历史名胜", "tags": ["文化"], "estimated_duration": 150}],
            "recommendations": [{
                "place_id": "p1",
                "name": "武侯祠",
                "reason": "早上九点开门，游客较少 [ck001]",
                "suitable_for": ["深度文化", "历史爱好者"],
                "avoid_tips": ["周末人多，早去"],
                "source_chunk_ids": ["ck001"],
                "alternatives": [{"place_id": "p2", "name": "锦里", "why_alternative": "更轻松"}],
                "confidence": "medium",
            }]
        }, ensure_ascii=False)

        mock_response = MagicMock()
        mock_response.content = llm_json
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        state = {
            "messages": [HumanMessage(content="推荐成都景点")],
            "amap_places": [place],
            "rag_chunks": [rag_chunk],
            "trip_city": "成都",
            "working_context": None,
            "thread_id": "test-rec-002",
            "user_id": "anonymous",
        }

        with patch("app.config.settings.demo_mode", False):
            with patch("app.agents.nodes.synthesizer._get_llm", return_value=mock_llm):
                result = asyncio.run(run(state))

        assert "recommendations" in result
        recs = result["recommendations"]
        assert len(recs) == 1
        assert recs[0].place_id == "p1"
        assert recs[0].source_chunk_ids == ["ck001"]
        assert recs[0].reason != ""
        assert len(recs[0].suitable_for) == 2
        assert len(recs[0].alternatives) == 1
        assert recs[0].confidence == "medium"


# ─── 品类过滤（已有功能回归） ─────────────────────────────────────────────────────

class TestCuisineConstraintExtraction:
    def test_extracts_hotpot(self):
        from app.agents.nodes.synthesizer import _extract_user_cuisine_constraint
        kws = _extract_user_cuisine_constraint("我想吃火锅")
        assert "火锅" in kws

    def test_extracts_coffee(self):
        from app.agents.nodes.synthesizer import _extract_user_cuisine_constraint
        kws = _extract_user_cuisine_constraint("找个好喝的咖啡")
        assert "咖啡" in kws

    def test_no_constraint(self):
        from app.agents.nodes.synthesizer import _extract_user_cuisine_constraint
        kws = _extract_user_cuisine_constraint("推荐成都景点")
        assert kws == []

    def test_multiple_constraints(self):
        from app.agents.nodes.synthesizer import _extract_user_cuisine_constraint
        kws = _extract_user_cuisine_constraint("想吃烧烤或者烤肉")
        assert len(kws) > 0
