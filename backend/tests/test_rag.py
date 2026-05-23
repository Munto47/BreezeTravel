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
                result = await generate_hypothetical_doc("成都附近有哪些适合亲子的自然景区", "成都")
            return result

        result = asyncio.run(_run())
        assert result == "成都附近有哪些适合亲子的自然景区"

    def test_disabled_hyde(self):
        """HYDE_ENABLED=false 时，直接返回原始 query"""

        async def _run():
            from app.rag.hyde import generate_hypothetical_doc
            from app.config import settings

            with patch.object(settings, "hyde_enabled", False):
                result = await generate_hypothetical_doc("北京有哪些历史文化景点", "北京")
            return result

        result = asyncio.run(_run())
        assert result == "北京有哪些历史文化景点"


class TestHyDEQueryRouting:
    """HyDE 查询路由：短查询和精确地名应跳过假设文档生成"""

    def test_short_query_skips_hyde(self):
        """查询长度 < 12 字时跳过 HyDE"""
        from app.rag.hyde import _should_skip_hyde

        assert _should_skip_hyde("西湖") is True
        assert _should_skip_hyde("故宫") is True
        assert _should_skip_hyde("鼓浪屿") is True

    def test_known_landmark_skips_hyde(self):
        """精确已知地标名跳过 HyDE"""
        from app.rag.hyde import _should_skip_hyde

        assert _should_skip_hyde("颐和园") is True
        assert _should_skip_hyde("陈家祠") is True
        assert _should_skip_hyde("灵隐寺") is True

    def test_long_descriptive_query_uses_hyde(self):
        """长描述性查询应使用 HyDE（不跳过）"""
        from app.rag.hyde import _should_skip_hyde

        assert _should_skip_hyde("成都附近有哪些适合亲子家庭的自然景区推荐") is False
        assert _should_skip_hyde("深圳大鹏半岛一日游最佳路线安排") is False
        assert _should_skip_hyde("杭州西湖周边有哪些特色民宿值得住") is False


# ═══════════════════════════════════════════════════════════════════
# 2. RAGAS 集成评估（需要 API Key + 已入库游记数据）
# ═══════════════════════════════════════════════════════════════════

