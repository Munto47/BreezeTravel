"""
POST /api/optimize - 路线优化接口

接收已选地点列表，执行 K-Means 聚类 + TSP 排线，返回完整行程。
"""

import time

from fastapi import APIRouter

from app.agents.planner import run_planner
from app.memory.working import format_for_prompt
from app.schemas.api import OptimizeRequest, OptimizeResponse

router = APIRouter()


@router.post("/optimize", response_model=OptimizeResponse)
async def optimize(request: OptimizeRequest):
    """
    智能排线接口（Phase 4：PlannerAgent 多智能体子图）。

    PlannerGraph 拓扑：
      clusterer → distance → sequencer → scheduler → tips → END
    每个子 Agent 通过共享 PlannerState 协作（A2A），互不直接调用。
    """
    start = time.time()

    if not request.places:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="places 不能为空")

    preferences_text = (
        format_for_prompt(request.working_context) if request.working_context else ""
    )

    itinerary = await run_planner(
        places=request.places,
        trip_days=request.trip_days,
        thread_id=request.thread_id,
        start_date=request.start_date,
        preferences_text=preferences_text,
    )

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
        optimization_method="kmeans_tsp",
        duration_ms=duration_ms,
    )
