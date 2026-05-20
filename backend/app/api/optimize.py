"""
POST /api/optimize - 路线优化接口

接收已选地点列表，执行 K-Means 聚类 + TSP 排线，返回完整行程。
"""

import time

from fastapi import APIRouter

from app.agents.nodes.optimizer import run as optimizer_run
from app.agents.nodes.tips_generator import generate_tips
from app.memory.working import format_for_prompt
from app.schemas.api import OptimizeRequest, OptimizeResponse

router = APIRouter()


@router.post("/optimize", response_model=OptimizeResponse)
async def optimize(request: OptimizeRequest):
    """
    智能排线接口。

    算法：K-Means（按经纬度聚类为每日簇）+ 高德驾车距离矩阵 + TSP 最近邻启发式（簇内排序）
    排线完成后调用 TipsGenerator，为每个地点注入温馨提示。
    """
    start = time.time()

    if not request.places:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="places 不能为空")

    itinerary = await optimizer_run(
        places=request.places,
        trip_days=request.trip_days,
        thread_id=request.thread_id,
        start_date=request.start_date,
    )

    # 注入温馨提示（TipsGenerator）
    preferences_text = format_for_prompt(request.working_context) if request.working_context else ""
    itinerary = await generate_tips(itinerary, preferences=preferences_text)

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
