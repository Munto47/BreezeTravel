"""
LangGraph 主图定义（Sprint 5 升级版）

完整图拓扑
----------
  router ──→ tool_executor ──┐
    ↑                        │  （ReAct 循环，最多 MAX_REACT_ITERATIONS 次）
    └────────────────────────┘
    │ （无 tool_calls 或达到上限）
    ↓
  synthesizer
    ↓
  critic（质量反思）
    ├── critic_retry=True  → router（重新搜索，最多 MAX_CRITIC_RETRIES 次）
    └── critic_retry=False → END

路由函数
--------
_route_after_react  : router 之后的路由
  - tool_calls 存在  → tool_executor
  - 其他             → synthesizer

_route_after_critic : critic 之后的路由
  - critic_retry     → router（重试）
  - 其他             → END

节点说明
--------
- router        : ReAct Agent，LLM native tool calling，选工具或结束
- tool_executor : 并发执行工具调用，累积 amap_places + rag_chunks
- synthesizer   : DeepSeek 合并数据，生成推荐文本 + Place 列表
- critic        : 规则驱动质量检查，低质结果触发重检索（最多 1 次）

旧版兼容节点（保留供独立测试，不注册为图节点）
- amap_search / rag_retrieval
"""

from langgraph.graph import StateGraph, END
from langchain_core.messages import AIMessage

from app.agents.state import AgentState
from app.agents.nodes import router, tool_executor, synthesizer, critic
# amap_search 和 rag_retrieval 由 tool_executor 内部直接调用，不注册为图节点
from app.config import settings

MAX_REACT_ITERATIONS = 3  # 与 router.py 保持一致


def _route_after_react(state: AgentState) -> str:
    """
    ReAct 路由函数：决定 router 节点之后走哪条边

    条件：
    1. 达到最大迭代次数 → synthesizer（强制结束循环）
    2. 最后一条 AI 消息有 tool_calls → tool_executor（继续 ReAct）
    3. 否则 → synthesizer（LLM 无需更多工具，直接合成）
    """
    iterations = state.get("react_iterations", 0)
    if iterations > MAX_REACT_ITERATIONS:
        return "synthesizer"

    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tool_executor"

    return "synthesizer"


def _route_after_critic(state: AgentState) -> str:
    """
    Critic 路由函数：决定 critic 节点之后走哪条边

    条件：
    - critic_retry=True  → router（重新发起检索）
    - critic_retry=False → END（质量通过或已达重试上限）
    """
    if state.get("critic_retry"):
        return "router"
    return END


def build_graph(checkpointer=None):
    """构建并编译 LangGraph ReAct + Critic 图"""
    g = StateGraph(AgentState)

    # ── 注册节点 ──────────────────────────────────────────────────────
    g.add_node("router", router.run)                  # ReAct Agent
    g.add_node("tool_executor", tool_executor.run)    # 工具执行器
    g.add_node("synthesizer", synthesizer.run)        # 合成节点
    g.add_node("critic", critic.run)                  # Critic 反思节点

    # ── 入口 ─────────────────────────────────────────────────────────
    g.set_entry_point("router")

    # ── ReAct 主路由：router → tool_executor 或 synthesizer ──────────
    g.add_conditional_edges(
        "router",
        _route_after_react,
        {
            "tool_executor": "tool_executor",
            "synthesizer": "synthesizer",
        },
    )

    # ── ReAct 循环：tool_executor → router（Observe 后再次 Think）────
    g.add_edge("tool_executor", "router")

    # ── synthesizer 完成后进入 Critic 反思 ───────────────────────────
    g.add_edge("synthesizer", "critic")

    # ── Critic 路由：质量通过 → END，不足 → router 重试 ──────────────
    g.add_conditional_edges(
        "critic",
        _route_after_critic,
        {
            "router": "router",
            END: END,
        },
    )

    return g.compile(checkpointer=checkpointer)


# 无持久化的简单图（测试/fallback 用）
simple_graph = build_graph()

# ===== 持久化图单例 =====
_cm = None
_checkpointer = None
_persistent_graph = None


async def init_persistent_graph():
    """
    在 FastAPI lifespan startup 中调用，初始化带 PostgreSQL Checkpointer 的图。
    setup() 会自动建 langgraph_checkpoints 等表（幂等）。
    """
    global _cm, _checkpointer, _persistent_graph

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        _cm = AsyncPostgresSaver.from_conn_string(dsn)
        _checkpointer = await _cm.__aenter__()
        await _checkpointer.setup()
        _persistent_graph = build_graph(_checkpointer)
        print("[Graph] PostgreSQL Checkpointer 初始化成功，会话历史将持久化")
    except Exception as exc:
        print(f"[Graph] Checkpointer 初始化失败，回退到无持久化模式：{exc}")
        _persistent_graph = simple_graph


async def close_checkpointer():
    """在 FastAPI lifespan shutdown 中调用"""
    global _cm, _checkpointer
    if _cm:
        try:
            await _cm.__aexit__(None, None, None)
        except Exception:
            pass
        _cm = None
        _checkpointer = None


async def get_graph_with_persistence():
    """获取持久化图（startup 后可用）"""
    return _persistent_graph if _persistent_graph is not None else simple_graph
