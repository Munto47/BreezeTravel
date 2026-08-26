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
from unittest.mock import patch

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
        "ground_truth": "成都火锅以麻辣著称，推荐了具体火锅店名和特色（如毛肚、鹅肠等招牌菜），提到人均消费或性价比，建议用餐高峰期提前排号取号。",
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
        "ground_truth": "初次游北京推荐住故宫、天安门附近区域或南锣鼓巷周边，靠近地铁沿线酒店出行便利、性价比高，提到了具体酒店或民宿名称和价格区间。",
        "city": "北京",
        "intent": "hotel",
    },
    {
        "question": "去北京旅游最需要注意哪些避坑事项？",
        "ground_truth": "主要避坑：故宫等热门景点需提前网上预约；长城旺季排队建议工作日去；提到了交通出行建议和天气穿衣提醒。",
        "city": "北京",
        "intent": "tips",
    },
    # ── 上海（3条）─────────────────────────────────────────────────────
    {
        "question": "上海豫园周边有哪些地道小吃值得尝？",
        "ground_truth": "豫园周边有小笼包老字号和各类小吃（生煎、梨膏糖等），提到了具体店铺名称和招牌菜品，建议避开周末高峰时段，平日前往人少体验好。",
        "city": "上海",
        "intent": "food",
    },
    {
        "question": "上海外滩附近住哪里比较方便？有哪些推荐？",
        "ground_truth": "外滩附近推荐住南京东路周边，步行可达外滩和豫园，交通便利；提到了具体酒店或民宿名称、价格区间，以及不同预算（高端/平价）的选择建议。",
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
        "ground_truth": "厦门沙茶面推荐中山路或八市周边的老字号店铺，提到了具体店名和特色；曾厝垵有口碑好的海蛎煎摊位，提到了价格和排队情况。",
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
        "ground_truth": "广州早茶推荐了具体老字号茶楼名称（荔湾区或老城区），提到虾饺、肠粉等招牌点心，建议早茶时间和排队情况，工作日人少体验更好。",
        "city": "广州",
        "intent": "food",
    },
    {
        "question": "广州有哪些值得游览的历史文化景点？",
        "ground_truth": "推荐陈家祠（岭南建筑精华）、广州塔（小蛮腰）、沙面（欧陆建筑）和越秀公园（免费）等景点，提到了游览建议、门票信息和避坑经验。",
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
        "ground_truth": "深圳不同区域定位不同：口岸附近区域去香港方便、商务区附近适合出差、海岸休闲区适合度假；提到了具体酒店名称和价格区间，以及距地铁站的距离。",
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
        "ground_truth": "杭州必尝美食包括西湖醋鱼、龙井虾仁、片儿川等特色菜，提到了具体餐厅或小吃店名称、人均价格，以及景区店与本地老字号的性价比对比。",
        "city": "杭州",
        "intent": "food",
    },
    {
        "question": "杭州旅游有哪些容易踩坑的注意事项？",
        "ground_truth": "主要避坑：部分景点（如塔类）内部体验一般，需合理安排时间；西湖周边交通拥堵建议地铁或骑行；旺季住宿价格翻倍需提前预订；提到了具体踩坑经历和省钱技巧。",
        "city": "杭州",
        "intent": "tips",
    },
    # ── 补充 hotel 样本（Context Recall 改善，2026-05）─────────────────────────
    # hotel 意图原得分最低（0.22），补充至 6 条提高统计可靠性
    {
        "question": "成都旅游住在哪里最方便？春熙路附近有什么好住的？",
        "ground_truth": "成都推荐住春熙路或太古里周边，步行可达核心景点；提到了具体酒店或民宿名称、不同档位价格区间（青旅/中端/高端），以及预订建议。",
        "city": "成都",
        "intent": "hotel",
    },
    {
        "question": "广州旅游第一次去住哪里合适？性价比高的区域推荐",
        "ground_truth": "广州初次游推荐住地铁沿线便利区域（靠近北京路步行街或陈家祠），提到了具体酒店或民宿名称和价格区间，老城区适合体验老广氛围。",
        "city": "广州",
        "intent": "hotel",
    },
    {
        "question": "杭州西湖周边哪些民宿值得住？有什么预订建议？",
        "ground_truth": "西湖周边民宿推荐湖景沿线或龙井村附近区域，提到了具体民宿或酒店名称、价格区间（平价/高端），旺季需提前预订，节假日价格翻倍。",
        "city": "杭州",
        "intent": "hotel",
    },
    # ── 补充 tips 样本（Context Recall 改善，2026-05）─────────────────────────
    # tips 意图得分 0.33，补充至 6 条覆盖更多城市
    {
        "question": "第一次去成都旅游最需要注意哪些避坑事项？",
        "ground_truth": "成都主要避坑：熊猫基地建议早上入园（下午熊猫睡觉）；热门景点需提前预约；火锅店避开景区周边选本地人去的巷子店；提到了具体踩坑经历和交通出行建议。",
        "city": "成都",
        "intent": "tips",
    },
    {
        "question": "去上海旅游有哪些常见踩坑点？怎么避免？",
        "ground_truth": "上海避坑：热门景点旺季需提前预订，节假日人流极大；外滩拍照有最佳时间建议；景区周边餐饮偏贵性价比低；提到了具体踩坑经历和省钱的交通或餐饮替代方案。",
        "city": "上海",
        "intent": "tips",
    },
    {
        "question": "深圳旅游需要注意哪些事项？有哪些容易踩的坑？",
        "ground_truth": "深圳主要避坑：热门海滩周末极堵建议周中出行；华强北购物需谨慎避免被宰；提到了具体踩坑经历、最佳游览季节建议和交通出行提醒。",
        "city": "深圳",
        "intent": "tips",
    },
]


