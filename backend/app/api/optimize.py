"""POST /api/optimize — 路线优化接口（Phase A v2）

PlannerGraph v2 拓扑：
  clusterer → distance → sequencer → weather_fetcher → scheduler_v2 → critic_v2 → tips → END

响应新增：
  - backup_pool       备选池（因排不下而移出行程的地点）
  - critic_violations Critic 硬规则违规摘要
"""

import time

from fastapi import APIRouter, HTTPException

from app.agents.planner import run_planner
from app.memory.working import format_for_prompt
from app.schemas.api import OptimizeRequest, OptimizeResponse

router = APIRouter()


@router.post("/optimize", response_model=OptimizeResponse)
async def optimize(request: OptimizeRequest):
    if not request.places:
        raise HTTPException(status_code=400, detail="places 不能为空")

    start = time.time()

    preferences_text = (
        format_for_prompt(request.working_context) if request.working_context else ""
    )

    result = await run_planner(
        places=request.places,
        trip_days=request.trip_days,
        thread_id=request.thread_id,
        start_date=request.start_date,
        preferences_text=preferences_text,
    )

    itinerary = result.itinerary
    backup_pool = result.backup_pool
    violations = result.critic_violations

    total_distance = sum(
        slot.transport.distance_km
        for day in itinerary.days
        for slot in day.slots
        if slot.transport
    )

    duration_ms = int((time.time() - start) * 1000)

    return OptimizeResponse(
        itinerary=itinerary,
        total_distance_km=round(total_distance, 1),
        optimization_method="planner_v2",
        duration_ms=duration_ms,
        backup_pool=backup_pool,
        critic_violations=violations,
    )
