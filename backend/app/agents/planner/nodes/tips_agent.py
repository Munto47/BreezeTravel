"""Assemble a draft, then inject tips only after the final verification pass."""

import uuid
from datetime import datetime, timezone

from app.agents.nodes.tips_generator import generate_tips
from app.agents.planner.state import PlannerState
from app.schemas.itinerary import Itinerary


def assemble(state: PlannerState) -> dict:
    day_plans = state["day_plans"]
    activities = state.get("activities", [])
    thread_id = state["thread_id"]

    city = activities[0].city if activities else "未知"

    itinerary = Itinerary(
        itinerary_id=str(uuid.uuid4()),
        thread_id=thread_id,
        city=city,
        days=day_plans,
        generated_at=datetime.now(timezone.utc).isoformat(),
        version=1,
    )

    trace = state.get("trace", []) + [
        f"[Assembler] Itinerary 装配完成，days={len(day_plans)}"
    ]
    return {"itinerary": itinerary, "trace": trace}


async def run(state: PlannerState) -> dict:
    itinerary = state.get("itinerary")
    if itinerary is None:
        assembled = assemble(state)
        itinerary = assembled["itinerary"]
        base_trace = assembled["trace"]
    else:
        base_trace = state.get("trace", [])
    preferences = state.get("preferences_text", "")

    try:
        itinerary = await generate_tips(itinerary, preferences=preferences)
    except Exception as e:
        # Tips 失败不阻塞主流程
        print(f"[TipsAgent] 注入提示失败（继续返回原始行程）：{e}")

    trace = base_trace + [
        f"[TipsAgent] 最终复验后注入提示，revision={itinerary.version}"
    ]
    return {"itinerary": itinerary, "trace": trace}
