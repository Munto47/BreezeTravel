"""TipsAgent：将 day_plans 装配为 Itinerary，并调用 TipsGenerator 注入贴心提示"""

import uuid
from datetime import datetime, timezone

from app.agents.nodes.tips_generator import generate_tips
from app.agents.planner.state import PlannerState
from app.schemas.itinerary import Itinerary


async def run(state: PlannerState) -> dict:
    day_plans = state["day_plans"]
    activities = state.get("activities", [])
    thread_id = state["thread_id"]
    preferences = state.get("preferences_text", "")

    city = activities[0].city if activities else "未知"

    itinerary = Itinerary(
        itinerary_id=str(uuid.uuid4()),
        thread_id=thread_id,
        city=city,
        days=day_plans,
        generated_at=datetime.now(timezone.utc).isoformat(),
        version=1,
    )

    try:
        itinerary = await generate_tips(itinerary, preferences=preferences)
    except Exception as e:
        # Tips 失败不阻塞主流程
        print(f"[TipsAgent] 注入提示失败（继续返回原始行程）：{e}")

    trace = state.get("trace", []) + [
        f"[TipsAgent] Itinerary 装配完成，days={len(day_plans)}"
    ]
    return {"itinerary": itinerary, "trace": trace}
