"""
RAGRetrieval 节点：Advanced RAG Pipeline

完整检索流程
-----------
1. HyDE 查询扩展（意图感知，可选）
   - 用 DeepSeek 生成一段"假设性游记片段"
   - 根据 intent 选择专项 prompt（hotel/food/tips/transport）
   - 对假设文档而非原始查询做 embedding

2. Multi-Query 展开（可选，通过 MULTI_QUERY_ENABLED 控制）
   - LLM 将查询改写为 3 条语义互补的子查询
   - 并行检索所有子查询，全局 RRF 融合 → 覆盖更广的相关文档
   - 特别适合 hotel/tips 类词汇多样性高的查询

3. 混合检索（并行）
   - Dense 路 ：query_vector → pgvector 余弦相似度 → top-30
   - Sparse 路：jieba 分词 → PostgreSQL tsvector BM25 → top-30

4. RRF 融合（Reciprocal Rank Fusion）
   - 无需调参，对两路结果做倒数排名加权融合 → top-20

5. Cross-Encoder 重排序
   - BAAI/bge-reranker-v2-m3 本地推理（GPU）
   - 对 (query, doc) 精细打分 → top-8

输出 chunk 结构
---------------
{
    "content"          : str,         # 游记段落文本
    "place_ids"        : list[str],   # 关联高德 POI IDs
    "note_id"          : str,         # 源游记 ID（可溯源）
    "similarity"       : float,       # 综合相关度分（兼容旧字段）
    "rrf_score"        : float,       # RRF 融合分数
    "rerank_score"     : float|None,  # Cross-Encoder 分数（reranker 可用时）
    "retrieval_sources": list[str],   # ["dense"] | ["sparse"] | ["dense","sparse"]
}

Context Recall 改善历史（2026-05）
-----------------------------------
- _DENSE_TOP_K 20→30, _SPARSE_TOP_K 20→30, _RRF_TOP_K 10→20, _RERANK_TOP_K 5→8
- Intent-aware HyDE：hotel/food/tips/transport 专项 System Prompt
- Multi-Query 展开：3 条子查询并行检索，全局 RRF 合并（适合 hotel/tips）
"""

from app.agents.state import AgentState
from app.config import settings
from app.rag.embedder import embed_text
from app.rag.hyde import generate_hypothetical_doc
from app.rag.retriever import hybrid_search
from app.rag.reranker import rerank
from app.rag.multi_query import multi_query_search

# 已知城市列表（与 amap_search.py 保持一致）
_KNOWN_CITIES = [
    "北京", "上海", "成都", "厦门", "广州",
    "深圳", "杭州", "西安", "重庆", "南京",
]

# 各阶段候选数
# Context Recall 改善（2026-05）：扩大候选池以提升关键信息覆盖率
# _RRF_TOP_K 20 → 更多候选进入 reranker，_RERANK_TOP_K 5→8 多传给 Synthesizer
_DENSE_TOP_K = 30   # 20 → 30，扩大 dense 候选
_SPARSE_TOP_K = 30  # 20 → 30，扩大 sparse 候选（BM25 对精确关键词命中尤其重要）
_RRF_TOP_K = 20     # 10 → 20，进入 reranker 的候选数翻倍
_RERANK_TOP_K = 8   # 5 → 8，最终传给 Synthesizer 的 chunks 增加，提升 recall 覆盖

# Multi-Query 启用阈值：hotel/tips 意图默认启用（词汇多样性高，收益最大）
# 其他意图可通过 env MULTI_QUERY_ENABLED=true 强制开启
_MULTI_QUERY_INTENTS = {"hotel", "tips", "food"}  # 这些意图自动走多查询路径


def _extract_city(state: AgentState) -> str:
    """
    城市提取优先级：
    1. state.trip_city（从 ChatRequest 或 eval 直接传入，最可靠）
    2. 逆序扫描消息文本（用于会话历史推断）
    3. 默认成都
    """
    if trip_city := state.get("trip_city"):
        return trip_city
    for msg in reversed(state["messages"]):
        content = str(msg.content)
        for city in _KNOWN_CITIES:
            if city in content:
                return city
    return "成都"  # 默认城市（Demo 主城市）


