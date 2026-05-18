"""
POST /api/chat — 对话接口（SSE 流式响应）

Sprint 2 变更：
- 对话开始前加载用户长期偏好（Long-term Memory），注入 initial state
- 初始 state 新增 working_context / user_long_term_prefs / react_iterations
- SSE 事件处理新增 tool_executor 节点（展示工具调用可视化）

SSE 事件格式（不变，向后兼容）：
  thinking: {node, summary, ms}
  place:    {place: Place}
  text:     {delta: str}
  done:     {total_places, total_ms}
  error:    {message}
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

router = APIRouter()


async def _event_stream(request: ChatRequest):
    """生成 SSE 事件流"""
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
                timeout=2.0,  # 最多等 2 秒，不阻塞对话
            )
    except Exception:
        pass  # 加载失败静默跳过

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
        # Sprint 2 新增
        "working_context": default_working_context(),
        "user_long_term_prefs": long_term_prefs or None,
        "react_iterations": 0,
    }

    # ── 推送初始 thinking 事件 ────────────────────────────────────────
    mem_hint = "（已加载历史偏好）" if long_term_prefs else ""
    yield _thinking("router", f"正在分析需求{mem_hint}...", 0)

    places: list = []
    response_text: str = ""
    react_round = 0

    try:
        async for chunk in graph.astream(input_state, config=config):
            elapsed = int((time.time() - start_time) * 1000)

            # ── Router / ReAct Agent ──────────────────────────────────
            if "router" in chunk:
                router_state = chunk["router"]
                messages = router_state.get("messages", [])

                # 检查 LLM 是否输出了工具调用
                tool_names = []
                for m in messages:
                    if isinstance(m, AIMessage) and m.tool_calls:
                        tool_names = [tc["name"] for tc in m.tool_calls]

                if tool_names:
                    react_round += 1
                    tool_labels = {
                        "search_places": "高德地点搜索",
                        "search_travel_notes": "游记攻略检索",
                        "get_weather": "天气查询",
                    }
                    names_cn = "、".join(tool_labels.get(n, n) for n in tool_names)
                    yield _thinking("router", f"思考完成，调用工具：{names_cn}", elapsed)
                else:
                    iterations = router_state.get("react_iterations", 0)
                    if iterations > 0:
                        yield _thinking("router", "信息收集完毕，准备生成推荐", elapsed)

            # ── Tool Executor ─────────────────────────────────────────
            elif "tool_executor" in chunk:
                te_state = chunk["tool_executor"]
                places_count = len(te_state.get("amap_places", []))
                chunks_count = len(te_state.get("rag_chunks", []))

                parts = []
                if places_count:
                    parts.append(f"地点 {places_count} 个")
                if chunks_count:
                    parts.append(f"游记 {chunks_count} 条")
                summary = "、".join(parts) if parts else "工具执行完成"

                yield _thinking("tool_executor", f"工具返回：{summary}", elapsed)

            # ── Synthesizer ───────────────────────────────────────────
            elif "synthesizer" in chunk:
                synth_state = chunk["synthesizer"]
                places = synth_state.get("synthesized_places", [])
                response_text = synth_state.get("final_response", "")
                yield _thinking("synthesizer", f"整合完成，推荐 {len(places)} 个地点", elapsed)

                # 逐个推送地点卡片
                for place in places:
                    yield f"data: {json.dumps({'event': 'place', 'data': {'place': place.model_dump()}}, ensure_ascii=False)}\n\n"

                # 逐字推送文字回复
                for char in response_text:
                    yield f"data: {json.dumps({'event': 'text', 'data': {'delta': char}}, ensure_ascii=False)}\n\n"

        total_ms = int((time.time() - start_time) * 1000)
        yield f"data: {json.dumps({'event': 'done', 'data': {'total_places': len(places), 'total_ms': total_ms, 'react_rounds': react_round}}, ensure_ascii=False)}\n\n"

    except Exception as exc:
        yield f"data: {json.dumps({'event': 'error', 'data': {'message': str(exc)}}, ensure_ascii=False)}\n\n"


def _thinking(node: str, summary: str, ms: int) -> str:
    return f"data: {json.dumps({'event': 'thinking', 'data': {'node': node, 'summary': summary, 'ms': ms}}, ensure_ascii=False)}\n\n"


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
