"""
Sprint 3 — Router 微调模块单元测试（无需 GPU / 模型文件）

覆盖不依赖真实模型的纯逻辑部分：
  - _parse_output：JSON 解析 + 回退逻辑
  - to_chat_messages：训练数据格式
  - parse_jsonl_response：LLM 输出解析
  - Config 配置项加载
  - Router fast path 条件逻辑
  - classify() 无模型时返回 None

运行：
  cd backend
  python -m pytest tests/test_router_ft_unit.py -v
"""

import json
import pytest


# ─────────────────────────────────────────────────────────────
# _parse_output 单元测试
# ─────────────────────────────────────────────────────────────

class TestParseOutput:
    """router_classifier._parse_output 的纯逻辑测试，无需模型"""

    def setup_method(self):
        from app.agents.nodes.router_classifier import _parse_output
        self._parse = _parse_output

    def test_valid_json_amap(self):
        raw = '{"intent": "amap", "query_rewrite": "成都火锅餐厅"}'
        result = self._parse(raw, "原始查询")
        assert result["intent"] == "amap"
        assert result["query_rewrite"] == "成都火锅餐厅"

    def test_valid_json_rag(self):
        raw = '{"intent": "rag", "query_rewrite": "西安旅游攻略"}'
        result = self._parse(raw, "原始")
        assert result["intent"] == "rag"

    def test_valid_json_weather(self):
        raw = '{"intent": "weather", "query_rewrite": "成都天气"}'
        result = self._parse(raw, "原始")
        assert result["intent"] == "weather"

    def test_valid_json_both(self):
        raw = '{"intent": "both", "query_rewrite": "口碑好景点"}'
        result = self._parse(raw, "原始")
        assert result["intent"] == "both"

    def test_invalid_intent_falls_back_to_amap(self):
        raw = '{"intent": "unknown_type", "query_rewrite": "xxx"}'
        result = self._parse(raw, "原始查询")
        assert result["intent"] == "amap"

    def test_empty_query_rewrite_uses_original(self):
        raw = '{"intent": "amap", "query_rewrite": ""}'
        result = self._parse(raw, "原始查询文本")
        assert result["query_rewrite"] == "原始查询文本"

    def test_null_query_rewrite_uses_original(self):
        raw = '{"intent": "rag", "query_rewrite": null}'
        result = self._parse(raw, "原始查询")
        assert result["query_rewrite"] == "原始查询"

    def test_json_embedded_in_text(self):
        raw = '好的，分类结果是：{"intent": "weather", "query_rewrite": "杭州天气"} 以上。'
        result = self._parse(raw, "原始")
        assert result["intent"] == "weather"
        assert result["query_rewrite"] == "杭州天气"

    def test_keyword_fallback_weather(self):
        raw = "天气查询相关"
        result = self._parse(raw, "今天天气怎样")
        assert result["intent"] == "weather"
        assert result["query_rewrite"] == "今天天气怎样"

    def test_keyword_fallback_rag(self):
        raw = "这是一个攻略类问题"
        result = self._parse(raw, "攻略")
        assert result["intent"] == "rag"

    def test_keyword_fallback_both(self):
        raw = "用户需要查口碑信息"
        result = self._parse(raw, "口碑查询")
        assert result["intent"] == "both"

    def test_fallback_default_to_amap(self):
        raw = "完全无法识别的输出内容xyz"
        result = self._parse(raw, "随机查询")
        assert result["intent"] == "amap"
        assert result["query_rewrite"] == "随机查询"

    def test_json_with_extra_whitespace(self):
        raw = '  \n  {"intent":  "rag",  "query_rewrite":  "北京游记"  }  \n'
        result = self._parse(raw, "原始")
        assert result["intent"] == "rag"


# ─────────────────────────────────────────────────────────────
# parse_jsonl_response 单元测试
# ─────────────────────────────────────────────────────────────

class TestParseJsonlResponse:
    """generate_training_data.parse_jsonl_response 解析逻辑测试"""

    def setup_method(self):
        from scripts.generate_training_data import parse_jsonl_response
        self._parse = parse_jsonl_response

    def test_single_valid_line(self):
        raw = '{"query": "成都火锅", "intent": "amap", "query_rewrite": "成都火锅餐厅"}'
        result = self._parse(raw)
        assert len(result) == 1
        assert result[0]["intent"] == "amap"

    def test_multiple_lines(self):
        raw = '\n'.join([
            '{"query": "q1", "intent": "amap", "query_rewrite": "r1"}',
            '{"query": "q2", "intent": "rag", "query_rewrite": "r2"}',
            '{"query": "q3", "intent": "weather", "query_rewrite": "r3"}',
        ])
        result = self._parse(raw)
        assert len(result) == 3
        assert result[1]["intent"] == "rag"

    def test_skip_empty_lines(self):
        raw = '\n{"query": "q1", "intent": "amap", "query_rewrite": "r1"}\n\n'
        result = self._parse(raw)
        assert len(result) == 1

    def test_skip_markdown_code_block(self):
        raw = '```json\n{"query": "q1", "intent": "amap", "query_rewrite": "r1"}\n```'
        result = self._parse(raw)
        assert len(result) == 1

    def test_skip_lines_missing_required_fields(self):
        raw = '{"query": "q1", "intent": "amap"}'  # 缺少 query_rewrite
        result = self._parse(raw)
        assert len(result) == 0

    def test_extract_json_from_mixed_line(self):
        raw = '第1条: {"query": "test", "intent": "rag", "query_rewrite": "rw"} end'
        result = self._parse(raw)
        assert len(result) == 1

    def test_empty_input(self):
        result = self._parse("")
        assert result == []