async def run(state: AgentState) -> dict:
    """
    RAGRetrieval 节点入口

    HyDE → embedding → 混合检索（dense+sparse）→ RRF 融合 → reranker 精排
    """
    query = state.get("query_rewrite") or ""
    city = _extract_city(state)

    if not query:
        return {"rag_chunks": []}

    # Demo 模式：跳过所有外部调用
    if settings.demo_mode:
        print("[RAGRetrieval] Demo 模式，返回空 chunks")
        return {"rag_chunks": []}

    # 无 API Key：优雅降级
    has_key = bool(settings.effective_embedding_api_key)
    if not has_key:
        print("[RAGRetrieval] 未配置 Embedding API Key，跳过 RAG 检索")
        return {"rag_chunks": []}

    # 从 state 中提取意图（Router 节点设置的 intent 字段）
    intent = state.get("intent") or ""

    try:
        # ── 路径选择：Multi-Query vs 单路检索 ───────────────────────────
        use_multi_query = (
            intent in _MULTI_QUERY_INTENTS
            or getattr(settings, "multi_query_enabled", False)
        )

        if use_multi_query:
            # ── Multi-Query 路径 ─────────────────────────────────────────
            # 1. LLM 展开 3 条子查询
            # 2. 每条子查询独立 HyDE + hybrid_search
            # 3. 全局 RRF 融合 → top-20
            print(f"[RAGRetrieval] Multi-Query 模式（intent={intent}）")
            fused = await multi_query_search(
                query=query,
                city=city,
                intent=intent,
                dense_top_k=_DENSE_TOP_K,
                sparse_top_k=_SPARSE_TOP_K,
                n_queries=3,
                final_top_k=_RRF_TOP_K,
            )
        else:
            # ── 单路检索路径（scenic / transport / 默认）─────────────────
            # Step 1：Intent-aware HyDE 查询扩展
            embed_input = await generate_hypothetical_doc(query, city, intent)

            # Step 2：生成查询向量
            query_vector = await embed_text(embed_input)

            # Step 3：混合检索 + RRF 融合 → top-20
            fused = await hybrid_search(
                query_vector=query_vector,
                query_text=query,           # BM25 始终用原始 query（非假设文档）
                city=city,
                dense_top_k=_DENSE_TOP_K,
                sparse_top_k=_SPARSE_TOP_K,
                rrf_top_k=_RRF_TOP_K,
            )

        if not fused:
            print(f"[RAGRetrieval] city={city} 无命中结果")
            return {"rag_chunks": []}

        # ── Step 4：Cross-Encoder 重排序 ─────────────────────────────────
        # reranker_enabled=False 或 FlagEmbedding 未安装时自动跳过
        if settings.reranker_enabled:
            final = rerank(
                query=query,
                candidates=fused,
                top_k=_RERANK_TOP_K,
                model_name=settings.reranker_model,
                device=settings.reranker_device,
            )
        else:
            final = fused[:_RERANK_TOP_K]

        # ── 格式化输出（兼容 Synthesizer 的旧字段 similarity） ───────────
        chunks = [
            {
                "content": doc["content"],
                "place_ids": doc["place_ids"],
                "note_id": doc["note_id"],
                # similarity 字段：优先用 rerank_score，回退 rrf_score
                "similarity": doc.get("rerank_score") or doc.get("rrf_score", 0.0),
                "rrf_score": doc.get("rrf_score", 0.0),
                "rerank_score": doc.get("rerank_score"),
                "retrieval_sources": doc.get("retrieval_sources", ["dense"]),
            }
            for doc in final
        ]

        print(
            f"[RAGRetrieval] 完成：city={city}, query={query[:30]}..., "
            f"返回 {len(chunks)} 条 chunks"
        )
        return {"rag_chunks": chunks}

    except Exception as exc:
        print(f"[RAGRetrieval] 检索失败，返回空 chunks：{exc}")
        return {"rag_chunks": []}
