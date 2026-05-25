"""
Multi-Query 检索扩展模块

动机
----
单条查询在词汇选择上存在随机性：用户说"酒店"，游记可能写"住宿"、"民宿"、"客栈"；
用户问"怎么去"，游记可能写"出行"、"路线"、"交通"。
Multi-Query 将一条查询改写为多个语义角度不同的子查询，分别检索后 RRF 合并，
覆盖更多相关文档，显著提升 Context Recall。

原理
----
1. 用 LLM 将原始查询改写为 N 条语义互补的子查询（默认 N=3）
2. 每条子查询独立走 HyDE → embedding → dense+sparse hybrid_search
3. 将所有子查询的检索结果合并，再做一次全局 RRF 融合
4. 最终结果去重后送入 reranker 精排

与单路 HyDE 的对比
-----------------
- 单路 HyDE：1 条假设文档 → 1 次检索 → top-20 dense + top-20 sparse
- Multi-Query：3 条子查询 → 3 次检索 → 合并 60+60 → RRF → 覆盖更广

特别适合 hotel / tips 类查询（词汇多样性高、ground_truth 含具体名称）。

参考：LangChain MultiQueryRetriever，RAG-Fusion（Shi et al. 2024）
"""

import asyncio
from openai import AsyncOpenAI
from app.config import settings


# ── 子查询生成 Prompt ─────────────────────────────────────────────────────────

_MQ_SYSTEM = (
    "你是一位旅行信息检索专家。给定一条旅行查询，"
    "请从不同角度改写出 {n} 条语义互补的子查询，"
    "帮助在游记数据库中检索到更全面的信息。\n\n"
    "**硬约束（必须遵守）**：\n"
    "1. 每条子查询必须保留原始查询的**核心品类名词**。\n"
    "   例：原始是「火锅」就只能写不同火锅相关问法（地道火锅 / 火锅推荐 / 火锅排行），\n"
    "   不能漂移到「网红美食」「快餐」「烤肉」「蛋糕」「炒饭」等其他品类。\n"
    "2. 仅允许同义词替换（酒店↔住宿↔民宿↔客栈）和侧面扩展（价格/位置/体验/交通），\n"
    "   不允许把核心名词换成上位词或不同品类。\n\n"
    "格式要求：\n"
    "- 每条子查询独立成行，不加编号或前缀\n"
    "- 保持中文，长度 10-25 字\n"
    "- 只输出子查询，不输出其他任何内容"
)

_MQ_USER = """原始查询：{query}
目的地：{city}

请改写为 {n} 条互补子查询："""


# 各意图类型的默认扩展方向（给 LLM 的额外提示）
_INTENT_HINT: dict[str, str] = {
    "hotel": "侧重：具体酒店名/价格区间/地铁附近/性价比/早餐/房间体验",
    "food":  "侧重：具体餐厅名/招牌菜/人均消费/排队情况/营业时间",
    "tips":  "侧重：预约方式/避坑注意/最佳时间/省钱技巧/常见误区",
    "transport": "侧重：具体线路/换乘方式/时长/票价/出租车费用",
    "scenic": "侧重：游览路线/开放时间/门票/人流量/周边餐饮",
}


async def generate_sub_queries(
    query: str,
    city: str = "",
    intent: str = "",
    n: int = 3,
) -> list[str]:
    """
    用 LLM 将原始查询改写为 N 条语义互补的子查询

    Args:
        query  : 原始用户查询（或 query_rewrite 后的查询）
        city   : 目的地城市
        intent : 意图类型（hotel/food/tips/transport/scenic）
        n      : 生成子查询数量（默认 3，过多会增加延迟）

    Returns:
        子查询列表（含原始查询，共 n+1 条，去重后返回）
        若生成失败则仅返回原始查询 [query]
    """
    if not settings.effective_llm_api_key:
        return [query]

    # 构造带意图提示的 system prompt
    intent_hint = _INTENT_HINT.get(intent, "")
    system = _MQ_SYSTEM.format(n=n)
    if intent_hint:
        system += f"\n\n当前查询意图：{intent_hint}"

    user_content = _MQ_USER.format(
        query=query,
        city=city or "目的地",
        n=n,
    )

    try:
        client = AsyncOpenAI(
            api_key=settings.effective_llm_api_key,
            base_url=settings.effective_llm_api_url,
        )
        resp = await client.chat.completions.create(
            model=settings.llm_model_router,   # 使用 router 模型（低延迟低 cost）
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            max_tokens=200,
            temperature=0.5,
        )
        raw = resp.choices[0].message.content.strip()
        lines = [l.strip() for l in raw.splitlines() if l.strip()]

        # 去重，保留原始查询（顺序：原始在前）
        seen: set[str] = {query}
        sub_queries = [query]
        for line in lines[:n]:
            if line not in seen:
                seen.add(line)
                sub_queries.append(line)

        print(f"[MultiQuery] 生成 {len(sub_queries)} 条子查询（intent={intent or 'default'}）")
        for i, q in enumerate(sub_queries):
            print(f"  [{i}] {q}")
        return sub_queries

    except Exception as exc:
        print(f"[MultiQuery] 子查询生成失败，回退到单查询：{exc}")
        return [query]