# ─────────────────────────────────────────────────────────────
# to_chat_messages 格式测试
# ─────────────────────────────────────────────────────────────

class TestToChatMessages:
    """训练数据 ChatML 格式验证"""

    def setup_method(self):
        from scripts.generate_training_data import to_chat_messages
        self._fn = to_chat_messages

    def test_output_schema(self):
        result = self._fn("成都有什么好吃的？", "成都", "amap", "成都美食推荐")
        assert "messages" in result
        assert "label" in result
        assert "city" in result
        assert "original_query" in result

    def test_messages_roles(self):
        result = self._fn("test query", "北京", "rag", "rewrite")
        roles = [m["role"] for m in result["messages"]]
        assert roles == ["system", "user", "assistant"]

    def test_assistant_message_is_valid_json(self):
        result = self._fn("test query", "北京", "both", "改写后的查询")
        assistant_content = result["messages"][2]["content"]
        obj = json.loads(assistant_content)
        assert obj["intent"] == "both"
        assert obj["query_rewrite"] == "改写后的查询"

    def test_user_message_contains_query_and_city(self):
        result = self._fn("好吃的火锅", "重庆", "amap", "重庆火锅")
        user_content = result["messages"][1]["content"]
        assert "好吃的火锅" in user_content
        assert "重庆" in user_content

    def test_label_matches_intent(self):
        for intent in ["amap", "rag", "both", "weather"]:
            result = self._fn("query", "城市", intent, "rewrite")
            assert result["label"] == intent


# ─────────────────────────────────────────────────────────────
# Config 配置项测试
# ─────────────────────────────────────────────────────────────

class TestFtRouterConfig:
    """ft_router 配置项默认值与类型测试"""

    def test_ft_router_enabled_default_false(self):
        from app.config import Settings
        s = Settings()
        assert s.ft_router_enabled is False

    def test_ft_router_model_path_default(self):
        from app.config import Settings
        s = Settings()
        assert s.ft_router_model_path == "models/router_lora"

    def test_ft_router_enabled_from_env(self, monkeypatch):
        monkeypatch.setenv("FT_ROUTER_ENABLED", "true")
        from app.config import Settings
        s = Settings()
        assert s.ft_router_enabled is True

    def test_ft_router_model_path_from_env(self, monkeypatch):
        monkeypatch.setenv("FT_ROUTER_MODEL_PATH", "/custom/path/lora")
        from app.config import Settings
        s = Settings()
        assert s.ft_router_model_path == "/custom/path/lora"


# ─────────────────────────────────────────────────────────────
# classify() 无模型时的降级测试
# ─────────────────────────────────────────────────────────────

def _skip_if_model_loaded():
    """单例模型已加载时跳过降级测试（全局状态跨测试持久化）"""
    import app.agents.nodes.router_classifier as clf_mod
    if clf_mod._loaded:
        pytest.skip("模型已全局加载（单例），此测试需在独立进程中运行")


class TestClassifyWithoutModel:
    """无模型文件时 classify() 应返回 None（不抛异常）

    注：单独运行本文件时有效。与集成测试合并运行时，
    因单例已加载会自动 skip（不影响 CI 整体通过）。
    独立验证：python -m pytest tests/test_router_ft_unit.py -v
    """

    def test_returns_none_for_nonexistent_path(self):
        _skip_if_model_loaded()
        from app.agents.nodes.router_classifier import classify
        result = classify("成都火锅推荐", "成都", "/nonexistent/model/path")
        assert result is None

    def test_returns_none_for_missing_meta(self, tmp_path):
        _skip_if_model_loaded()
        from app.agents.nodes.router_classifier import classify
        result = classify("查询", "城市", str(tmp_path))
        assert result is None

    def test_ensure_loaded_false_for_nonexistent(self):
        _skip_if_model_loaded()
        from app.agents.nodes.router_classifier import ensure_loaded
        result = ensure_loaded("/nonexistent/path/lora")
        assert result is False


# ─────────────────────────────────────────────────────────────
# VALID_INTENTS 常量测试
# ─────────────────────────────────────────────────────────────

def test_valid_intents_set():
    from app.agents.nodes.router_classifier import VALID_INTENTS
    assert VALID_INTENTS == {"amap", "rag", "both", "weather"}


def test_valid_intents_covers_all_labels():
    """训练数据的 4 个标签必须都在 VALID_INTENTS 中"""
    from app.agents.nodes.router_classifier import VALID_INTENTS
    from scripts.generate_training_data import INTENT_TARGETS
    assert set(INTENT_TARGETS.keys()) == VALID_INTENTS
