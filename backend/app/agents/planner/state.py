"""PlannerState：Planner 子图的共享状态（A2A 数据主干）

每个 Planner 子 Agent 读写此 State 的不同字段，实现解耦协作：

  ClustererAgent  → activities / hotels_pool / clusters
  DistanceAgent   → time_matrices（按 cluster_id 分组）
  SequencerAgent  → orderings（簇内 TSP 顺序）
  SchedulerAgent  → day_plans（时间槽 + 酒店挂载 + 天气）
  TipsAgent       → itinerary（注入贴心提示）
"""

from typing import TypedDict, Optional

from app.schemas.itinerary import DayPlan, Itinerary
from app.schemas.place import Place


class PlannerState(TypedDict, total=False):
    # ── 输入 ─────────────────────────────────────────────────────────
    places: list[Place]
    trip_days: int
    thread_id: str
    start_date: Optional[str]
    preferences_text: str  # working_context 格式化后的偏好文本（喂给 TipsAgent）

    # ── ClustererAgent 输出 ─────────────────────────────────────────
    activities: list[Place]
    hotels_pool: list[Place]
    clusters: dict[int, list[Place]]  # cluster_id → 簇内地点

    # ── DistanceAgent 输出 ──────────────────────────────────────────
    # cluster_id → {(place_id_a, place_id_b): (duration_mins, distance_km)}
    time_matrices: dict[int, dict]

    # ── SequencerAgent 输出 ─────────────────────────────────────────
    orderings: dict[int, list[Place]]  # cluster_id → TSP 排好序的地点列表

    # ── SchedulerAgent 输出 ─────────────────────────────────────────
    day_plans: list[DayPlan]
    center_lat: float
    center_lng: float

    # ── 终态 ─────────────────────────────────────────────────────────
    itinerary: Optional[Itinerary]

    # ── 调试 / 可观测 ───────────────────────────────────────────────
    trace: list[str]  # 每个子 Agent 写一行进度，便于 LangSmith / 日志
