"""PlannerState：Planner 子图的共享状态（A2A 数据主干）

子 Agent 分工：
  ClustererAgent  → activities / hotels_pool / clusters / center_lat / center_lng
  DistanceAgent   → time_matrices
  SequencerAgent  → orderings
  SchedulerAgent  → day_plans / day_states（v2 新增，按天切片）
  Assembler       → draft itinerary
  TipsAgent       → final verified itinerary tips

v2 新增字段（SPEC Phase A）：
  weather_forecast  — 按 day_index 的天气预报
  user_prefs        — 群体偏好（GroupPreferences）
  day_states        — DayPlannerState 按天切片（Scheduler v2 写入）
  backup_pool       — 因时间/体力不足被剔除的备选地点（A7）
  critic_violations — Critic 硬规则违规列表（A6）
"""

from typing import TypedDict, Optional

from app.schemas.itinerary import DayPlan, Itinerary
from app.schemas.place import Place
from app.schemas.preferences import GroupPreferences, WeatherDay
from app.schemas.task_spec import TripTaskSpec
from app.schemas.verification import VerificationReport


class Slot(TypedDict, total=False):
    """鱼骨模板槽位（已填入地点后的具体 slot）"""
    slot_index: int
    template_slot_id: str        # 对应模板中的 slot 名，如 "lunch" / "morning_main"
    place_id: Optional[str]
    place: Optional[dict]        # Place.model_dump()
    start_time: str              # "HH:MM"
    end_time: str
    category_l1: str
    category_l2: str
    is_required: bool            # 必须槽（餐厅）vs 可选槽


class DayPlannerState(TypedDict, total=False):
    """单天的规划状态（v2 按天切片）"""
    day_index: int
    template_id: str             # 选用的鱼骨模板 ID
    slots: list[Slot]
    locked: bool                 # EditorAgent 标记后不可重排
    rationale: str               # 该天为什么这样安排（LLM 一句话摘要）
    overflow_places: list[str]   # 本天排不下、转入 backup_pool 的 place_id


class PlannerState(TypedDict, total=False):
    # ── 输入 ─────────────────────────────────────────────────────────
    places: list[Place]
    trip_days: int
    thread_id: str
    start_date: Optional[str]
    preferences_text: str        # working_context 格式化偏好（喂给 TipsAgent）

    # v2 新增输入
    user_prefs: Optional[GroupPreferences]
    weather_forecast: dict[int, WeatherDay]   # day_index → 天气
    vote_counts: dict[str, int]               # D24：place_id → 票数（前端 Yjs 传入）
    task_spec: Optional[TripTaskSpec]
    planning_input_hash: str
    defer_tips: bool                    # persisted flow waits for canonical AuditReport

    # ── ClustererAgent 输出 ─────────────────────────────────────────
    activities: list[Place]
    hotels_pool: list[Place]
    clusters: dict[int, list[Place]]          # cluster_id → 簇内地点

    # ── DistanceAgent 输出 ──────────────────────────────────────────
    time_matrices: dict[int, dict]

    # ── SequencerAgent 输出 ─────────────────────────────────────────
    orderings: dict[int, list[Place]]

    # ── SchedulerAgent 输出 ─────────────────────────────────────────
    day_plans: list[DayPlan]                  # 向后兼容旧 itinerary 结构
    day_states: dict[int, DayPlannerState]    # v2 按天切片（Critic 消费）
    center_lat: float
    center_lng: float

    # ── CriticV2 输出 ───────────────────────────────────────────────
    critic_violations: list[dict]             # [{rule, day_index, place_id, message}]

    # ── 过载备选池（A7） ────────────────────────────────────────────
    backup_pool: list[Place]

    # ── 终态 ─────────────────────────────────────────────────────────
    itinerary: Optional[Itinerary]
    verification_report: Optional[VerificationReport]
    repair_rounds: int
    repair_signatures: list[str]
    unresolved_repair_reasons: list[str]

    # ── 调试 / 可观测 ───────────────────────────────────────────────
    trace: list[str]
