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
from typing import Annotated, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
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
    workspace_id: Optional[str] = None
    base_revision: Optional[int] = None
    command_id: Optional[str] = None
    user_msg: Optional[str] = None         # 自然语言编辑意图（EditorAgent 路径）
    itinerary: Itinerary                   # 当前行程
    patch: Optional[ItineraryPatch] = None # 直接传 patch（绕过 LLM 解析）


class EditResponse(BaseModel):
    itinerary: Itinerary
    patch: Optional[ItineraryPatch]
    violations: list[dict] = []
    path_used: str
    duration_ms: int
    workspace_id: Optional[str] = None
    itinerary_revision: Optional[int] = None
    report_stale: bool = True


@router.post("/edit", response_model=EditResponse)
async def edit_itinerary(
    request: EditRequest,
    current_user: str | None = Depends(get_optional_user),
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    cfg = get_settings()
    if request.room_id:
        if current_user is None:
            raise HTTPException(status_code=401, detail="请先登录")
        await require_room_member(request.room_id, current_user, thread_id=request.thread_id)
    elif not cfg.demo_mode:
        raise HTTPException(status_code=400, detail="room_id 必填")

    if request.workspace_id or not cfg.demo_mode:
        if current_user is None:
            raise HTTPException(status_code=401, detail="请先登录")
        if not request.workspace_id or request.base_revision is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "WORKSPACE_REVISION_REQUIRED", "message": "workspace_id 和 base_revision 必填"},
            )
        if if_match is None or idempotency_key is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "REVISION_HEADERS_REQUIRED", "message": "If-Match 和 Idempotency-Key 必填"},
            )
        return await _edit_authoritative(
            request,
            current_user=current_user,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )
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


async def _edit_authoritative(
    request: EditRequest,
    *,
    current_user: str,
    if_match: str,
    idempotency_key: str,
) -> EditResponse:
    """Legacy adapter: parse old patch shape, but mutate only the server revision."""

    from app.itineraries.adapters import revision_to_legacy
    from app.itineraries.command_service import RevisionCommandService
    from app.itineraries.errors import ItineraryDomainError
    from app.itineraries.models import ItineraryEditCommand
    from app.itineraries.repositories import PostgresItineraryRepository

    started = time.time()
    repository = PostgresItineraryRepository()
    workspace = await repository.get_workspace(request.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": "workspace 不存在"})
    if workspace.room_id != request.room_id:
        raise HTTPException(status_code=403, detail={"code": "RESOURCE_SCOPE_DENIED", "message": "workspace 不属于该 room"})
    server_revision = await repository.get_revision(request.workspace_id, request.base_revision)
    if server_revision is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": "revision 不存在"})

    server_legacy = revision_to_legacy(server_revision, thread_id=request.thread_id)
    patch = request.patch
    path_used = "revision_command_adapter"
    if patch is None:
        if not request.user_msg:
            raise HTTPException(status_code=400, detail="user_msg 和 patch 至少提供一个")
        patch = _try_rule_fast_path(request.user_msg, server_legacy)
        if patch is None:
            from app.agents.editor.editor_agent import parse_edit_intent

            patch = await parse_edit_intent(request.user_msg, server_legacy)
            if patch is None:
                raise HTTPException(status_code=422, detail="无法解析编辑意图，请换一种说法再试。")
            path_used += "+editor_agent"
        else:
            path_used += "+rule_parser"

    operation, payload = _legacy_patch_command(server_revision, patch)
    command = ItineraryEditCommand(
        command_id=request.command_id or str(uuid4()),
        workspace_id=request.workspace_id,
        base_revision=request.base_revision,
        actor_user_id=current_user,
        operation=operation,
        payload=payload,
    )
    try:
        match_revision = int(if_match.strip().removeprefix("W/").strip().strip('"'))
        result = await RevisionCommandService(repository).apply(
            command,
            if_match_revision=match_revision,
            idempotency_key=idempotency_key,
        )
    except (ValueError, ItineraryDomainError) as exc:
        if isinstance(exc, ItineraryDomainError):
            raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
        raise HTTPException(status_code=400, detail={"code": "INVALID_IF_MATCH", "message": "If-Match 无效"}) from exc

    new_revision = await repository.get_revision(request.workspace_id, result.new_revision)
    if new_revision is None:
        raise HTTPException(status_code=500, detail={"code": "REVISION_READBACK_FAILED"})
    place_lookup = {
        slot.place_id: slot.place
        for day in request.itinerary.days
        for slot in day.slots
    }
    return EditResponse(
        itinerary=revision_to_legacy(new_revision, thread_id=request.thread_id, place_lookup=place_lookup),
        patch=patch,
        violations=[],
        path_used=path_used,
        duration_ms=int((time.time() - started) * 1000),
        workspace_id=request.workspace_id,
        itinerary_revision=new_revision.revision,
        report_stale=result.report_stale,
    )


def _legacy_patch_command(server_revision, patch: ItineraryPatch):
    from app.itineraries.errors import InvalidEditCommandError
    from app.itineraries.models import EditOperation

    if patch.op == "swap_days":
        if patch.target_place_id is None:
            raise HTTPException(status_code=422, detail={"code": "INVALID_ITINERARY_EDIT_COMMAND"})
        try:
            target_day = int(patch.target_place_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "INVALID_ITINERARY_EDIT_COMMAND"}) from exc
        return EditOperation.REORDER_STOP, {"swap_day_indices": [patch.day_index, target_day]}

    target = None
    for day in server_revision.days:
        if day.day_index != patch.day_index:
            continue
        target = next((stop for stop in day.stops if stop.place_id == patch.target_place_id), None)
        if target:
            break
    if patch.op in {"remove_place", "replace_place"} and target is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_ITINERARY_EDIT_COMMAND", "message": "目标地点不在服务端 revision 中"},
        )
    if patch.op == "remove_place":
        return EditOperation.REMOVE_STOP, {"stop_id": target.stop_id}
    if patch.op == "replace_place" and patch.new_place_id:
        return EditOperation.REPLACE_STOP, {"stop_id": target.stop_id, "new_place_id": patch.new_place_id}
    raise HTTPException(
        status_code=422,
        detail={
            "code": InvalidEditCommandError.code,
            "message": f"legacy {patch.op} 缺少安全映射，请使用 revision-aware edits API",
        },
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
