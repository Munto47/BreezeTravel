"""
POST /api/chat — 对话接口（SSE 流式响应）

Sprint 4 变更（X1）：
- 切换到 graph.astream_events(version="v2") 真实事件流
  - on_chain_start  → 节点启动 thinking 事件（实时可见）
  - on_chain_end    → 节点完成，提取输出数据

P1-12（2026-05）真流式：
  - tool_executor 完成后立即推送地点预览卡（Amap 原始数据），用户 ~5s 内可见
  - synthesizer 完成后推送 place_update 增量事件（LLM 增强：description/tags/tips/duration）
  - 文字仍批量推送（_TEXT_CHUNK_SIZE 字/帧）

SSE 事件格式（向后兼容）：
  progress:     {phase}
  place:        {place: Place}             # 首次推送的卡片
  place_update: {place_id, fields: {...}}  # LLM 增强后的字段增量
  text:         {delta: str}
  text_reset:   {}
  done:         {status, total_places}
  error:        {message}
"""

import hashlib
import hmac
import json
import re
import time
import asyncio
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage, AIMessage

from app.agents.graph import get_graph_with_persistence
from app.agents.state import default_working_context
from app.schemas.api import ChatRequest
from app.config import get_settings
from app.services.room_access import reject_claimed_identity, require_room_member
from app.utils.auth import get_optional_user
from app.observability.metrics import metrics as _prom_metrics
from app import metrics as _m
from app.api.rate_limit import check_public_chat_limit
from app.constraints.location import extract_explicit_district_constraint
from app.constraints.recommendation_intent import rank_places_for_request
from app.memory.policy import should_use_long_term_memory

router = APIRouter()

# 图内节点名集合（用于过滤 astream_events 中的无关事件）
_GRAPH_NODES = {"router", "tool_executor", "synthesizer", "critic"}

# 文字推送批大小（字符数），避免逐字 SSE 帧
_TEXT_CHUNK_SIZE = 12


def _progress(phase: str) -> str:
    return f"data: {json.dumps({'event': 'progress', 'data': {'phase': phase}}, ensure_ascii=False)}\n\n"


