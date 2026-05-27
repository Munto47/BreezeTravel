"""PlannerGraph：多 Agent 路线规划子图（v2）

拓扑（SPEC Phase A）：

    clusterer → distance → sequencer → scheduler_v2 → critic_v2 → tips → END

v2 相对 v1 的变化：
  - scheduler 替换为 scheduler_v2（鱼骨模板 + 营业时间 + 用餐窗 + 体力曲线）
  - 新增 critic_v2 节点（7 条硬规则检验，违规打印告警）
  - PlannerState 新增 day_states / backup_pool / critic_violations 字段
"""

from typing import Optional

from langgraph.graph import StateGraph, END

from app.agents.planner.nodes import (
    clusterer,
    distance,
    scheduler_v2,
    weather_fetcher,
    critic_v2,
    sequencer,
    tips_agent,
)
from app.agents.planner.state import PlannerState
from app.schemas.itinerary import Itinerary
from app.schemas.place import Place
from app.schemas.preferences import GroupPreferences

# 完整 planner 输出（itinerary + 备选池 + 违规报告）
from typing import NamedTuple

class PlannerResult(NamedTuple):
    itinerary: Itinerary
    backup_pool: list[Place]
    critic_violations: list[dict]


def build_planner_graph():
    g = StateGraph(PlannerState)

    g.add_node("clusterer",       clusterer.run)
    g.add_node("distance",        distance.run)
    g.add_node("sequencer",       sequencer.run)
    g.add_node("weather_fetcher", weather_fetcher.run)
    g.add_node("scheduler_v2",    scheduler_v2.run)
    g.add_node("critic_v2",       critic_v2.run)
    g.add_node("tips",            tips_agent.run)

    g.set_entry_point("clusterer")
    g.add_edge("clusterer",       "distance")
    g.add_edge("distance",        "sequencer")
    g.add_edge("sequencer",       "weather_fetcher")
    g.add_edge("weather_fetcher", "scheduler_v2")
    g.add_edge("scheduler_v2",    "critic_v2")
    g.add_edge("critic_v2",       "tips")
    g.add_edge("tips",            END)

    return g.compile()


_planner_graph = build_planner_graph()


async def run_planner(
    places: list[Place],
    trip_days: int,
    thread_id: str,
    start_date: Optional[str] = None,
    preferences_text: str = "",
    user_prefs: Optional[GroupPreferences] = None,
    vote_counts: Optional[dict[str, int]] = None,
) -> PlannerResult:
    """PlannerGraph v2 入口。返回 PlannerResult(itinerary, backup_pool, critic_violations)。"""
    initial: PlannerState = {
        "places": places,
        "trip_days": trip_days,
        "thread_id": thread_id,
        "start_date": start_date,
        "preferences_text": preferences_text,
        "user_prefs": user_prefs,
        "weather_forecast": {},
        "vote_counts": vote_counts or {},
        "backup_pool": [],
        "critic_violations": [],
        "trace": [],
    }

    final_state = await _planner_graph.ainvoke(initial)

    if final_state.get("trace"):
        for line in final_state["trace"]:
            print(line)

    violations = final_state.get("critic_violations", [])
    if violations:
        print(f"[PlannerGraph] Critic 发现 {len(violations)} 条规则违规")

    itinerary = final_state.get("itinerary")
    if itinerary is None:
        raise RuntimeError("[PlannerGraph] 未能生成 Itinerary")

    return PlannerResult(
        itinerary=itinerary,
        backup_pool=final_state.get("backup_pool", []),
        critic_violations=violations,
    )
