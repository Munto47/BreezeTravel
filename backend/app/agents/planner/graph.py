"""PlannerGraph：多 Agent 路线规划子图（v2）

拓扑（SPEC Phase A）：

    clusterer → distance → sequencer → scheduler_v2
      → assembler → verifier → repair loop → tips or END

v2 相对 v1 的变化：
  - scheduler 替换为 scheduler_v2（鱼骨模板 + 营业时间 + 用餐窗 + 体力曲线）
  - Critic 独有规则迁入统一 RuleRegistry 后，主图不再运行第二套 finding 源
  - 持久化调用可延后 Tips，等 canonical revision + AuditReport 成立后再生成
"""

from typing import Optional

from langgraph.graph import StateGraph, END

from app.agents.planner.nodes import (
    clusterer,
    distance,
    scheduler_v2,
    weather_fetcher,
    sequencer,
    tips_agent,
)
from app.agents.planner.state import PlannerState
from app.schemas.itinerary import Itinerary
from app.schemas.place import Place
from app.schemas.preferences import GroupPreferences
from app.schemas.task_spec import TripTaskSpec
from app.schemas.verification import ConstraintStatus, VerificationReport
from app.constraints.verifier import ItineraryVerifier
from app.agents.planner.repair_controller import TargetedRepairController
from app.constraints.location import filter_human_suitable_places, filter_places_by_district

# 完整 planner 输出（itinerary + 备选池 + 违规报告）
from typing import NamedTuple

class PlannerResult(NamedTuple):
    itinerary: Itinerary
    backup_pool: list[Place]
    critic_violations: list[dict]
    verification_report: Optional[VerificationReport] = None


async def _verify(state: PlannerState) -> dict:
    task_spec = state.get("task_spec")
    itinerary = state.get("itinerary")
    if task_spec is None or itinerary is None:
        return {"verification_report": None, "trace": state.get("trace", []) + ["[Verifier] 无 TaskSpec，兼容模式跳过"]}
    verifier = ItineraryVerifier()
    report = verifier.verify(
        task_spec,
        itinerary,
        places=state.get("places", []),
        planning_input_hash=state.get("planning_input_hash") or None,
        repair_rounds=state.get("repair_rounds", 0),
        unresolved_reasons=state.get("unresolved_repair_reasons", []),
    )
    return {
        "verification_report": report,
        "trace": state.get("trace", []) + [f"[Verifier] {report.overall_status.value} checks={len(report.checks)}"],
    }


def _verification_route(state: PlannerState) -> str:
    report = state.get("verification_report")
    if report is None:
        return "done" if state.get("defer_tips", False) else "tips"
    repairable = [item for item in report.checks if item.status == ConstraintStatus.VIOLATED and item.repairable]
    if not repairable or state.get("repair_rounds", 0) >= TargetedRepairController.max_rounds:
        return "done" if state.get("defer_tips", False) else "tips"
    signature = TargetedRepairController.signature(repairable)
    if signature in state.get("repair_signatures", []):
        return "done" if state.get("defer_tips", False) else "tips"
    return "repair"


async def _repair(state: PlannerState) -> dict:
    report = state.get("verification_report")
    itinerary = state.get("itinerary")
    task_spec = state.get("task_spec")
    if report is None or itinerary is None or task_spec is None:
        return {}
    controller = TargetedRepairController()
    repaired, plan = controller.repair_once(itinerary, task_spec, report.checks, state.get("places", []))
    return {
        "itinerary": repaired,
        "repair_rounds": state.get("repair_rounds", 0) + 1,
        "repair_signatures": state.get("repair_signatures", []) + [plan.violation_signature],
        "unresolved_repair_reasons": state.get("unresolved_repair_reasons", []) + plan.unresolved,
        "trace": state.get("trace", []) + [f"[Repair] actions={len(plan.actions)} unresolved={len(plan.unresolved)}"],
    }