# ── 多查询并行检索 + RRF 合并 ─────────────────────────────────────────────────

async def multi_query_search(
    query: str,
    city: str,
    intent: str = "",
    dense_top_k: int = 20,
    sparse_top_k: int = 20,
    n_queries: int = 3,
    final_top_k: int = 20,
) -> list[dict]:
    """
    多查询展开 → 并行检索 → RRF 全局融合

    流程：
      1. generate_sub_queries（1条 → n+1 条）
      2. 每条子查询并行：HyDE embedding → hybrid_search（dense+sparse）
      3. 收集所有子查询的检索结果，做一次全局 RRF 融合
      4. 返回 top final_top_k 条（送入 reranker 精排）

    Args:
        query        : 原始查询
        city         : 目的地城市
        intent       : 意图类型
        dense_top_k  : 每条子查询 dense 候选数
        sparse_top_k : 每条子查询 sparse 候选数
        n_queries    : 生成子查询数（不含原始，共 n+1 条）
        final_top_k  : 全局 RRF 融合后保留条数

    Returns:
        全局 RRF 融合后的文档列表（含 rrf_score, retrieval_sources）
    """
    from app.rag.embedder import embed_text
    from app.rag.hyde import generate_hypothetical_doc
    from app.rag.retriever import dense_search, sparse_search, rrf_fusion

    # Step 1：生成子查询
    sub_queries = await generate_sub_queries(query, city, intent, n=n_queries)

    # Step 2：并行检索所有子查询
    async def _search_one(sub_q: str, q_idx: int) -> tuple[list[dict], list[dict]]:
        """单条子查询的 dense + sparse 检索"""
        try:
            embed_input = await generate_hypothetical_doc(sub_q, city, intent)
            query_vec = await embed_text(embed_input)
            d_res, s_res = await asyncio.gather(
                dense_search(query_vec, city, top_k=dense_top_k),
                sparse_search(sub_q, city, top_k=sparse_top_k),
            )
            # 给每个 doc 打上子查询来源标签，便于调试
            for doc in d_res:
                doc["sub_query_idx"] = q_idx
            for doc in s_res:
                doc["sub_query_idx"] = q_idx
            return d_res, s_res
        except Exception as exc:
            print(f"[MultiQuery] 子查询 [{q_idx}] 检索失败：{exc}")
            return [], []

    tasks = [_search_one(q, i) for i, q in enumerate(sub_queries)]
    all_results = await asyncio.gather(*tasks)

    # Step 3：收集所有子查询结果，按检索路径分别聚合
    # 全局 RRF：将所有 dense 结果视为一路，所有 sparse 结果视为另一路
    # 更细粒度：对每条子查询分别做 RRF，再对子查询级结果做一次 RRF
    all_dense: list[dict] = []
    all_sparse: list[dict] = []
    for d_res, s_res in all_results:
        all_dense.extend(d_res)
        all_sparse.extend(s_res)

    if not all_dense and not all_sparse:
        print("[MultiQuery] 所有子查询均无结果")
        return []

    # Step 4：全局 RRF 融合
    # 注意：all_dense / all_sparse 中可能有同一文档多次出现（来自不同子查询）
    # rrf_fusion 按 (note_id, chunk_idx) 去重并累加 RRF 分，多次命中自然得分更高
    fused = rrf_fusion(all_dense, all_sparse, top_k=final_top_k)

    print(
        f"[MultiQuery] 全局 RRF 完成：{len(sub_queries)} 条子查询，"
        f"dense={len(all_dense)}, sparse={len(all_sparse)}, fused={len(fused)}"
    )
    return fused