def _public_place_id(scope: str, place_id: str) -> str:
    secret = get_settings().jwt_secret_key.encode("utf-8")
    digest = hmac.new(
        secret,
        f"collaboration-place:{scope}:{place_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"place_{digest[:24]}"


def _public_collaboration_text(value: object) -> str:
    """Translate internal/provider phrasing before it reaches ordinary SSE clients."""

    text = str(value or "")
    replacements = (
        ("高德评分", "地点评分"),
        ("高德记录营业", "已记录营业时间"),
        ("高德参考价", "参考价"),
        ("高德步行路线", "步行路线"),
        ("高德坐标", "已确认坐标"),
        ("高德 POI", "地点"),
        ("高德记录", "地图记录"),
        ("安全降级回执", "当前结果"),
        ("预算约束回执", "预算情况"),
        ("时间约束回执", "时间情况"),
        ("排除项回执", "已按你的要求排除"),
        ("指定顺序回执", "建议顺序"),
        ("明确组合回执", "组合建议"),
        ("明确多场馆回执", "多场馆安排"),
        ("低转场备选池回执", "少转场备选"),
        ("转机路线回执", "转机路线提示"),
        ("证据门禁", "核验条件"),
        ("无证据", "暂时无法确认"),
        ("内部阶段", "处理过程"),
        ("回执", "结果"),
        ("证据", "核验信息"),
        ("POI", "地点"),
        ("高德", "地图服务"),
        ("LangGraph", "AI"),
        ("DeepSeek", "AI"),
        ("Qwen", "AI"),
    )
    for internal, public in replacements:
        text = text.replace(internal, public)
    text = re.sub(
        r"\[(?:ClustererAgent|RouterAgent|ToolExecutorAgent|SynthesizerAgent|CriticAgent)\]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:router|tool_executor|synthesizer|critic|provider|receipt|runspec)\b",
        "系统",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:模型)?版本\s*(?:v?\d+(?:\.\d+)*)?",
        "当前结果",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"引用\s*ID\s*[:：]?\s*[A-Za-z0-9._:-]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("模型", "AI")
    return text.strip()


def _public_place(place, scope: str) -> dict:
    coords = getattr(place, "coords", None)
    category = getattr(place, "category", None)
    return {
        "place_id": _public_place_id(scope, str(place.place_id)),
        "name": str(place.name),
        "category": getattr(category, "value", category),
        "address": str(place.address or ""),
        "coords": {
            "lng": float(getattr(coords, "lng", 0.0)),
            "lat": float(getattr(coords, "lat", 0.0)),
        },
        "city": str(place.city or ""),
        "district": str(place.district) if place.district else None,
        "rating": place.amap_rating,
        "average_price": place.amap_price,
        "opening_hours": place.opening_hours,
        "phone": place.phone,
        "description": _public_collaboration_text(place.description) if place.description else None,
        "tags": list(place.tags or []),
        "confirmation_actions": [
            _public_collaboration_text(action) for action in (place.confirmation_actions or [])
        ],
        "suggested_visit_minutes": place.estimated_duration,
    }


def _public_place_update(place, scope: str) -> dict:
    return {
        "place_id": _public_place_id(scope, str(place.place_id)),
        "fields": {
            "description": _public_collaboration_text(place.description) if place.description else None,
            "tags": list(place.tags or []),
            "confirmation_actions": [
                _public_collaboration_text(action) for action in (place.confirmation_actions or [])
            ],
            "suggested_visit_minutes": place.estimated_duration,
        },
    }


async def _events_until_deadline(events, deadline_monotonic: float):
    """Await each graph event within the one request-level deadline."""
    iterator = events.__aiter__()
    try:
        while True:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("chat request deadline exhausted")
            try:
                yield await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                break
    finally:
        close = getattr(iterator, "aclose", None)
        if close:
            await close()


async def _event_stream(request: ChatRequest, trace_id: str, http_request: Request):
    """生成 SSE 事件流（使用 graph.astream_events v2）"""
    graph = await get_graph_with_persistence()
    config = {
        "configurable": {"thread_id": request.thread_id},
        "metadata": {"trace_id": trace_id, "room_id": request.room_id},
    }
    start_time = time.time()
    public_scope = request.room_id or request.thread_id
    _prom_metrics.inc("agent_request_total", profile=get_settings().runtime_profile)

    # ── 加载用户长期偏好（Long-term Memory）────────────────────────────
    long_term_prefs = ""
    try:
        from app.memory.longterm import load_user_preferences
        if request.use_long_term_memory and should_use_long_term_memory(request.user_id):
            long_term_prefs = await asyncio.wait_for(
                load_user_preferences(request.user_id),
                timeout=2.0,
            )
    except Exception:
        pass

    # ── 构建初始状态 ──────────────────────────────────────────────────
    input_state = {
        "messages": [HumanMessage(content=request.message)],
        "thread_id": request.thread_id,
        "user_id": request.user_id,
        "long_term_memory_enabled": request.use_long_term_memory,
        "room_id": request.room_id,
        "trace_id": trace_id,
        "deadline_monotonic": time.monotonic() + get_settings().chat_deadline_seconds,
        "trip_city": request.trip_city,
        # Only a district the visitor explicitly named is a request-wide hard
        # boundary.  Landmark-derived districts stay slot-local; otherwise an
        # ordered route such as 故宫(东城) -> 景山(西城) deletes its second POI.
        "trip_district": extract_explicit_district_constraint(request.message),
        "amap_places": [],
        "eligible_amap_places": [],
        "eligible_candidates_computed": False,
        "rag_chunks": [],
        "citations": [],
        "tool_failures": [],
        "tool_receipts": [],
        "retrieval_audits": [],
        "retrieval_snapshots": [],
        "synthesized_places": [],
        "selected_place_ids": request.selected_place_ids,
        "intent": None,
        "query_rewrite": None,
        "routing_signals": [],
        "recommendation_plan": None,
        "slot_coverage": {},
        "missing_slot_ids": [],
        "itinerary": None,
        "final_response": None,
        "working_context": default_working_context(),
        "user_long_term_prefs": long_term_prefs or None,
        "react_iterations": 0,
        # Critic 反思节点初始状态
        "critic_retry": False,
        "critic_reason": None,
        "critic_iterations": 0,
        "critic_exhausted": False,
    }

    # Public progress is deliberately a closed vocabulary. Internal graph
    # nodes, tools and retry reasons never cross the collaboration boundary.
    yield _progress("UNDERSTANDING")

    places: list = []
    react_round = 0
    router_start_count = 0  # 区分首轮 vs. ReAct 循环中的第 N 轮
    _tool_call_counts: dict[str, int] = {}   # 本次请求内各工具调用次数
    _critic_fired = False
    _previewed_ids: set[str] = set()         # 已作为预览卡推送过的 place_id（P1-12）
    _preview_per_cat: dict[str, int] = {}    # 已预览的各类目计数（首批硬上限用）
    _reported_failure_count = 0              # tool_failures 在图状态中累积，SSE 只推送新增项
    _latest_grounded_places: list = []       # 模型超时时仍可返回已获取的 POI
    _latest_tool_failures: list[dict] = []
    degraded = False

    # 首批预览硬上限：每类 5 个，总 15 个（与 synthesizer 同步）
    _PREVIEW_PER_CAT = 5
    _PREVIEW_TOTAL = 15

    try:
        async for event in _events_until_deadline(
            graph.astream_events(input_state, config=config, version="v2"),
            input_state["deadline_monotonic"],
        ):
            if await http_request.is_disconnected():
                # Cancelling the generator propagates to LangGraph/tool awaits;
                # do not keep paying for a response the browser abandoned.
                raise asyncio.CancelledError("SSE client disconnected")
            etype: str = event["event"]
            ename: str = event.get("name", "")
            edata: dict = event.get("data", {})

            # 只处理图内节点事件，忽略 LangGraph 图级别事件和 LLM 内部事件
            if ename not in _GRAPH_NODES:
                continue

            # Only three stable, user-facing stages are public.
            if etype == "on_chain_start":
                if ename == "router":
                    router_start_count += 1
                elif ename == "tool_executor":
                    yield _progress("FINDING_PLACES")
                elif ename == "synthesizer":
                    yield _progress("ORGANIZING")

            # ── 节点完成：提取输出，生成详情 thinking + 业务事件 ──────
            elif etype == "on_chain_end":
                output: dict = edata.get("output") or {}

                if ename == "router":
                    # 检查 LLM 是否要求调用工具
                    messages = output.get("messages", [])
                    tool_names: list[str] = []
                    for m in messages:
                        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                            tool_names = [tc["name"] for tc in m.tool_calls]

                    if tool_names:
                        react_round += 1
                        for tn in tool_names:
                            _tool_call_counts[tn] = _tool_call_counts.get(tn, 0) + 1

                elif ename == "tool_executor":
                    amap_raw = output.get("amap_places", []) or []
                    if amap_raw:
                        _latest_grounded_places = list(amap_raw)
                    chunks_count = len(output.get("rag_chunks", []))
                    if chunks_count and not output.get("citations"):
                        _m.inc("rag_empty_count")
                    failures = output.get("tool_failures", []) or []
                    _latest_tool_failures = list(failures)
                    new_failures = failures[_reported_failure_count:]
                    _reported_failure_count = len(failures)
                    for failure in new_failures:
                        del failure
                        _m.inc("tool_error_count")
                        _m.inc("agent_degraded_count")
                        degraded = True

                    # P1-12 真流式：立即推送预览卡，不等 Synthesizer 完成
                    # 受首批上限约束（每类 5、总 15），按 amap_rating 降序优先推送
                    sorted_raw = rank_places_for_request(amap_raw, request.message)
                    for place in sorted_raw:
                        if place.place_id in _previewed_ids:
                            continue
                        if len(_previewed_ids) >= _PREVIEW_TOTAL:
                            break
                        cat_key = place.category.value if place.category else "unknown"
                        if _preview_per_cat.get(cat_key, 0) >= _PREVIEW_PER_CAT:
                            continue
                        _previewed_ids.add(place.place_id)
                        _preview_per_cat[cat_key] = _preview_per_cat.get(cat_key, 0) + 1
                        yield f"data: {json.dumps({'event': 'place', 'data': {'place': _public_place(place, public_scope)}}, ensure_ascii=False)}\n\n"
                        if len(_previewed_ids) == 1:
                            _prom_metrics.observe("agent_time_to_first_meaningful_place_seconds", time.time() - start_time, status="ok")

                elif ename == "synthesizer":
                    places = output.get("synthesized_places", [])
                    response_text = _public_collaboration_text(
                        output.get("final_response", "") or ""
                    )

                    final_ids = {p.place_id for p in places}
                    # 预览过但被 Synthesizer 过滤掉的（如菜系硬约束剔除） → 通知前端移除
                    dropped = _previewed_ids - final_ids
                    for pid in dropped:
                        yield f"data: {json.dumps({'event': 'place_remove', 'data': {'place_id': _public_place_id(public_scope, pid)}}, ensure_ascii=False)}\n\n"
                        _previewed_ids.discard(pid)

                    # 已预览的 place 走 place_update 增量；新增 place 走 place
                    for place in places:
                        if place.place_id in _previewed_ids:
                            update = _public_place_update(place, public_scope)
                            fields = {key: value for key, value in update["fields"].items() if value is not None}
                            if not fields:
                                continue
                            update["fields"] = fields
                            yield f"data: {json.dumps({'event': 'place_update', 'data': update}, ensure_ascii=False)}\n\n"
                        else:
                            _previewed_ids.add(place.place_id)
                            yield f"data: {json.dumps({'event': 'place', 'data': {'place': _public_place(place, public_scope)}}, ensure_ascii=False)}\n\n"

                    # 文本重置帧：Critic 触发重检索时 synthesizer 会再跑一次，
                    # 此时清空前一轮文本，避免前端追加导致重复段落
                    yield f"data: {json.dumps({'event': 'text_reset', 'data': {}}, ensure_ascii=False)}\n\n"

                    # 批量推送文字（_TEXT_CHUNK_SIZE 字符/帧，减少 SSE 帧数量）
                    for i in range(0, len(response_text), _TEXT_CHUNK_SIZE):
                        chunk = response_text[i:i + _TEXT_CHUNK_SIZE]
                        yield f"data: {json.dumps({'event': 'text', 'data': {'delta': chunk}}, ensure_ascii=False)}\n\n"

                elif ename == "critic":
                    retry = output.get("critic_retry", False)
                    if retry:
                        _critic_fired = True
                    if output.get("critic_exhausted", False):
                        degraded = True

        total_ms = int((time.time() - start_time) * 1000)
        _prom_metrics.observe("agent_duration_seconds", total_ms / 1000, status="ok" if places else "degraded")
        _prom_metrics.inc("agent_task_completed_total", status="ok" if places else "degraded")
        _prom_metrics.observe("agent_react_iterations", react_round, status="ok")
        public_status = "LIMITED" if degraded or _latest_tool_failures or not places else "READY"
        yield f"data: {json.dumps({'event': 'done', 'data': {'status': public_status, 'total_places': len(places)}}, ensure_ascii=False)}\n\n"

        # ── 写入 Agent 级指标 ──────────────────────────────────────
        if places:
            _m.inc("agent_success_count")
        else:
            _m.inc("agent_failure_count")
        if _critic_fired:
            _m.inc("critic_trigger_count")
        _m.inc("total_react_iterations", react_round)
        for tool_name, cnt in _tool_call_counts.items():
            _m.inc("tool_calls_total", cnt)
            if tool_name == "search_places":
                _m.inc("tool_calls_amap", cnt)
            elif tool_name == "search_travel_notes":
                _m.inc("tool_calls_rag", cnt)
            elif tool_name == "get_weather":
                _m.inc("tool_calls_weather", cnt)

    except asyncio.CancelledError:
        _m.inc("sse_disconnect_count")
        _prom_metrics.inc("sse_disconnect_total", reason="cancelled_by_client")
        raise
    except TimeoutError:
        _m.inc("agent_degraded_count")
        _prom_metrics.inc("agent_degraded_total", error_category="deadline_exceeded")
        if _latest_grounded_places and _previewed_ids:
            from app.agents.nodes.synthesizer import ensure_grounded_fallback_descriptions

            displayed = ensure_grounded_fallback_descriptions([
                place for place in _latest_grounded_places if place.place_id in _previewed_ids
            ])
            for place in displayed:
                yield f"data: {json.dumps({'event': 'place_update', 'data': _public_place_update(place, public_scope)}, ensure_ascii=False)}\n\n"
            fallback_text = "说明整理超时，已先返回可核对的地点基础信息；营业时间和价格请以最新页面为准。"
            yield f"data: {json.dumps({'event': 'text_reset', 'data': {}}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'event': 'text', 'data': {'delta': fallback_text}}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'event': 'done', 'data': {'status': 'LIMITED', 'total_places': len(displayed)}}, ensure_ascii=False)}\n\n"
            _m.inc("agent_success_count")
        else:
            _m.inc("agent_failure_count")
            yield f"data: {json.dumps({'event': 'error', 'data': {'message': '暂时无法完成，请稍后重试。'}}, ensure_ascii=False)}\n\n"
    except Exception:
        _m.inc("agent_failure_count")
        _m.inc("agent_degraded_count")
        _m.inc("tool_error_count")
        yield f"data: {json.dumps({'event': 'error', 'data': {'message': '暂时无法完成，请稍后重试。'}}, ensure_ascii=False)}\n\n"


@router.post("/chat")
async def chat(request: ChatRequest, http_request: Request, current_user: str | None = Depends(get_optional_user)):
    cfg = get_settings()
    if request.room_id:
        if current_user is None:
            raise HTTPException(status_code=401, detail="请先登录")
        reject_claimed_identity(request.user_id, current_user)
        await require_room_member(request.room_id, current_user, thread_id=request.thread_id)
        request.user_id = current_user
    elif not cfg.demo_mode:
        raise HTTPException(status_code=400, detail="room_id 必填")
    """
    AI 对话接口，返回 SSE 流式响应。

    事件类型：
    - progress: {phase: UNDERSTANDING | FINDING_PLACES | ORGANIZING}
    - place:    {place: Place}
    - text:     {delta: str}
    - done:     {status: READY | LIMITED, total_places: int}
    - error:    {message: str}
    """
    await check_public_chat_limit(http_request)
    trace_id = uuid4().hex
    return StreamingResponse(
        _event_stream(request, trace_id, http_request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
