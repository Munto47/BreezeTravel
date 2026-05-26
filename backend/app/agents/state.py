from typing import TypedDict, Annotated, Optional
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from app.schemas.place import Place
from app.schemas.itinerary import Itinerary
from app.schemas.recommendation import PlaceRecommendation


class WorkingContext(TypedDict, total=False):
    """
    会话内工作记忆（Working Memory）

    在单次对话中积累的结构化用户偏好，随对话进展自动更新。
    由 tool_executor 节点在每次工具返回后提取并写入。

    total=False 表示所有字段可选，避免初始化时必须填写所有字段。
    """
    # ── 原有字段 ──────────────────────────────────────────────────────
    preferred_categories: list[str]   # 偏好品类，如 ["美食", "文化景点", "自然"]
    excluded_keywords: list[str]      # 排除关键词，如 ["商业区", "太贵", "人多"]
    budget_level: Optional[str]       # 预算档次："高" | "中" | "低"
    travel_style: Optional[str]       # 旅行风格："亲子" | "情侣" | "独行" | "闺蜜" | "商务"
    party_size: Optional[int]         # 出行人数
    special_needs: list[str]          # 特殊需求，如 ["无障碍", "儿童友好", "宠物友好"]
    confirmed_place_ids: list[str]    # 用户已表示感兴趣的地点 IDs
    # ── 深层偏好（R1 新增）───────────────────────────────────────────
    dietary: Optional[str]            # 饮食限制："素食" | "清真" | "无辣" | "海鲜过敏" | "纯素"
    nationality: Optional[str]        # 国籍/文化背景："韩国" | "日本" | "穆斯林" | "西方"
    cuisine_pref: list[str]           # 偏好菜系，如 ["韩国料理", "火锅", "本地特色"]
    pace: Optional[str]               # 旅行节奏："慢节奏" | "打卡党" | "深度游" | "高效游"
    physical_level: Optional[str]     # 体力水平："老人小孩" | "一般" | "户外达人"
    prefer_chain: bool                # 是否偏好连锁/品牌餐厅（稳定保障）
    prefer_trending: bool             # 是否偏好网红/热门当下流行地点
    avoid: list[str]                  # 明确回避的场所类型（与 excluded_keywords 语义对齐）


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
        dietary=None,
        nationality=None,
        cuisine_pref=[],
        pace=None,
        physical_level=None,
        prefer_chain=False,
        prefer_trending=False,
        avoid=[],
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
    recommendations: list[PlaceRecommendation]  # Phase B：结构化推荐（reason/alternatives）

    # ── Optimizer 输出（独立 /api/optimize 触发） ─────────────────────────
    itinerary: Optional[Itinerary]

    # ── 前端上下文 ────────────────────────────────────────────────────────
    selected_place_ids: list[str]      # 已选地点 ID（影响推荐质量）

    # ── Critic 反思节点（Sprint 5 新增） ──────────────────────────────────
    critic_retry: bool                 # Critic 是否触发重检索
    critic_reason: Optional[str]       # 重试原因（供 SSE thinking 事件展示）
    critic_iterations: int             # Critic 重试次数（上限 MAX_CRITIC_RETRIES=1）
