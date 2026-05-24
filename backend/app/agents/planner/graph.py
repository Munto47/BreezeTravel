"""PlannerGraph：多 Agent 路线规划子图

拓扑（线性 A2A 流水线，每个节点专职单一职责）：

    clusterer ──→ distance ──→ sequencer ──→ scheduler ──→ tips ──→ END

每个节点只读写 PlannerState 中自己关心的字段，节点间不直接调用，
通过共享 State 解耦——这是 LangGraph 多 Agent 编排的核心模式。

入口函数 `run_planner` 给出与原 `optimizer.run` 一致的签名，
便于 /api/optimize 无缝迁移。
"""

from typing import Optional

from langgraph.graph import StateGraph, END

from app.agents.planner.nodes import (
    clusterer,
    distance,
    scheduler,
    sequencer,
    tips_agent,
)
from app.agents.planner.state import PlannerState
from app.schemas.itinerary import Itinerary
from app.schemas.place import Place


def build_planner_graph():
    g = StateGraph(PlannerState)

    g.add_node("clusterer", clusterer.run)
    g.add_node("distance", distance.run)
    g.add_node("sequencer", sequencer.run)
    g.add_node("scheduler", scheduler.run)
    g.add_node("tips", tips_agent.run)

    g.set_entry_point("clusterer")
    g.add_edge("clusterer", "distance")
    g.add_edge("distance", "sequencer")
    g.add_edge("sequencer", "scheduler")
    g.add_edge("scheduler", "tips")
    g.add_edge("tips", END)

    return g.compile()


# 子图单例（无 checkpointer，每次调用独立）
_planner_graph = build_planner_graph()


async def run_planner(
    places: list[Place],
    trip_days: int,
    thread_id: str,
    start_date: Optional[str] = None,
    preferences_text: str = "",
) -> Itinerary:
    """PlannerGraph 入口，与 optimizer.run 兼容的签名"""
    initial: PlannerState = {
        "places": places,
        "trip_days": trip_days,
        "thread_id": thread_id,
        "start_date": start_date,
        "preferences_text": preferences_text,
        "trace": [],
    }

    final_state = await _planner_graph.ainvoke(initial)

    if final_state.get("trace"):
        for line in final_state["trace"]:
            print(line)

    itinerary = final_state.get("itinerary")
    if itinerary is None:
        raise RuntimeError("[PlannerGraph] 未能生成 Itinerary")
    return itinerary
