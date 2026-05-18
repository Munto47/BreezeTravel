from typing import TypedDict, Annotated, Optional
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from app.schemas.place import Place
from app.schemas.itinerary import Itinerary


class WorkingContext(TypedDict, total=False):
    """
    会话内工作记忆（Working Memory）

    在单次对话中积累的结构化用户偏好，随对话进展自动更新。
    由 tool_executor 节点在每次工具返回后提取并写入。

    total=False 表示所有字段可选，避免初始化时必须填写所有字段。
    """
    preferred_categories: list[str]   # 偏好品类，如 ["美食", "文化景点", "自然"]
    excluded_keywords: list[str]      # 排除关键词，如 ["商业区", "太贵", "人多"]
    budget_level: Optional[str]       # 预算档次："高" | "中" | "低"
    travel_style: Optional[str]       # 旅行风格："亲子" | "情侣" | "独行" | "闺蜜" | "商务"
    party_size: Optional[int]         # 出行人数
    special_needs: list[str]          # 特殊需求，如 ["无障碍", "儿童友好", "宠物友好"]
    confirmed_place_ids: list[str]    # 用户已表示感兴趣的地点 IDs


def default_working_context() -> WorkingContext:
    """返回空的工作记忆初始状态"""
    return WorkingContext(
        preferred_categories=[],
        excluded_keywords=[],
        budget_level=None,
        travel_style=None,
        party_size=None,
        special_needs=[],
        confirmed_place_ids=[],
    )


class AgentState(TypedDict):
    """
    LangGraph 状态机的核心数据主干，所有节点共享读写此 State

    Sprint 2 新增字段：
    - working_context     : 会话内工作记忆（偏好、预算、风格）
    - user_long_term_prefs: 从数据库加载的历史偏好摘要（文本）
    - react_iterations    : ReAct 循环计数（防无限循环，上限 MAX_REACT_ITERATIONS）
    """

    # ── 对话历史（LangGraph 原生消息追加） ──────────────────────────────
    messages: Annotated[list[BaseMessage], add_messages]

    # ── 会话标识 ─────────────────────────────────────────────────────────
    thread_id: str
    user_id: str

    # ── 目的地上下文 ──────────────────────────────────────────────────────
    trip_city: Optional[str]           # 如 "成都"、"北京"

    # ── ReAct Agent 控制 ──────────────────────────────────────────────────
    intent: Optional[str]              # "rag" | "amap" | "both"（向后兼容）
    query_rewrite: Optional[str]       # 改写后的查询（向后兼容）
    react_iterations: int              # ReAct 循环次数（防无限循环）

    # ── Memory 字段（Sprint 2 新增） ──────────────────────────────────────
    working_context: Optional[WorkingContext]   # 会话内偏好追踪
    user_long_term_prefs: Optional[str]         # 历史偏好文本（从 DB 加载）

    # ── 各工具节点输出 ────────────────────────────────────────────────────
    amap_places: list[Place]           # 高德 API 返回的候选地点
    rag_chunks: list[dict]             # RAG 检索返回的 chunk 列表

    # ── Synthesizer 输出 ──────────────────────────────────────────────────
    synthesized_places: list[Place]
    final_response: Optional[str]

    # ── Optimizer 输出（独立 /api/optimize 触发） ─────────────────────────
    itinerary: Optional[Itinerary]

    # ── 前端上下文 ────────────────────────────────────────────────────────
    selected_place_ids: list[str]      # 已选地点 ID（影响推荐质量）
