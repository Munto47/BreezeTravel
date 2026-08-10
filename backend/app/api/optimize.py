"""POST /api/optimize — 路线优化接口（Phase A v2）

PlannerGraph v2 拓扑：
  clusterer → distance → sequencer → weather_fetcher → scheduler_v2 → critic_v2 → tips → END

响应新增：
  - backup_pool       备选池（因排不下而移出行程的地点）
  - critic_violations Critic 硬规则违规摘要
"""

import time

from fastapi import APIRouter, Depends, HTTPException

from app.agents.planner import run_planner
from app.memory.working import format_for_prompt
from app.schemas.api import OptimizeRequest, OptimizeResponse
from app.config import get_settings
from app.services.room_access import require_room_member
from app.utils.auth import get_optional_user

router = APIRouter()


@router.post("/optimize", response_model=OptimizeResponse)
async def optimize(request: OptimizeRequest, current_user: str | None = Depends(get_optional_user)):
    cfg = get_settings()
    if request.room_id:
        if current_user is None:
            raise HTTPException(status_code=401, detail="请先登录")
        await require_room_member(request.room_id, current_user, thread_id=request.thread_id)
    elif not cfg.demo_mode:
        raise HTTPException(status_code=400, detail="room_id 必填")
    if not request.places:
        raise HTTPException(status_code=400, detail="places 不能为空")
    if request.task_spec and request.task_spec.needs_clarification:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TASK_NEEDS_CLARIFICATION",
                "missing_fields": request.task_spec.missing_fields,
                "conflicts": request.task_spec.conflicts,
            },
        )

    start = time.time()

    preferences_text = (
        format_for_prompt(request.working_context) if request.working_context else ""
    )

    # D24：解析 GroupPreferences
    from app.schemas.preferences import GroupPreferences
    user_prefs: GroupPreferences | None = None
    if request.user_prefs:
        try:
            user_prefs = GroupPreferences(**request.user_prefs)
        except Exception as e:
            print(f"[Optimize] user_prefs 解析失败（跳过）：{e}")

    result = await run_planner(
        places=request.places,
        trip_days=request.trip_days,
        thread_id=request.thread_id,
        start_date=request.start_date,
        preferences_text=preferences_text,
        user_prefs=user_prefs,
        vote_counts=request.vote_counts,
        task_spec=request.task_spec,
        planning_input_hash=request.planning_input_hash or "",
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
        task_spec=request.task_spec,
        verification_report=result.verification_report,
        planning_input_hash=result.verification_report.planning_input_hash if result.verification_report else request.planning_input_hash,
    )
