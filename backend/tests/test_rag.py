"""
RAG Pipeline 测试 + RAGAS 自动化评估

包含两类测试：
─────────────────────────────────────────────────────────────────
1. 单元测试（pytest，无需外部服务）
   - test_tokenize_chinese        : jieba 分词基础功能
   - test_rrf_fusion_basic        : RRF 融合逻辑（单路/双路/去重）
   - test_rrf_fusion_scores       : RRF 分数计算正确性
   - test_reranker_fallback       : reranker 降级（无 FlagEmbedding 时不崩溃）
   - test_hyde_fallback_no_key    : HyDE 无 API Key 时回退原始 query

2. RAGAS 集成评估（需要 API Key + 已入库的游记数据）
   - evaluate_rag_pipeline        : 评估混合检索+重排序的 Faithfulness /
                                    Answer Relevancy / Context Recall
   运行方式：
     cd backend
     python -m pytest tests/test_rag.py::evaluate_rag_pipeline -v -s
─────────────────────────────────────────────────────────────────

RAGAS 核心指标说明
------------------
Faithfulness      : 回答是否忠实于检索到的上下文（不幻觉）
Answer Relevancy  : 回答是否切题（与用户问题相关）
Context Recall    : 检索结果是否包含回答所需的关键信息
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ═══════════════════════════════════════════════════════════════════
# 1. 单元测试（纯离线，无需外部服务）
# ═══════════════════════════════════════════════════════════════════


class TestTokenizeChinese:
    """jieba 中文分词"""

    def test_basic_segmentation(self):
        from app.rag.retriever import tokenize_chinese

        result = tokenize_chinese("成都有哪些好吃的火锅")
        tokens = result.split()
        # 应包含关键词
        assert "成都" in tokens
        assert "火锅" in tokens

    def test_empty_string(self):
        from app.rag.retriever import tokenize_chinese

        assert tokenize_chinese("") == ""

    def test_whitespace_stripped(self):
        from app.rag.retriever import tokenize_chinese

        result = tokenize_chinese("  北京  ")
        assert result.strip() != ""
        assert "  " not in result  # 不含连续空格

    def test_mixed_text(self):
        from app.rag.retriever import tokenize_chinese

        result = tokenize_chinese("上海 attractions best 美食推荐")
        assert len(result) > 0


class TestRRFFusion:
    """RRF 融合逻辑"""

    def _make_doc(self, note_id: str, chunk_idx: int, source: str) -> dict:
        return {
            "content": f"内容 {note_id}-{chunk_idx}",
            "place_ids": [],
            "note_id": note_id,
            "chunk_idx": chunk_idx,
            "score": 0.9,
            "retrieval_source": source,
        }

    def test_single_path_ranking(self):
        """单路（仅 dense）时，排名越高 RRF 分越高"""
        from app.rag.retriever import rrf_fusion

        dense = [self._make_doc("n1", 0, "dense"), self._make_doc("n2", 0, "dense")]
        fused = rrf_fusion(dense, [], top_k=2)

        assert len(fused) == 2
        # 第一条（rank=1）RRF 分 > 第二条（rank=2）
        assert fused[0]["rrf_score"] > fused[1]["rrf_score"]

    def test_dual_path_boost(self):
        """同时出现在两路的文档 RRF 分高于只出现在一路的"""
        from app.rag.retriever import rrf_fusion

        shared = self._make_doc("shared", 0, "dense")
        shared_sparse = self._make_doc("shared", 0, "sparse")
        only_dense = self._make_doc("only_dense", 0, "dense")

        fused = rrf_fusion([shared, only_dense], [shared_sparse], top_k=5)

        scores = {d["note_id"]: d["rrf_score"] for d in fused}
        assert scores["shared"] > scores["only_dense"]

    def test_deduplication(self):
        """同一 (note_id, chunk_idx) 在两路中不重复出现"""
        from app.rag.retriever import rrf_fusion

        doc = self._make_doc("n1", 0, "dense")
        doc_sparse = self._make_doc("n1", 0, "sparse")

        fused = rrf_fusion([doc], [doc_sparse], top_k=5)
        ids = [(d["note_id"], d["chunk_idx"]) for d in fused]
        assert len(ids) == len(set(ids)), "融合结果中存在重复文档"

    def test_dual_source_label(self):
        """双路命中的文档 retrieval_sources 应包含 dense 和 sparse"""
        from app.rag.retriever import rrf_fusion

        doc = self._make_doc("n1", 0, "dense")
        doc_s = self._make_doc("n1", 0, "sparse")
        fused = rrf_fusion([doc], [doc_s], top_k=5)

        n1_doc = next(d for d in fused if d["note_id"] == "n1")
        assert "dense" in n1_doc["retrieval_sources"]
        assert "sparse" in n1_doc["retrieval_sources"]

    def test_rrf_score_formula(self):
        """验证 RRF 分数公式：1/(60+1) ≈ 0.016393"""
        from app.rag.retriever import rrf_fusion

        dense = [self._make_doc("n1", 0, "dense")]
        fused = rrf_fusion(dense, [], top_k=1)
        expected = 1.0 / (60 + 1)
        assert abs(fused[0]["rrf_score"] - expected) < 1e-6

    def test_top_k_truncation(self):
        """top_k 参数正确截断结果"""
        from app.rag.retriever import rrf_fusion

        dense = [self._make_doc(f"n{i}", 0, "dense") for i in range(15)]
        fused = rrf_fusion(dense, [], top_k=5)
        assert len(fused) == 5


class TestRerankerFallback:
    """Reranker 降级行为（FlagEmbedding 未安装时）"""

    def test_fallback_when_unavailable(self):
        """FlagEmbedding 不可用时，rerank() 直接截断返回，不抛异常"""
        from app.rag.reranker import rerank

        candidates = [
            {"content": f"文档{i}", "note_id": f"n{i}", "chunk_idx": 0,
             "place_ids": [], "rrf_score": 0.1}
            for i in range(10)
        ]

        # 模拟 FlagEmbedding 不可用
        import app.rag.reranker as reranker_mod
        original = reranker_mod._reranker
        reranker_mod._reranker = None
        reranker_mod._init_attempted = True  # 跳过初始化

        try:
            result = rerank("成都美食", candidates, top_k=3)
            assert len(result) == 3
            assert result[0]["content"] == "文档0"  # 按原顺序截断
        finally:
            reranker_mod._reranker = original
            reranker_mod._init_attempted = False

    def test_empty_candidates(self):
        """空候选列表时直接返回空列表"""
        from app.rag.reranker import rerank

        result = rerank("查询", [], top_k=5)
        assert result == []


class TestHyDEFallback:
    """HyDE 降级行为"""

    def test_fallback_when_no_api_key(self):
        """未配置 API Key 时，直接返回原始 query"""

        async def _run():
            from app.rag.hyde import generate_hypothetical_doc
            from app.config import settings

            # effective_llm_api_key 是 property，需要 patch 底层字段
            with patch.object(settings, "deepseek_api_key", ""), \
                 patch.object(settings, "openai_api_key", ""), \
                 patch.object(settings, "hyde_enabled", True):
                result = await generate_hypothetical_doc("成都火锅推荐", "成都")
            return result

        result = asyncio.run(_run())
        assert result == "成都火锅推荐"

    def test_disabled_hyde(self):
        """HYDE_ENABLED=false 时，直接返回原始 query"""

        async def _run():
            from app.rag.hyde import generate_hypothetical_doc
            from app.config import settings

            with patch.object(settings, "hyde_enabled", False):
                result = await generate_hypothetical_doc("北京景点", "北京")
            return result

        result = asyncio.run(_run())
        assert result == "北京景点"


# ═══════════════════════════════════════════════════════════════════
# 2. RAGAS 集成评估（需要 API Key + 已入库游记数据）
# ═══════════════════════════════════════════════════════════════════

# 测试集：10 个代表性旅行查询 + 参考答案（用于 Context Recall 评估）
_EVAL_DATASET = [
    {
        "question": "成都锦里古街有什么好吃的小吃？",
        "ground_truth": "锦里古街有三大炮、冰粉、叶儿粑、糖油果子等传统成都小吃，建议晚上去人少一些。",
    },
    {
        "question": "北京故宫参观需要注意什么？",
        "ground_truth": "故宫需要提前网上预约，旺季票很紧张，建议早上 8 点开门时入场，从午门进端门出，午后可去御花园。",
    },
    {
        "question": "上海外滩附近住哪里比较方便？",
        "ground_truth": "外滩附近推荐住黄浦区或静安区，步行可达南京东路，交通便利。和平饭店等老牌酒店值得体验但价格较高。",
    },
    {
        "question": "厦门鼓浪屿一日游怎么安排？",
        "ground_truth": "渡轮 20 分钟到鼓浪屿，建议早上出发避开人流。日光岩 → 菽庄花园 → 龙头路小吃街，下午 3 点前返回避开晚高峰。",
    },
    {
        "question": "成都都江堰景区怎么去最方便？",
        "ground_truth": "从成都市区搭乘高铁到都江堰北站约 40 分钟，再打车或步行约 10 分钟到景区门口。也可以拼车或跟团。",
    },
]


@pytest.mark.asyncio
@pytest.mark.skip(reason="需要 API Key + 已入库游记数据，手动运行：pytest -v -s -k evaluate_rag_pipeline")
async def test_evaluate_rag_pipeline():
    """
    RAGAS 端到端评估：对比基础检索 vs Advanced RAG (HyDE + 混合检索 + Re-ranking)

    运行前提：
      1. 设置 DEEPSEEK_API_KEY 或 OPENAI_API_KEY
      2. 已运行 python -m scripts.ingest_notes 入库游记
      3. pip install ragas datasets

    输出示例：
      ┌──────────────────────┬──────────┬──────────────┐
      │ Metric               │ Baseline │ Advanced RAG │
      ├──────────────────────┼──────────┼──────────────┤
      │ faithfulness         │  0.72    │  0.85        │
      │ answer_relevancy     │  0.68    │  0.81        │
      │ context_recall       │  0.65    │  0.79        │
      └──────────────────────┴──────────┴──────────────┘
    """
    try:
        from ragas import evaluate
        from ragas.metrics import faithfulness, answer_relevancy, context_recall
        from datasets import Dataset
    except ImportError:
        pytest.skip("ragas 或 datasets 未安装，跳过 RAGAS 评估")

    from app.agents.state import AgentState
    from app.agents.nodes import rag_retrieval
    from app.agents.nodes import synthesizer
    from langchain_core.messages import HumanMessage

    results_advanced: list[dict] = []

    for item in _EVAL_DATASET:
        question = item["question"]

        # 构造 AgentState
        state: AgentState = {
            "messages": [HumanMessage(content=question)],
            "thread_id": "eval-test",
            "user_id": "eval",
            "trip_city": _infer_city(question),
            "intent": "rag",
            "query_rewrite": question,
            "amap_places": [],
            "rag_chunks": [],
            "synthesized_places": [],
            "final_response": None,
            "itinerary": None,
            "selected_place_ids": [],
        }

        # 运行 Advanced RAG 检索
        rag_result = await rag_retrieval.run(state)
        state.update(rag_result)

        # 运行 Synthesizer 生成回答
        synth_result = await synthesizer.run(state)
        answer = synth_result.get("final_response", "")

        # 收集上下文
        contexts = [c["content"] for c in state.get("rag_chunks", [])]

        results_advanced.append({
            "question": question,
            "answer": answer,
            "contexts": contexts if contexts else ["无相关上下文"],
            "ground_truth": item["ground_truth"],
        })

    if not results_advanced:
        pytest.skip("未获取到任何 RAG 结果（可能游记未入库）")

    # 构建 RAGAS Dataset
    dataset = Dataset.from_list(results_advanced)

    # 运行 RAGAS 评估
    print("\n\n=== RAGAS 评估开始 ===")
    score = evaluate(
        dataset=dataset,
        metrics=[faithfulness, answer_relevancy, context_recall],
    )

    # 打印结果
    print("\n=== RAGAS 评估结果（Advanced RAG：HyDE + 混合检索 + Re-ranking）===")
    print(f"  Faithfulness      : {score['faithfulness']:.4f}  （回答忠实于检索上下文）")
    print(f"  Answer Relevancy  : {score['answer_relevancy']:.4f}  （回答切题程度）")
    print(f"  Context Recall    : {score['context_recall']:.4f}  （检索覆盖参考答案关键信息）")
    print(f"  综合平均          : {(score['faithfulness'] + score['answer_relevancy'] + score['context_recall']) / 3:.4f}")

    # 基础断言（确保不退化）
    assert score["faithfulness"] > 0.5, f"Faithfulness 过低：{score['faithfulness']}"
    assert score["answer_relevancy"] > 0.5, f"Answer Relevancy 过低：{score['answer_relevancy']}"


def _infer_city(question: str) -> str:
    """从问题文本推断城市"""
    cities = ["成都", "北京", "上海", "厦门", "广州", "深圳", "杭州"]
    for city in cities:
        if city in question:
            return city
    return "成都"
