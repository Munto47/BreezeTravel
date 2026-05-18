"""
Cross-Encoder 重排序模块

模型：BAAI/bge-reranker-v2-m3
------------------------------
- 支持中英双语，中文表现优秀
- Cross-Encoder 架构：将 (query, document) 拼接后一次性打分
  （区别于 Bi-Encoder embedding 的近似相似度，Cross-Encoder 更精准）
- 本地 GPU 推理，RTX 4060 Laptop 约 550MB 显存，可流畅运行
- 首次使用时自动从 HuggingFace 下载（约 550MB）

为何 Re-ranking 重要
--------------------
RRF 融合后的 top-10 候选中，仍可能有相关度低的文档排在前面。
Cross-Encoder 对每对 (query, doc) 重新计算精细匹配分，
相比 embedding 相似度，精度提升 10-20%（BEIR benchmark）。

降级策略
--------
如果 FlagEmbedding 或 torch 未安装（如轻量部署/CI），
模块会打印警告并静默跳过重排序，直接返回 RRF 结果截断版。
主流程不会因此中断。

安装
----
  pip install FlagEmbedding          # 自动安装 torch 等依赖
  # 或仅 CPU 推理（慢，不推荐）：
  pip install FlagEmbedding torch    # pip install torch --index-url cpu-only-url
"""

from __future__ import annotations

# 懒加载：避免 import 时因 torch 未安装而崩溃
_reranker = None          # FlagReranker 实例
_init_attempted = False   # 防止重复初始化


def _try_init(model_name: str, device: str) -> None:
    """懒加载 reranker，首次调用 rerank() 时触发"""
    global _reranker, _init_attempted
    if _init_attempted:
        return
    _init_attempted = True

    try:
        from FlagEmbedding import FlagReranker  # type: ignore[import]

        use_fp16 = device.startswith("cuda")
        _reranker = FlagReranker(model_name, use_fp16=use_fp16)
        print(f"[Reranker] {model_name} 加载成功（device={device}, fp16={use_fp16}）")

    except ImportError:
        print(
            "[Reranker] FlagEmbedding 未安装，重排序已禁用。\n"
            "  若需开启：pip install FlagEmbedding"
        )
    except Exception as exc:
        print(f"[Reranker] 模型加载失败，重排序已禁用：{exc}")


def rerank(
    query: str,
    candidates: list[dict],
    top_k: int = 5,
    model_name: str = "BAAI/bge-reranker-v2-m3",
    device: str = "cuda",
) -> list[dict]:
    """
    对 RRF 融合后的候选文档做 Cross-Encoder 重排序

    Args:
        query      : 用户原始查询（或改写查询）
        candidates : RRF 融合后的候选列表（必须含 'content' 字段）
        top_k      : 返回 top-K 结果
        model_name : reranker 模型（HuggingFace Hub ID）
        device     : "cuda" | "cpu"

    Returns:
        重排后的 top-K 文档列表，新增 rerank_score 字段（0~1，normalize=True）
        若 reranker 不可用，返回原始列表截断到 top_k
    """
    if not candidates:
        return candidates

    _try_init(model_name, device)

    if _reranker is None:
        # 降级：直接截断 RRF 结果
        return candidates[:top_k]

    try:
        pairs = [[query, doc["content"]] for doc in candidates]
        scores: list[float] = _reranker.compute_score(pairs, normalize=True)

        scored = [
            {**doc, "rerank_score": float(score)}
            for doc, score in zip(candidates, scores)
        ]
        scored.sort(key=lambda x: x["rerank_score"], reverse=True)

        print(
            f"[Reranker] 重排序完成：{len(candidates)} → top-{top_k}，"
            f"top1 score={scored[0]['rerank_score']:.4f}"
        )
        return scored[:top_k]

    except Exception as exc:
        print(f"[Reranker] 重排序执行失败，返回 RRF 截断结果：{exc}")
        return candidates[:top_k]