# 评估集：21 条问题覆盖 7 城市 × 3 意图类型（景点/美食/交通住宿避坑）
# 样本量足够支撑 RAGAS 指标的统计可靠性
_EVAL_DATASET = [
    # ── 成都（3条）─────────────────────────────────────────────────────
    {
        "question": "成都宽窄巷子适合哪些旅行者？有什么体验推荐？",
        "ground_truth": "宽窄巷子适合亲子和文化爱好者，可以体验成都茶馆文化、品尝小吃、观看变脸表演。建议工作日早上 9 点前进入人少，下午人流密集。",
        "city": "成都",
        "intent": "scenic",
    },
    {
        "question": "成都有哪些值得打卡的特色火锅餐厅？",
        "ground_truth": "成都火锅以麻辣著称，推荐巴奴毛肚火锅（食材新鲜）、蜀大侠（性价比高）和大龙燚（口味正宗）。用餐高峰建议提前 1 小时取号。",
        "city": "成都",
        "intent": "food",
    },
    {
        "question": "从成都市区去都江堰怎么最方便？",
        "ground_truth": "从成都市区搭乘高铁到都江堰北站约 40 分钟，再打车或步行约 10 分钟到景区门口。也可以拼车或跟团一日游。",
        "city": "成都",
        "intent": "transport",
    },
    # ── 北京（3条）─────────────────────────────────────────────────────
    {
        "question": "北京颐和园游览需要多长时间？有什么参观建议？",
        "ground_truth": "颐和园面积大，轻松游览需 3-4 小时，完整游览需 1 天。建议从东宫门入，先游仁寿殿区域再沿长廊至万寿山，旺季提前网上预约门票。",
        "city": "北京",
        "intent": "scenic",
    },
    {
        "question": "北京初次旅游住在哪个区交通最方便？",
        "ground_truth": "初次游北京推荐住东城区（靠近故宫、天安门）或西城区（靠近南锣鼓巷）；地铁 2 号线沿线酒店性价比高，出行最便利。",
        "city": "北京",
        "intent": "hotel",
    },
    {
        "question": "去北京旅游最需要注意哪些避坑事项？",
        "ground_truth": "主要避坑：天安门、故宫必须提前网上预约；长城旺季排队 2 小时以上建议工作日去；北京春秋天气变化大，多带外套。",
        "city": "北京",
        "intent": "tips",
    },
    # ── 上海（3条）─────────────────────────────────────────────────────
    {
        "question": "上海豫园周边有哪些地道小吃值得尝？",
        "ground_truth": "豫园周边有南翔馒头店的小笼包、绿波廊的八宝饭、城隍庙梨膏糖等，建议平日下午前往人少，周末人流量极大需提前排队。",
        "city": "上海",
        "intent": "food",
    },
    {
        "question": "上海外滩附近住哪里比较方便？有哪些推荐？",
        "ground_truth": "外滩附近推荐住黄浦区或静安区，步行可达南京东路，交通便利。和平饭店等老牌酒店值得体验但价格较高；预算有限可选南京东路附近 B&B。",
        "city": "上海",
        "intent": "hotel",
    },
    {
        "question": "从上海虹桥火车站去浦东机场最快怎么走？",
        "ground_truth": "虹桥到浦东机场：地铁 2 号线全程约 70 分钟；打车约 40-50 分钟费用 80-120 元；磁悬浮从龙阳路站出发仅 8 分钟但需换乘地铁。",
        "city": "上海",
        "intent": "transport",
    },
    # ── 厦门（3条）─────────────────────────────────────────────────────
    {
        "question": "厦门鼓浪屿有哪些不能错过的景点？",
        "ground_truth": "鼓浪屿必游：日光岩（最高点可俯瞰全岛）、菽庄花园（临海园林）、钢琴博物馆（免费）和龙头路小吃街。建议上午 8 点前上岛避开人群。",
        "city": "厦门",
        "intent": "scenic",
    },
    {
        "question": "厦门哪里可以吃到最正宗的沙茶面和海蛎煎？",
        "ground_truth": "厦门沙茶面推荐中山路周边老字号，如好清香、黄则和；曾厝垵有多家口碑好的海蛎煎摊位，价格 15-25 元一份性价比高。",
        "city": "厦门",
        "intent": "food",
    },
    {
        "question": "去鼓浪屿旅游有哪些必须知道的注意事项？",
        "ground_truth": "鼓浪屿注意：渡轮票旺季提前在厦门轮渡 App 预约；岛上禁止电动车；住宿价格旺季翻倍，提前一个月预订；岛上景点步行为主需穿舒适鞋。",
        "city": "厦门",
        "intent": "tips",
    },
    # ── 广州（3条）─────────────────────────────────────────────────────
    {
        "question": "广州哪里可以喝到最正宗的早茶？有哪些推荐？",
        "ground_truth": "广州早茶推荐陶陶居（荔湾区老字号）、莲香楼（中山四路）、点都德（连锁，虾饺皮薄馅足）。早茶时间 7-11 点，周末排队 1 小时以上，工作日最舒适。",
        "city": "广州",
        "intent": "food",
    },
    {
        "question": "广州有哪些值得游览的历史文化景点？",
        "ground_truth": "推荐陈家祠（清代岭南建筑精华，建筑雕刻精美）、广州塔（小蛮腰，登顶俯瞰全城）、沙面岛（欧陆风情租界建筑群）和越秀公园（五羊雕塑，免费）。",
        "city": "广州",
        "intent": "scenic",
    },
    {
        "question": "从广州白云机场去市区最方便的交通方式是什么？",
        "ground_truth": "白云机场到市区：地铁 3 号线北延线直达广州南/体育西约 40 分钟最经济；机场快线大巴约 60 分钟至流花路；打车约 40-60 分钟费用 80-120 元。",
        "city": "广州",
        "intent": "transport",
    },
    # ── 深圳（3条）─────────────────────────────────────────────────────
    {
        "question": "深圳大鹏半岛适合什么类型的旅行者？怎么安排行程？",
        "ground_truth": "大鹏半岛适合户外徒步、海钓和亲子露营爱好者。杨梅坑、西涌沙滩是热门目的地；东西冲穿越全程约 4-5 小时适合有一定体力的徒步者，建议结伴而行。",
        "city": "深圳",
        "intent": "scenic",
    },
    {
        "question": "深圳有哪些必吃的特色美食和值得去的夜市？",
        "ground_truth": "深圳美食推荐华强北的猪脚饭（20 元超满足）、沙井蚝（新鲜烤蚝）、东门老街的肠粉和牛杂。华强北周边夜市最热闹，体验深圳烟火气的好地方。",
        "city": "深圳",
        "intent": "food",
    },
    {
        "question": "深圳哪个区域住宿性价比最高？适合什么类型的游客？",
        "ground_truth": "性价比最高：罗湖（靠近东门商圈和罗湖口岸，去香港方便）；福田（CBD 附近，商务出行首选）；南山（科技园和欢乐海岸周边，适合休闲游客）。",
        "city": "深圳",
        "intent": "hotel",
    },
    # ── 杭州（3条）─────────────────────────────────────────────────────
    {
        "question": "杭州西湖有哪些值得游览的免费景点？",
        "ground_truth": "西湖免费景点：苏堤春晓、白堤断桥、花港观鱼、曲院风荷均免费；三潭映月景区需购票；雷峰塔和岳王庙付费但值得。工作日上午游客少光线好。",
        "city": "杭州",
        "intent": "scenic",
    },
    {
        "question": "杭州特色美食除了东坡肉还有哪些必尝？",
        "ground_truth": "杭州必尝美食：西湖醋鱼（楼外楼最正宗但贵，清河坊有平价版）、龙井虾仁（龙井村农家乐更实惠）、姐弟俩土豆粉、知味观小笼包。",
        "city": "杭州",
        "intent": "food",
    },
    {
        "question": "杭州旅游有哪些容易踩坑的注意事项？",
        "ground_truth": "主要避坑：雷峰塔内部体验一般，腿脚不便者慎爬；乌镇需一整天游览，门票含西栅；西湖周边停车极难，推荐地铁或共享单车代步；旺季住宿提前一周预订。",
        "city": "杭州",
        "intent": "tips",
    },
]


