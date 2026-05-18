"""
混合检索模块（Hybrid Search）

两路检索
--------
1. Dense  路：query embedding → pgvector 余弦相似度 → top-20
2. Sparse 路：jieba 中文分词 → PostgreSQL tsvector BM25 → top-20

RRF 融合（Reciprocal Rank Fusion）
-----------------------------------
融合两路结果，取各文档在各路排名的倒数加权和：

  score_RRF(d) = Σ  1 / (k + rank_i(d))

  k = 60（平滑参数，TREC 标准值，防止排名极好的单路文档分数爆炸）
  rank_i = 文档在第 i 路中的位置（1-indexed）

最终按 RRF 分数降序取 top-k 返回。

为何选 RRF
----------
- 无需调参（BM25 分和余弦分量纲不同，线性组合需要仔细调权重）
- 对稀有关键词（BM25 胜出）和语义相近表述（dense 胜出）均有良好召回
- 被大量工业 RAG 系统采用（Elasticsearch、Pinecone 等）

依赖
----
- jieba：pip install jieba
- PostgreSQL travel_notes_chunks 表需有 content_tsv 列及 GIN 索引
  （见 db/init.sql 和 db/migrations/001_add_bm25.sql）
"""

import asyncio

import jieba

from app.db.connection import get_pool

# RRF 平滑参数（TREC 推荐值 60）
_RRF_K = 60


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def tokenize_chinese(text: str) -> str:
    """
    jieba 精确模式分词，返回空格分隔的词语串。
    示例："成都有哪些好吃的" → "成都 有 哪些 好吃 的"

    空格分隔格式可被 PostgreSQL plainto_tsquery('simple', ...) 直接使用。
    """
    tokens = jieba.cut(text, cut_all=False)
    return " ".join(t for t in tokens if t.strip())


# ── Dense 检索（pgvector） ────────────────────────────────────────────────────

async def dense_search(
    query_vector: list[float],
    city: str,
    top_k: int = 20,
) -> list[dict]:
    """
    pgvector 余弦相似度检索

    Returns:
        按相似度降序的文档列表，每条含 score（余弦相似度 0~1）
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT content, place_ids, note_id, chunk_idx,
                   1 - (embedding <=> $1::vector) AS score
            FROM travel_notes_chunks
            WHERE city = $2
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            query_vector,
            city,
            top_k,
        )
    return [
        {
            "content": r["content"],
            "place_ids": list(r["place_ids"] or []),
            "note_id": r["note_id"],
            "chunk_idx": r["chunk_idx"],
            "score": float(r["score"]),
            "retrieval_source": "dense",
        }
        for r in rows
    ]


# ── Sparse 检索（PostgreSQL BM25 / tsvector） ─────────────────────────────────

async def sparse_search(
    query: str,
    city: str,
    top_k: int = 20,
) -> list[dict]:
    """
    PostgreSQL tsvector + ts_rank_cd BM25-like 检索

    实现原理：
    - 入库时用 jieba 分词后存入 content_tokens 列
    - content_tsv 是从 content_tokens GENERATED 的 tsvector（GIN 索引）
    - 查询时对 query 做同样的 jieba 分词，再用 plainto_tsquery 匹配
    - ts_rank_cd 计算 BM25-style 分数（考虑词频和文档长度）

    Returns:
        按 BM25 分数降序的文档列表，score 为 ts_rank_cd 得分（0~1）
        如果 content_tsv 列不存在（旧数据库），返回空列表
    """
    tokenized = tokenize_chinese(query)
    if not tokenized.strip():
        return []

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                SELECT content, place_ids, note_id, chunk_idx,
                       ts_rank_cd(content_tsv, plainto_tsquery('simple', $1)) AS score
                FROM travel_notes_chunks
                WHERE city = $2
                  AND content_tsv @@ plainto_tsquery('simple', $1)
                ORDER BY score DESC
                LIMIT $3
                """,
                tokenized,
                city,
                top_k,
            )
        except Exception as exc:
            # content_tsv 列不存在时（旧库未迁移）优雅降级
            print(f"[Retriever] BM25 稀疏检索跳过（请运行 migrations/001_add_bm25.sql）：{exc}")
            return []

    return [
        {
            "content": r["content"],
            "place_ids": list(r["place_ids"] or []),
            "note_id": r["note_id"],
            "chunk_idx": r["chunk_idx"],
            "score": float(r["score"]),
            "retrieval_source": "sparse",
        }
        for r in rows
    ]


# ── RRF 融合 ─────────────────────────────────────────────────────────────────

def rrf_fusion(
    dense_results: list[dict],
    sparse_results: list[dict],
    top_k: int = 10,
) -> list[dict]:
    """
    RRF（Reciprocal Rank Fusion）融合两路检索结果

    文档唯一键为 (note_id, chunk_idx)，相同文档在两路中的 RRF 分累加。
    最终按 rrf_score 降序返回 top_k 条。

    Args:
        dense_results  : Dense 路结果（按相似度降序）
        sparse_results : Sparse 路结果（按 BM25 分降序）
        top_k          : 返回结果数量

    Returns:
        融合后的文档列表，新增 rrf_score 字段和 retrieval_sources 字段
    """
    scores: dict[tuple, float] = {}
    meta: dict[tuple, dict] = {}
    sources: dict[tuple, set] = {}

    def _add(results: list[dict]) -> None:
        for rank, doc in enumerate(results, start=1):
            key = (doc["note_id"], doc["chunk_idx"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            sources.setdefault(key, set()).add(doc["retrieval_source"])
            if key not in meta:
                meta[key] = doc

    _add(dense_results)
    _add(sparse_results)

    sorted_keys = sorted(scores, key=lambda k: scores[k], reverse=True)

    results: list[dict] = []
    for key in sorted_keys[:top_k]:
        doc = meta[key].copy()
        doc["rrf_score"] = round(scores[key], 6)
        doc["retrieval_sources"] = sorted(sources[key])  # e.g. ["dense", "sparse"]
        results.append(doc)

    # 统计双路命中情况（面试时可展示）
    both_hit = sum(1 for k in sorted_keys[:top_k] if len(sources[k]) == 2)
    print(
        f"[Retriever] RRF 融合完成：dense={len(dense_results)}, "
        f"sparse={len(sparse_results)}, fused={len(results)}, "
        f"双路命中={both_hit}"
    )

    return results


# ── 并行双路检索入口 ──────────────────────────────────────────────────────────

async def hybrid_search(
    query_vector: list[float],
    query_text: str,
    city: str,
    dense_top_k: int = 20,
    sparse_top_k: int = 20,
    rrf_top_k: int = 10,
) -> list[dict]:
    """
    并行执行 dense + sparse 检索，再 RRF 融合

    Args:
        query_vector : 查询向量（HyDE 或原始查询的 embedding）
        query_text   : 原始查询文本（供 BM25 分词使用）
        city         : 城市过滤
        dense_top_k  : Dense 候选数
        sparse_top_k : Sparse 候选数
        rrf_top_k    : RRF 融合后保留数

    Returns:
        RRF 融合后的文档列表
    """
    dense_res, sparse_res = await asyncio.gather(
        dense_search(query_vector, city, top_k=dense_top_k),
        sparse_search(query_text, city, top_k=sparse_top_k),
    )
    return rrf_fusion(dense_res, sparse_res, top_k=rrf_top_k)
