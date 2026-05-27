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
  thinking:     {node, summary, ms}
  place:        {place: Place}             # 首次推送的卡片
  place_update: {place_id, fields: {...}}  # LLM 增强后的字段增量
  text:         {delta: str}
  text_reset:   {}
  done:         {total_places, total_ms, react_rounds}
  error:        {message}
"""

import json
import time
import asyncio

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage, AIMessage

from app.agents.graph import get_graph_with_persistence
from app.agents.state import default_working_context
from app.schemas.api import ChatRequest
from app import metrics as _m

router = APIRouter()

# 图内节点名集合（用于过滤 astream_events 中的无关事件）
_GRAPH_NODES = {"router", "tool_executor", "synthesizer", "critic"}

_TOOL_LABELS = {
    "search_places": "高德地点搜索",
    "search_travel_notes": "游记攻略检索",
    "get_weather": "天气查询",
}

_NODE_START_SUMMARY = {
    "router":        "意图分析中...",
    "tool_executor": "正在执行工具调用...",
    "synthesizer":   "整合数据，生成推荐...",
    "critic":        "质量检查中...",
}

# 文字推送批大小（字符数），避免逐字 SSE 帧
_TEXT_CHUNK_SIZE = 12


def _thinking(node: str, summary: str, ms: int) -> str:
    return f"data: {json.dumps({'event': 'thinking', 'data': {'node': node, 'summary': summary, 'ms': ms}}, ensure_ascii=False)}\n\n"


async def _event_stream(request: ChatRequest):
    """生成 SSE 事件流（使用 graph.astream_events v2）"""
    graph = await get_graph_with_persistence()
    config = {"configurable": {"thread_id": request.thread_id}}
    start_time = time.time()

    # ── 加载用户长期偏好（Long-term Memory）────────────────────────────
    long_term_prefs = ""
    try:
        from app.memory.longterm import load_user_preferences
        if request.user_id and request.user_id not in ("anonymous", ""):
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
        "trip_city": request.trip_city,
        "amap_places": [],
        "rag_chunks": [],
        "synthesized_places": [],
        "selected_place_ids": request.selected_place_ids,
        "intent": None,
        "query_rewrite": None,
        "itinerary": None,
        "final_response": None,
        "working_context": default_working_context(),
        "user_long_term_prefs": long_term_prefs or None,
        "react_iterations": 0,
        # Critic 反思节点初始状态
        "critic_retry": False,
        "critic_reason": None,
        "critic_iterations": 0,
    }

    # ── 推送初始 thinking 事件（让用户立即看到响应）─────────────────
    mem_hint = "（已加载历史偏好）" if long_term_prefs else ""
    yield _thinking("router", f"正在分析需求{mem_hint}...", 0)

    places: list = []
    react_round = 0
    router_start_count = 0  # 区分首轮 vs. ReAct 循环中的第 N 轮
    _tool_call_counts: dict[str, int] = {}   # 本次请求内各工具调用次数
    _critic_fired = False
    _previewed_ids: set[str] = set()         # 已作为预览卡推送过的 place_id（P1-12）
    _preview_per_cat: dict[str, int] = {}    # 已预览的各类目计数（首批硬上限用）

    # LLM 增强后可能新增的字段（增量推送用）
    _ENRICH_FIELDS = ("description", "tags", "rag_meta", "estimated_duration", "duration_basis")

    # 首批预览硬上限：每类 5 个，总 15 个（与 synthesizer 同步）
    _PREVIEW_PER_CAT = 5
    _PREVIEW_TOTAL = 15

    try:
        async for event in graph.astream_events(input_state, config=config, version="v2"):
            etype: str = event["event"]
            ename: str = event.get("name", "")
            edata: dict = event.get("data", {})

            # 只处理图内节点事件，忽略 LangGraph 图级别事件和 LLM 内部事件
            if ename not in _GRAPH_NODES:
                continue

            elapsed = int((time.time() - start_time) * 1000)

            # ── 节点启动：立即推送 thinking 让前端显示进度 ─────────────
            if etype == "on_chain_start":
                if ename == "router":
                    router_start_count += 1
                    if router_start_count > 1:
                        # 第 2+ 轮 ReAct 循环，表示工具结果已收到，Router 再次思考
                        yield _thinking("router", "工具结果已获取，继续分析...", elapsed)
                    # 首轮已在上面手动 emit，跳过避免重复
                elif ename == "tool_executor":
                    yield _thinking("tool_executor", _NODE_START_SUMMARY["tool_executor"], elapsed)
                elif ename == "synthesizer":
                    yield _thinking("synthesizer", _NODE_START_SUMMARY["synthesizer"], elapsed)

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
                        names_cn = "、".join(_TOOL_LABELS.get(n, n) for n in tool_names)
                        yield _thinking("router", f"决策：调用工具 {names_cn}", elapsed)
                        for tn in tool_names:
                            _tool_call_counts[tn] = _tool_call_counts.get(tn, 0) + 1
                    elif router_start_count > 1:
                        # 非首轮且无 tool_calls，意味着信息已够，即将进入 synthesizer
                        yield _thinking("router", "信息收集完毕，准备生成推荐", elapsed)

                elif ename == "tool_executor":
                    amap_raw = output.get("amap_places", []) or []
                    chunks_count = len(output.get("rag_chunks", []))
                    parts = []
                    if amap_raw:
                        parts.append(f"地点 {len(amap_raw)} 个")
                    if chunks_count:
                        parts.append(f"游记 {chunks_count} 条")
                    summary = "、".join(parts) if parts else "工具执行完成"
                    yield _thinking("tool_executor", f"工具返回：{summary}", elapsed)

                    # P1-12 真流式：立即推送预览卡，不等 Synthesizer 完成
                    # 受首批上限约束（每类 5、总 15），按 amap_rating 降序优先推送
                    sorted_raw = sorted(
                        amap_raw,
                        key=lambda p: (p.amap_rating if p.amap_rating is not None else 0.0),
                        reverse=True,
                    )
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
                        yield f"data: {json.dumps({'event': 'place', 'data': {'place': place.model_dump()}}, ensure_ascii=False)}\n\n"

                elif ename == "synthesizer":
                    places = output.get("synthesized_places", [])
                    response_text: str = output.get("final_response", "") or ""

                    yield _thinking("synthesizer", f"推荐已生成：{len(places)} 个地点", elapsed)

                    final_ids = {p.place_id for p in places}
                    # 预览过但被 Synthesizer 过滤掉的（如菜系硬约束剔除） → 通知前端移除
                    dropped = _previewed_ids - final_ids
                    for pid in dropped:
                        yield f"data: {json.dumps({'event': 'place_remove', 'data': {'place_id': pid}}, ensure_ascii=False)}\n\n"
                        _previewed_ids.discard(pid)

                    # 已预览的 place 走 place_update 增量；新增 place 走 place
                    for place in places:
                        if place.place_id in _previewed_ids:
                            dumped = place.model_dump()
                            fields = {k: dumped.get(k) for k in _ENRICH_FIELDS if dumped.get(k) is not None}
                            if not fields:
                                continue
                            yield f"data: {json.dumps({'event': 'place_update', 'data': {'place_id': place.place_id, 'fields': fields}}, ensure_ascii=False)}\n\n"
                        else:
                            _previewed_ids.add(place.place_id)
                            yield f"data: {json.dumps({'event': 'place', 'data': {'place': place.model_dump()}}, ensure_ascii=False)}\n\n"

                    # 文本重置帧：Critic 触发重检索时 synthesizer 会再跑一次，
                    # 此时清空前一轮文本，避免前端追加导致重复段落
                    yield f"data: {json.dumps({'event': 'text_reset', 'data': {}}, ensure_ascii=False)}\n\n"

                    # 批量推送文字（_TEXT_CHUNK_SIZE 字符/帧，减少 SSE 帧数量）
                    for i in range(0, len(response_text), _TEXT_CHUNK_SIZE):
                        chunk = response_text[i:i + _TEXT_CHUNK_SIZE]
                        yield f"data: {json.dumps({'event': 'text', 'data': {'delta': chunk}}, ensure_ascii=False)}\n\n"

                elif ename == "critic":
                    retry = output.get("critic_retry", False)
                    reason = output.get("critic_reason", "")
                    if retry:
                        _critic_fired = True
                        yield _thinking("critic", f"结果待优化（{reason}），正在重新搜索...", elapsed)
                    else:
                        yield _thinking("critic", "质量检查通过", elapsed)

        total_ms = int((time.time() - start_time) * 1000)
        yield f"data: {json.dumps({'event': 'done', 'data': {'total_places': len(places), 'total_ms': total_ms, 'react_rounds': react_round}}, ensure_ascii=False)}\n\n"

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

    except Exception as exc:
        _m.inc("agent_failure_count")
        yield f"data: {json.dumps({'event': 'error', 'data': {'message': str(exc)}}, ensure_ascii=False)}\n\n"


@router.post("/chat")
async def chat(request: ChatRequest):
    """
    AI 对话接口，返回 SSE 流式响应。

    事件类型：
    - thinking: {node: str, summary: str, ms: int}
    - place:    {place: Place}
    - text:     {delta: str}
    - done:     {total_places: int, total_ms: int, react_rounds: int}
    - error:    {message: str}
    """
    return StreamingResponse(
        _event_stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
