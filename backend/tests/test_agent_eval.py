"""
Agent 评估体系测试

分两类：
────────────────────────────────────────────────────────────────
1. 离线单元测试（无需 API / DB）
   - test_eval_dataset_coverage     : 评估集覆盖验证（城市 / 意图类型均衡性）
   - test_tool_accuracy_logic        : _check_tool_accuracy 工具选择判定逻辑
   - test_synthesizer_validity_logic : _check_synthesizer_validity 输出有效性判定
   - TestFTRouterOnEvalSet           : FT Router 在 eval 集上的离线准确率（≥ 70%）

2. 集成评估（需要 API Key + DB，skip 修饰，手动运行）
   - test_full_pipeline_sample       : 10 条 case 完整 pipeline 冒烟测试

运行方式：
  # 离线测试（CI 使用）
  python -m pytest tests/test_agent_eval.py -v -k "not full_pipeline"

  # 完整集成评估（手动）
  python -m pytest tests/test_agent_eval.py::test_full_pipeline_sample -v -s
────────────────────────────────────────────────────────────────
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.eval_agent import (
    EVAL_CASES,
    _check_tool_accuracy,
    _check_synthesizer_validity,
)


# ═══════════════════════════════════════════════════════════════════
# 1. 评估集结构验证（纯离线）
# ═══════════════════════════════════════════════════════════════════

class TestEvalDataset:
    """评估集的覆盖度和结构完整性验证"""

    def test_total_size(self):
        """评估集共 50 条"""
        assert len(EVAL_CASES) == 50

    def test_all_cases_have_required_fields(self):
        """每条 case 都有必填字段"""
        required = {"id", "query", "city", "expected_intent", "expected_tools"}
        for case in EVAL_CASES:
            missing = required - set(case.keys())
            assert not missing, f"case {case.get('id')} 缺少字段：{missing}"

    def test_unique_ids(self):
        """所有 case ID 唯一"""
        ids = [c["id"] for c in EVAL_CASES]
        assert len(ids) == len(set(ids)), "存在重复 case ID"

    def test_city_coverage(self):
        """覆盖所有 7 个目标城市"""
        cities = {c["city"] for c in EVAL_CASES}
        required_cities = {"成都", "北京", "上海", "厦门", "广州", "深圳", "杭州"}
        assert required_cities.issubset(cities), f"缺少城市：{required_cities - cities}"

    def test_intent_distribution(self):
        """意图类型分布合理（每类至少 5 条）"""
        from collections import Counter
        dist = Counter(c["expected_intent"] for c in EVAL_CASES)
        for intent in ("amap", "rag", "both", "weather"):
            assert dist[intent] >= 5, f"意图 '{intent}' 样本过少：{dist[intent]} 条"

    def test_amap_cases_use_search_places(self):
        """amap 意图的 expected_tools 必须包含 search_places"""
        amap_cases = [c for c in EVAL_CASES if c["expected_intent"] == "amap"]
        for case in amap_cases:
            assert "search_places" in case["expected_tools"], \
                f"case {case['id']}: amap 意图缺少 search_places"

    def test_rag_cases_use_search_travel_notes(self):
        """rag 意图的 expected_tools 必须包含 search_travel_notes"""
        rag_cases = [c for c in EVAL_CASES if c["expected_intent"] == "rag"]
        for case in rag_cases:
            assert "search_travel_notes" in case["expected_tools"], \
                f"case {case['id']}: rag 意图缺少 search_travel_notes"

    def test_both_cases_have_both_tools(self):
        """both 意图的 expected_tools 必须同时包含两个工具"""
        both_cases = [c for c in EVAL_CASES if c["expected_intent"] == "both"]
        for case in both_cases:
            assert "search_places" in case["expected_tools"], \
                f"case {case['id']}: both 意图缺少 search_places"
            assert "search_travel_notes" in case["expected_tools"], \
                f"case {case['id']}: both 意图缺少 search_travel_notes"

    def test_weather_cases_use_get_weather(self):
        """weather 意图的 expected_tools 必须包含 get_weather"""
        weather_cases = [c for c in EVAL_CASES if c["expected_intent"] == "weather"]
        for case in weather_cases:
            assert "get_weather" in case["expected_tools"], \
                f"case {case['id']}: weather 意图缺少 get_weather"


# ═══════════════════════════════════════════════════════════════════
# 2. 工具选择判定逻辑验证
# ═══════════════════════════════════════════════════════════════════

class TestToolAccuracyLogic:
    """_check_tool_accuracy 函数的边界条件验证"""

    def test_amap_single_tool_match(self):
        """amap 意图：实际调用包含 search_places → 正确"""
        assert _check_tool_accuracy(["search_places"], ["search_places"], "amap") is True

    def test_amap_extra_tool_still_correct(self):
        """amap 意图：多调了其他工具仍算正确"""
        assert _check_tool_accuracy(
            ["search_places", "search_travel_notes"], ["search_places"], "amap"
        ) is True

    def test_amap_missing_tool_wrong(self):
        """amap 意图：只调了 search_travel_notes → 错误"""
        assert _check_tool_accuracy(
            ["search_travel_notes"], ["search_places"], "amap"
        ) is False

    def test_both_requires_all_tools(self):
        """both 意图：两个工具都必须调用"""
        assert _check_tool_accuracy(
            ["search_places", "search_travel_notes"],
            ["search_places", "search_travel_notes"],
            "both",
        ) is True

    def test_both_partial_is_wrong(self):
        """both 意图：只调用了一个工具 → 错误"""
        assert _check_tool_accuracy(
            ["search_places"], ["search_places", "search_travel_notes"], "both"
        ) is False

    def test_empty_actual_tools_is_wrong(self):
        """没有调用任何工具 → 错误"""
        assert _check_tool_accuracy([], ["search_places"], "amap") is False

    def test_weather_tool_match(self):
        """weather 意图：调用了 get_weather → 正确"""
        assert _check_tool_accuracy(
            ["get_weather"], ["get_weather"], "weather"
        ) is True


# ═══════════════════════════════════════════════════════════════════
# 3. Synthesizer 输出有效性判定
# ═══════════════════════════════════════════════════════════════════

class TestSynthesizerValidityLogic:
    """_check_synthesizer_validity 函数验证"""

    def test_empty_places_invalid(self):
        """synthesized_places 为空 → 无效"""
        assert _check_synthesizer_validity({"synthesized_places": []}) is False

    def test_missing_places_key_invalid(self):
        """state 中没有 synthesized_places → 无效"""
        assert _check_synthesizer_validity({}) is False

    def test_place_with_name_valid(self):
        """有 name 字段的字典对象 → 有效"""
        state = {"synthesized_places": [{"name": "宽窄巷子", "place_id": "B0FFI58N0H"}]}
        assert _check_synthesizer_validity(state) is True

    def test_place_object_with_name_valid(self):
        """有 name 属性的 Place 对象 → 有效"""
        class FakePlace:
            name = "锦里"
            place_id = "abc123"

        state = {"synthesized_places": [FakePlace()]}
        assert _check_synthesizer_validity(state) is True

    def test_place_with_empty_name_invalid(self):
        """name 为空字符串 → 无效"""
        state = {"synthesized_places": [{"name": "", "place_id": "abc"}]}
        assert _check_synthesizer_validity(state) is False

    def test_multiple_places_one_valid_is_ok(self):
        """多个地点中有一个有有效 name → 整体有效"""
        state = {
            "synthesized_places": [
                {"name": "", "place_id": "x"},
                {"name": "大悦城", "place_id": "y"},
            ]
        }
        assert _check_synthesizer_validity(state) is True


# ═══════════════════════════════════════════════════════════════════
# 4. FT Router 离线准确率（需要本地模型，无需 API/DB）
# ═══════════════════════════════════════════════════════════════════

class TestFTRouterOnEvalSet:
    """FT Router 在 50 条 eval 集上的离线分类准确率"""

    def _load_classifier(self):
        """加载分类器，返回 None 则 skip"""
        try:
            from app.config import settings
            from app.agents.nodes.router_classifier import classify
            return classify, settings.ft_router_model_path
        except Exception:
            return None, None

    def test_overall_accuracy_above_threshold(self):
        """FT Router 在 amap/rag/both 三类上整体准确率 ≥ 70%"""
        classify, model_path = self._load_classifier()
        if classify is None:
            pytest.skip("router_classifier 未能加载")

        # weather 类排除：FT Router 未专门训练该类，判断较宽松
        testable = [c for c in EVAL_CASES if c["expected_intent"] != "weather"]
        correct = 0
        for case in testable:
            result = classify(case["query"], case["city"], model_path)
            if result is None:
                pytest.skip("FT Router 模型未加载（ft_router_enabled=false 或模型不存在）")
            predicted = result.get("intent", "unknown")
            if predicted == case["expected_intent"]:
                correct += 1

        accuracy = correct / len(testable)
        print(f"\nFT Router 准确率（排除 weather）：{correct}/{len(testable)} = {accuracy:.1%}")
        assert accuracy >= 0.70, f"FT Router 准确率过低：{accuracy:.1%}（目标 ≥70%）"

    def test_amap_intent_accuracy(self):
        """amap 意图准确率 ≥ 75%"""
        classify, model_path = self._load_classifier()
        if classify is None:
            pytest.skip("router_classifier 未能加载")

        amap_cases = [c for c in EVAL_CASES if c["expected_intent"] == "amap"]
        correct = sum(
            1 for c in amap_cases
            if (r := classify(c["query"], c["city"], model_path)) and r.get("intent") == "amap"
        )
        if not any(classify(c["query"], c["city"], model_path) for c in amap_cases[:1]):
            pytest.skip("FT Router 模型未加载")
        accuracy = correct / len(amap_cases)
        assert accuracy >= 0.75, f"amap 准确率：{accuracy:.1%}（目标 ≥75%）"

    def test_rag_intent_accuracy(self):
        """rag 意图准确率 ≥ 70%"""
        classify, model_path = self._load_classifier()
        if classify is None:
            pytest.skip("router_classifier 未能加载")

        rag_cases = [c for c in EVAL_CASES if c["expected_intent"] == "rag"]
        correct = sum(
            1 for c in rag_cases
            if (r := classify(c["query"], c["city"], model_path)) and r.get("intent") == "rag"
        )
        if not any(classify(c["query"], c["city"], model_path) for c in rag_cases[:1]):
            pytest.skip("FT Router 模型未加载")
        accuracy = correct / len(rag_cases)
        assert accuracy >= 0.70, f"rag 准确率：{accuracy:.1%}（目标 ≥70%）"


# ═══════════════════════════════════════════════════════════════════
# 5. 集成评估（手动运行，需要 API + DB）
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.skip(
    reason="集成评估：需要 API Key + DB + 已入库游记，手动运行："
    " pytest tests/test_agent_eval.py::test_full_pipeline_sample -v -s"
)
async def test_full_pipeline_sample():
    """
    10 条 case 完整 pipeline 冒烟测试

    目标指标：
      Router 工具选择准确率  ≥ 75%
      Synthesizer 输出有效率 ≥ 80%
      端到端成功率           ≥ 70%
    """
    from scripts.eval_agent import run_full_eval

    # 各意图类型各取 2-3 条，确保覆盖
    sample_ids = {"a01", "a05", "a10", "r01", "r07", "r12", "b01", "b05", "w01", "w03"}
    sample_cases = [c for c in EVAL_CASES if c["id"] in sample_ids]

    result = await run_full_eval(sample_cases)

    assert result["router_accuracy"] >= 0.75, \
        f"Router 准确率过低：{result['router_accuracy']:.1%}"
    assert result["synthesizer_validity"] >= 0.80, \
        f"Synthesizer 有效率过低：{result['synthesizer_validity']:.1%}"
    assert result["e2e_success_rate"] >= 0.70, \
        f"端到端成功率过低：{result['e2e_success_rate']:.1%}"