def build_planner_graph():
    g = StateGraph(PlannerState)

    g.add_node("clusterer",       clusterer.run)
    g.add_node("distance",        distance.run)
    g.add_node("sequencer",       sequencer.run)
    g.add_node("weather_fetcher", weather_fetcher.run)
    g.add_node("scheduler_v2",    scheduler_v2.run)
    g.add_node("assembler",       tips_agent.assemble)
    g.add_node("tips",            tips_agent.run)
    g.add_node("verifier",        _verify)
    g.add_node("repair",          _repair)

    g.set_entry_point("clusterer")
    g.add_edge("clusterer",       "distance")
    g.add_edge("distance",        "sequencer")
    g.add_edge("sequencer",       "weather_fetcher")
    g.add_edge("weather_fetcher", "scheduler_v2")
    g.add_edge("scheduler_v2",    "assembler")
    g.add_edge("assembler",       "verifier")
    g.add_conditional_edges(
        "verifier",
        _verification_route,
        {"repair": "repair", "tips": "tips", "done": END},
    )
    g.add_edge("repair",          "verifier")
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
    task_spec: Optional[TripTaskSpec] = None,
    planning_input_hash: str = "",
    defer_tips: bool = False,
) -> PlannerResult:
    """PlannerGraph v2 入口。返回 PlannerResult(itinerary, backup_pool, critic_violations)。"""
    places = filter_human_suitable_places(places)
    if task_spec:
        excluded_terms = ["".join(item.value.lower().split()) for item in task_spec.exclude]
        filtered_places = []
        for place in places:
            searchable = "".join(f"{place.name}{place.address}{place.tags}{place.category.value}".lower().split())
            if not any(term and term in searchable for term in excluded_terms):
                filtered_places.append(place)
        places = filtered_places

        area_constraint = next(
            (item for item in task_spec.hard_constraints if item.type == "trip_area"),
            None,
        )
        if area_constraint:
            before_area = list(places)
            places = filter_places_by_district(places, str(area_constraint.value))
            if not places:
                raise ValueError(f"限定区域 {area_constraint.value} 内没有可排线地点")
            if any(place.category.value == "hotel" for place in before_area) and not any(
                place.category.value == "hotel" for place in places
            ):
                raise ValueError(f"限定区域 {area_constraint.value} 内缺少已选择的酒店")

        # The existing scheduler already prioritises vote_counts. Give explicit
        # must-include candidates a deterministic priority without introducing
        # another probabilistic planner step.
        boosted_votes = dict(vote_counts or {})
        must_terms = ["".join(item.value.lower().split()) for item in task_spec.must_include]
        for place in places:
            searchable = "".join(f"{place.name}{place.tags}".lower().split())
            if any(term and term in searchable for term in must_terms):
                boosted_votes[place.place_id] = max(boosted_votes.get(place.place_id, 0), 1000)
        vote_counts = boosted_votes

        if user_prefs is None and (
            task_spec.travelers.children > 0
            or task_spec.travelers.seniors > 0
            or any(pref.type in {"family_friendly", "senior_friendly", "low_walking"} for pref in task_spec.soft_preferences)
        ):
            user_prefs = GroupPreferences(
                style="family",
                has_kids=task_spec.travelers.children > 0,
                nice_to_have=["亲子", "室内", "轻松", "长辈推荐"],
                trip_city=task_spec.city,
                trip_days=task_spec.date_range.days,
            )

    initial: PlannerState = {
        "places": places,
        "trip_days": trip_days,
        "thread_id": thread_id,
        "start_date": start_date,
        "preferences_text": preferences_text,
        "user_prefs": user_prefs,
        "weather_forecast": {},
        "vote_counts": vote_counts or {},
        "task_spec": task_spec,
        "planning_input_hash": planning_input_hash,
        "defer_tips": defer_tips,
        "backup_pool": [],
        "critic_violations": [],
        "verification_report": None,
        "repair_rounds": 0,
        "repair_signatures": [],
        "unresolved_repair_reasons": [],
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
        verification_report=final_state.get("verification_report"),
    )