@pytest.mark.asyncio
@pytest.mark.skip(reason="需要 API Key + 已入库游记数据，手动运行：pytest -v -s -k evaluate_rag_pipeline")
async def test_evaluate_rag_pipeline():
    """
    RAGAS 端到端评估（27 条样本，7 城市 × 多意图类型）

    数据集组成（2026-05 扩充后）：
      scenic    : 6 条（均匀覆盖 7 城市）
      food      : 6 条
      transport : 3 条
      hotel     : 6 条（原 3 条，补充成都/广州/杭州）← Context Recall 最弱项
      tips      : 6 条（原 3 条，补充成都/上海/深圳）← Context Recall 次弱项

    运行前提：
      1. 设置 DEEPSEEK_API_KEY 或 OPENAI_API_KEY
      2. 已运行 python -m scripts.ingest_notes 入库游记
         （7 城市 × 50 篇常规 + hotel/tips/food 专项游记）
      3. pip install ragas datasets

    目标指标（Context Recall 改善方案实施后）：
      Faithfulness      ≥ 0.75   （基线 0.88，继续保持）
      Answer Relevancy  ≥ 0.75   （基线 0.91，继续保持）
      Context Recall    ≥ 0.65   ← 改善目标（基线 0.41）

    改善方案汇总：
      1. 检索 top-k 扩大：_RRF_TOP_K 10→20，_RERANK_TOP_K 5→8
      2. Intent-aware HyDE：hotel/food/tips/transport 专项 System Prompt
      3. Multi-Query 展开：hotel/tips/food 意图自动走 3 子查询并行检索
      4. 专项游记补强：hotel/tips/food 各 8 篇/城（含具体名称/价格/路线）
      5. chunk 粒度优化：CHUNK_SIZE 500→350，OVERLAP 50→100
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

    # 配置 RAGAS 使用 DeepSeek（OpenAI 兼容接口），避免默认 OpenAI() 实例化失败
    from app.config import settings as app_settings
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas.llms import LangchainLLMWrapper
    from ragas.embeddings import LangchainEmbeddingsWrapper

    ragas_llm = LangchainLLMWrapper(ChatOpenAI(
        model=app_settings.llm_model_synthesizer,
        api_key=app_settings.effective_llm_api_key,
        base_url=app_settings.effective_llm_api_url,
        temperature=0,
    ))
    ragas_embeddings = LangchainEmbeddingsWrapper(OpenAIEmbeddings(
        model=app_settings.embedding_model,
        api_key=app_settings.effective_embedding_api_key,
        base_url=app_settings.effective_embedding_api_url,
    ))

    print("\n\n=== RAGAS 评估开始（21 条 × 7 城市）===")
    print(f"  LLM: {app_settings.llm_model_synthesizer} @ {app_settings.effective_llm_api_url}")
    score = evaluate(
        dataset=dataset,
        metrics=[faithfulness, answer_relevancy, context_recall],
        llm=ragas_llm,
        embeddings=ragas_embeddings,
    )

    # RAGAS ≥0.2 返回 EvaluationResult，score[metric] 可能是 list/Series 而非标量
    # 统一用 pandas DataFrame 提取均值，兼容新旧版本
    import statistics

    def _extract_score(score_obj, key: str) -> float:
        """从 RAGAS EvaluationResult 中提取指标均值，兼容新旧 API"""
        val = score_obj[key]
        if isinstance(val, (int, float)):
            return float(val)
        if hasattr(val, "mean"):          # pandas Series / numpy array
            return float(val.mean())
        if isinstance(val, (list, tuple)):
            valid = [v for v in val if v is not None and not (isinstance(v, float) and v != v)]
            return statistics.mean(valid) if valid else 0.0
        # EvaluationResult — 尝试 to_pandas()
        try:
            df = score_obj.to_pandas()
            return float(df[key].mean())
        except Exception:
            return float(val)

    faithfulness_val = _extract_score(score, "faithfulness")
    relevancy_val    = _extract_score(score, "answer_relevancy")
    recall_val       = _extract_score(score, "context_recall")
    avg = (faithfulness_val + relevancy_val + recall_val) / 3

    print("\n=== RAGAS 综合评估结果 ===")
    print(f"  Faithfulness      : {faithfulness_val:.4f}  （目标 ≥ 0.75）{'✅' if faithfulness_val >= 0.75 else '❌'}")
    print(f"  Answer Relevancy  : {relevancy_val:.4f}  （目标 ≥ 0.75）{'✅' if relevancy_val >= 0.75 else '❌'}")
    print(f"  Context Recall    : {recall_val:.4f}  （目标 ≥ 0.65）{'✅' if recall_val >= 0.65 else '❌'}")
    print(f"  综合平均          : {avg:.4f}")

    # 按意图类型分布
    print("\n=== 按意图类型分布 ===")
    intent_groups: dict[str, list] = {}
    for r in results_advanced:
        intent = r.get("intent", "unknown")
        intent_groups.setdefault(intent, []).append(r["question"])
    for intent, qs in intent_groups.items():
        print(f"  {intent}: {len(qs)} 条")

    # 保存结果到 JSON
    import json
    from pathlib import Path
    results_path = Path(__file__).parent.parent / "results" / "ragas_eval.json"
    results_path.parent.mkdir(exist_ok=True)
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump({
            "faithfulness": round(faithfulness_val, 4),
            "answer_relevancy": round(relevancy_val, 4),
            "context_recall": round(recall_val, 4),
            "avg": round(avg, 4),
            "dataset_size": len(results_advanced),
            "cities": 7,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存：{results_path}")

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
