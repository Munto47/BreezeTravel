"""POST /api/edit — 行程增量编辑接口（SPEC §4 / C3-C4）

双路径：
  1. Rule Fast Path（remove_place / swap_days）→ 直接应用，无 LLM 调用
  2. EditorAgent（replace_place / add_place / rebuild_day）→ LLM 解析意图 → 应用 patch

请求体：
  {
    "thread_id": "...",
    "user_msg": "换掉第二天的武侯祠",
    "itinerary": { ...Itinerary JSON... },
    "patch": null  // 可选：直接传 ItineraryPatch 走 fast path
  }

响应体：
  {
    "itinerary": { ...更新后的行程... },
    "patch": { ...实际应用的 patch... },
    "violations": [...],
    "path_used": "fast_path" | "editor_agent" | "direct_patch"
  }
"""

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.schemas.itinerary import Itinerary
from app.schemas.patch import ItineraryPatch
from app.config import get_settings
from app.services.room_access import require_room_member
from app.utils.auth import get_optional_user

router = APIRouter()

_FAST_PATH_OPS = {"remove_place", "swap_days"}


class EditRequest(BaseModel):
    thread_id: str
    room_id: Optional[str] = None
    user_msg: Optional[str] = None         # 自然语言编辑意图（EditorAgent 路径）
    itinerary: Itinerary                   # 当前行程
    patch: Optional[ItineraryPatch] = None # 直接传 patch（绕过 LLM 解析）


class EditResponse(BaseModel):
    itinerary: Itinerary
    patch: Optional[ItineraryPatch]
    violations: list[dict] = []
    path_used: str
    duration_ms: int


@router.post("/edit", response_model=EditResponse)
async def edit_itinerary(request: EditRequest, current_user: str | None = Depends(get_optional_user)):
    if request.room_id:
        if current_user is None:
            raise HTTPException(status_code=401, detail="请先登录")
        await require_room_member(request.room_id, current_user, thread_id=request.thread_id)
    elif not get_settings().demo_mode:
        raise HTTPException(status_code=400, detail="room_id 必填")
    start = time.time()

    patch: Optional[ItineraryPatch] = request.patch
    path_used = "direct_patch"

    # ── 路径判断 ──────────────────────────────────────────────────────────────
    if patch is None:
        if not request.user_msg:
            raise HTTPException(status_code=400, detail="user_msg 和 patch 至少提供一个")

        # 先尝试用规则判断是否是简单意图
        patch = _try_rule_fast_path(request.user_msg, request.itinerary)
        if patch:
            path_used = "fast_path"
        else:
            # 走 EditorAgent（LLM）
            from app.agents.editor.editor_agent import parse_edit_intent
            patch = await parse_edit_intent(request.user_msg, request.itinerary)
            if patch is None:
                raise HTTPException(
                    status_code=422,
                    detail="无法解析编辑意图，请换一种说法再试。",
                )
            path_used = "editor_agent"

    # ── 应用 Patch ────────────────────────────────────────────────────────────
    from app.agents.editor.fast_path import fast_apply

    if patch.op in _FAST_PATH_OPS:
        new_itinerary, violations = fast_apply(patch, request.itinerary)
    else:
        # replace_place / add_place / rebuild_day → 目前降级到 fast_path 骨架
        # 完整实现需要 Planner 局部重跑（SPEC §4.2 step 4）
        new_itinerary, violations = fast_apply(patch, request.itinerary)
        if not violations:
            path_used += "+partial_replan"

    # PATCH_ERROR 类违规视为请求错误
    patch_errors = [v for v in violations if v.get("rule") == "PATCH_ERROR"]
    if patch_errors:
        raise HTTPException(status_code=400, detail=patch_errors[0]["message"])

    duration_ms = int((time.time() - start) * 1000)

    return EditResponse(
        itinerary=new_itinerary,
        patch=patch,
        violations=[v for v in violations if v.get("rule") != "PATCH_ERROR"],
        path_used=path_used,
        duration_ms=duration_ms,
    )


# ─── 规则快路径意图识别（简单文本匹配） ─────────────────────────────────────

def _try_rule_fast_path(
    user_msg: str,
    itinerary: Itinerary,
) -> Optional[ItineraryPatch]:
    """
    对低复杂度意图做规则匹配，避免 LLM 调用：
    - "删掉 / 去掉 / 移除 <地名>" → remove_place
    - "换掉 / 替换 <地名>" → replace_place（也算 fast path，由 fast_apply 处理）
    - "互换第X天和第Y天" → swap_days
    """
    import re

    # 互换两天
    swap_match = re.search(r'(互换|交换|把)第?(\d+)天.*(第?(\d+)天|和第?(\d+))', user_msg)
    if swap_match:
        try:
            day_a = int(swap_match.group(2)) - 1
            day_b = int(swap_match.group(4) or swap_match.group(5)) - 1
            if day_a >= 0 and day_b >= 0 and day_a != day_b:
                return ItineraryPatch(
                    op="swap_days",
                    day_index=day_a,
                    target_place_id=str(day_b),
                    rationale=f"互换第 {day_a+1} 天和第 {day_b+1} 天",
                )
        except Exception:
            pass

    # 删除地点：从行程中查找地名匹配
    remove_match = re.search(r'(删掉|去掉|移除|取消|不去|删除)\s*(.+?)(?:$|[，。！,!])', user_msg)
    if remove_match:
        keyword = remove_match.group(2).strip()
        # 在行程中查找匹配的 place
        for day in itinerary.days:
            for slot in day.slots:
                place_name = (slot.place or {}).get("name", "")
                if keyword in place_name or place_name in keyword:
                    return ItineraryPatch(
                        op="remove_place",
                        day_index=day.day_index,
                        target_place_id=slot.place_id,
                        rationale=f"删除第 {day.day_index+1} 天的 {place_name}",
                    )

    return None
