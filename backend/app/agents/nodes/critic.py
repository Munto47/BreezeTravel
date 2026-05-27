"""Critic 反思节点（v2，Phase B 升级）

Rule 1: 结果数量不足（< MIN_PLACES）→ 扩大搜索
Rule 2: 品类漂移（用户要美食但全是景点）→ 补充搜索
Rule 3: chunk_id 验证（Phase B）— source_chunk_ids 不在本次 rag_chunks 中 → 剥离 reason
Rule 4: Alternative 合法性（place_id 非空）→ 移除无效 alternative

设计原则：规则驱动，不调 LLM，< 10ms
"""

from app.agents.state import AgentState
from app.schemas.place import PlaceCategory
from app.schemas.recommendation import PlaceRecommendation

MIN_PLACES = 3
MAX_CRITIC_RETRIES = 1


# ─── Rule 3：chunk_id 验证（可独立测试） ──────────────────────────────────────

def _validate_chunk_ids(
    recs: list[PlaceRecommendation],
    valid_chunk_ids: set[str],
) -> list[PlaceRecommendation]:
    """验证每条推荐的 source_chunk_ids 是否在本次 RAG context 中。

    - 过滤掉无效 chunk_id
    - 过滤后 source_chunk_ids 为空 → reason 和 avoid_tips 置为空字符串
    - 保留有至少一个有效 chunk_id 的 reason
    """
    result: list[PlaceRecommendation] = []
    for rec in recs:
        valid_ids = [cid for cid in rec.source_chunk_ids if cid in valid_chunk_ids]
        if valid_ids:
            # 有有效 chunk，保留 reason 和 avoid_tips，只清理无效 id
            result.append(rec.model_copy(update={"source_chunk_ids": valid_ids}))
        else:
            # 无有效 chunk → 剥离 reason / avoid_tips / source_chunk_ids
            result.append(rec.model_copy(update={
                "reason": "",
                "avoid_tips": [],
                "source_chunk_ids": [],
            }))
    return result


def _validate_alternatives(recs: list[PlaceRecommendation]) -> list[PlaceRecommendation]:
    """Rule 4：移除 place_id 为空的 alternative"""
    result: list[PlaceRecommendation] = []
    for rec in recs:
        valid_alts = [a for a in rec.alternatives if a.place_id.strip()]
        result.append(rec.model_copy(update={"alternatives": valid_alts}))
    return result


# ─── 主入口 ───────────────────────────────────────────────────────────────────

async def run(state: AgentState) -> dict:
    places = state.get("synthesized_places", [])
    rag_chunks: list[dict] = state.get("rag_chunks", [])
    recs: list[PlaceRecommendation] = state.get("recommendations", [])
    working_ctx = state.get("working_context") or {}
    preferred_cats = working_ctx.get("preferred_categories", [])
    iterations = state.get("critic_iterations", 0)

    # ── 已达重试上限，强制通过 ────────────────────────────────────────────────
    if iterations >= MAX_CRITIC_RETRIES:
        # 仍执行 chunk 验证（不触发重试）
        recs = _run_quality_rules(recs, rag_chunks)
        return {
            "critic_retry": False,
            "critic_reason": "已达重试上限，直接输出",
            "recommendations": recs,
        }

    # ── Rule 1：结果数量不足 ───────────────────────────────────────────────────
    if len(places) < MIN_PLACES:
        return _make_retry(iterations, f"结果仅 {len(places)} 个（期望 ≥ {MIN_PLACES}），扩大搜索范围")

    # ── Rule 2：品类漂移 ───────────────────────────────────────────────────────
    if "美食" in preferred_cats or "餐饮" in preferred_cats:
        food_count = sum(1 for p in places if getattr(p, "category", None) == PlaceCategory.FOOD)
        if food_count == 0:
            return _make_retry(iterations, "用户偏好美食但结果中无餐饮地点，补充搜索餐厅")

    # ── Rule 3 + Rule 4：chunk 验证 + alternative 清洗（不触发重试） ──────────
    recs = _run_quality_rules(recs, rag_chunks)

    return {
        "critic_retry": False,
        "critic_reason": "质量检查通过",
        "recommendations": recs,
    }


def _run_quality_rules(
    recs: list[PlaceRecommendation],
    rag_chunks: list[dict],
) -> list[PlaceRecommendation]:
    """执行 Rule 3（chunk验证）+ Rule 4（alternative清洗）"""
    valid_chunk_ids = {c["chunk_id"] for c in rag_chunks if "chunk_id" in c}
    recs = _validate_chunk_ids(recs, valid_chunk_ids)
    recs = _validate_alternatives(recs)
    return recs


def _make_retry(iterations: int, reason: str) -> dict:
    # 注入引导消息：告知 Router 上次失败原因，强制下一轮调用 search_places
    from langchain_core.messages import SystemMessage
    hint = SystemMessage(
        content=f"[系统反思] 上一轮未返回地点数据（原因：{reason}）。"
                "本轮必须调用 search_places 工具以获取结构化 POI 数据，否则用户仍将看到空列表。"
    )
    return {
        "critic_retry": True,
        "critic_reason": reason,
        "critic_iterations": iterations + 1,
        "react_iterations": 0,
        "amap_places": [],
        "rag_chunks": [],
        "synthesized_places": [],
        "final_response": None,
        "recommendations": [],
        "messages": [hint],  # add_messages 注解会追加而非覆盖
    }