@pytest.mark.asyncio
@pytest.mark.skip(reason="需要 API Key + 已入库游记数据，手动运行：pytest -v -s -k evaluate_rag_pipeline")
async def test_evaluate_rag_pipeline():
    """
    RAGAS 端到端评估（21 条样本，7 城市 × 3 意图类型）

    运行前提：
      1. 设置 DEEPSEEK_API_KEY 或 OPENAI_API_KEY
      2. 已运行 python -m scripts.ingest_notes 入库游记（7 城市 × 50 篇）
      3. pip install ragas datasets

    目标指标（基于 200+ 篇游记）：
      Faithfulness      ≥ 0.75
      Answer Relevancy  ≥ 0.75
      Context Recall    ≥ 0.65   ← 扩大数据量后的目标（原 0.40）

    输出示例：
      ┌──────────────────────┬──────────┬──────────────┐
      │ Metric               │ 原先(5条)│ 当前(21条)   │
      ├──────────────────────┼──────────┼──────────────┤
      │ faithfulness         │  0.82    │  ?           │
      │ answer_relevancy     │  0.79    │  ?           │
      │ context_recall       │  0.40    │  目标≥0.65   │
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
    city_stats: dict[str, list[float]] = {}  # 按城市统计 Context Recall

    for item in _EVAL_DATASET:
        question = item["question"]
        # 优先从评估集字段取城市，回退到文本推断
        city = item.get("city") or _infer_city(question)

        state: AgentState = {
            "messages": [HumanMessage(content=question)],
            "thread_id": "eval-test",
            "user_id": "eval",
            "trip_city": city,
            "intent": "rag",
            "query_rewrite": question,
            "amap_places": [],
            "rag_chunks": [],
            "synthesized_places": [],
            "final_response": None,
            "itinerary": None,
            "selected_place_ids": [],
        }

        rag_result = await rag_retrieval.run(state)
        state.update(rag_result)

        synth_result = await synthesizer.run(state)
        answer = synth_result.get("final_response", "")

        contexts = [c["content"] for c in state.get("rag_chunks", [])]

        results_advanced.append({
            "question": question,
            "answer": answer,
            "contexts": contexts if contexts else ["无相关上下文"],
            "ground_truth": item["ground_truth"],
            "city": city,
            "intent": item.get("intent", "unknown"),
        })

    if not results_advanced:
        pytest.skip("未获取到任何 RAG 结果（可能游记未入库）")

    # 构建 RAGAS Dataset（RAGAS 只需要 question/answer/contexts/ground_truth）
    ragas_records = [
        {k: v for k, v in r.items() if k in {"question", "answer", "contexts", "ground_truth"}}
        for r in results_advanced
    ]
    dataset = Dataset.from_list(ragas_records)

    print("\n\n=== RAGAS 评估开始（21 条 × 7 城市）===")
    score = evaluate(
        dataset=dataset,
        metrics=[faithfulness, answer_relevancy, context_recall],
    )

    faithfulness_val = score["faithfulness"]
    relevancy_val = score["answer_relevancy"]
    recall_val = score["context_recall"]
    avg = (faithfulness_val + relevancy_val + recall_val) / 3

    print("\n=== RAGAS 综合评估结果 ===")
    print(f"  Faithfulness      : {faithfulness_val:.4f}  （目标 ≥ 0.75）{'✅' if faithfulness_val >= 0.75 else '❌'}")
    print(f"  Answer Relevancy  : {relevancy_val:.4f}  （目标 ≥ 0.75）{'✅' if relevancy_val >= 0.75 else '❌'}")
    print(f"  Context Recall    : {recall_val:.4f}  （目标 ≥ 0.65）{'✅' if recall_val >= 0.65 else '❌'}")
    print(f"  综合平均          : {avg:.4f}")

    # 按城市/意图维度分组打印（方便定位弱项）
    print("\n=== 按意图类型分布 ===")
    intent_groups: dict[str, list] = {}
    for r in results_advanced:
        intent = r.get("intent", "unknown")
        intent_groups.setdefault(intent, []).append(r["question"])
    for intent, qs in intent_groups.items():
        print(f"  {intent}: {len(qs)} 条")

    # 基础断言
    assert faithfulness_val > 0.5, f"Faithfulness 过低：{faithfulness_val}"
    assert relevancy_val > 0.5, f"Answer Relevancy 过低：{relevancy_val}"


def _infer_city(question: str) -> str:
    """从问题文本推断城市（回退逻辑，优先使用评估集中的 city 字段）"""
    cities = ["成都", "北京", "上海", "厦门", "广州", "深圳", "杭州"]
    for city in cities:
        if city in question:
            return city
    return "成都"
