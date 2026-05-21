"""
Critic 反思节点：对 Synthesizer 输出进行质量反思

设计原则
--------
- 规则驱动（不调 LLM），保持低延迟（< 10ms）
- critic_iterations 防止无限重试，上限 MAX_CRITIC_RETRIES=1
- 重试时清空上轮工具数据，让 Router 以新增 hint 重新搜索
- 通过返回 critic_reason 字段，让 SSE thinking 事件把原因展示给用户

反思规则
--------
Rule 1：结果数量不足（< MIN_PLACES 个）→ 扩大搜索
Rule 2：品类漂移（用户明确要"美食"，但返回全是景点）→ 补充搜索
Rule 3：以上均通过 → PASS，进入 END
"""

from app.agents.state import AgentState
from app.schemas.place import PlaceCategory

# 结果数量下限
MIN_PLACES = 3

# Critic 最多触发 1 次重试（避免无限循环）
MAX_CRITIC_RETRIES = 1


async def run(state: AgentState) -> dict:
    """
    Critic 节点入口

    返回字段：
    - critic_retry   : True 时图路由回 Router
    - critic_reason  : 重试原因文本（SSE thinking 展示）
    - critic_iterations: 累计重试次数
    - react_iterations : 重试时重置为 0，让 Router 有机会再次调工具
    - amap_places / rag_chunks / synthesized_places: 重试时清空旧数据
    """
    places = state.get("synthesized_places", [])
    working_ctx = state.get("working_context") or {}
    preferred_cats = working_ctx.get("preferred_categories", [])
    iterations = state.get("critic_iterations", 0)

    # ── 已达重试上限，强制通过 ─────────────────────────────────────────
    if iterations >= MAX_CRITIC_RETRIES:
        return {"critic_retry": False, "critic_reason": "已达重试上限，直接输出"}

    # ── Rule 1：结果数量不足 ───────────────────────────────────────────
    if len(places) < MIN_PLACES:
        reason = f"结果仅 {len(places)} 个（期望 ≥ {MIN_PLACES}），扩大搜索范围"
        return _make_retry(iterations, reason)

    # ── Rule 2：品类漂移检查 ──────────────────────────────────────────
    if "美食" in preferred_cats or "餐饮" in preferred_cats:
        food_count = sum(
            1 for p in places
            if getattr(p, "category", None) == PlaceCategory.FOOD
        )
        if food_count == 0:
            reason = "用户偏好美食但结果中无餐饮地点，补充搜索餐厅"
            return _make_retry(iterations, reason)

    # ── 质量通过 ──────────────────────────────────────────────────────
    return {"critic_retry": False, "critic_reason": "质量检查通过"}


def _make_retry(iterations: int, reason: str) -> dict:
    """构造重试返回值：清空旧数据 + 重置迭代计数"""
    return {
        "critic_retry": True,
        "critic_reason": reason,
        "critic_iterations": iterations + 1,
        # 重置 ReAct 计数，让 Router 下一轮能正常调工具
        "react_iterations": 0,
        # 清空旧检索数据，避免 Synthesizer 重用低质结果
        "amap_places": [],
        "rag_chunks": [],
        "synthesized_places": [],
        "final_response": None,
    }
