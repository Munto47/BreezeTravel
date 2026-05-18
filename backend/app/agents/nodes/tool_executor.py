"""
Tool Executor 节点（ReAct Observe 步骤）

职责
----
1. 读取 ReAct Agent 输出的 tool_calls（来自最后一条 AIMessage）
2. 并行执行所有工具调用（直接调用底层函数，不经过 @tool 包装器）
3. 将工具结果写入 AgentState（amap_places / rag_chunks 累积）
4. 将 ToolMessage 追加到 messages（供 LLM 下一轮 Observe）

设计说明：并发安全
-----------------
工具执行不使用任何模块级全局缓存。
每次工具调用的结果通过函数返回值传递：
  - _execute_amap()  → 直接返回 list[Place]（无副作用）
  - _execute_rag()   → 直接返回 list[dict]（无副作用）
  - _execute_weather() → 只返回字符串，不更新结构化状态

多个并发用户的请求彼此完全隔离，不会产生数据竞争。

累积语义
--------
多轮 ReAct 循环中，工具结果累积（不覆盖），以 place_id / (note_id, chunk_idx) 去重。
"""

import asyncio
import json
from langchain_core.messages import ToolMessage, AIMessage

from app.agents.state import AgentState


async def run(state: AgentState) -> dict:
    """Tool Executor 节点入口：并行执行工具调用，累积状态"""
    messages = state.get("messages", [])
    last_message = messages[-1] if messages else None

    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        print("[ToolExecutor] 无工具调用，跳过")
        return {}

    tool_calls = last_message.tool_calls
    print(f"[ToolExecutor] 执行 {len(tool_calls)} 个工具：{[tc['name'] for tc in tool_calls]}")

    # ── 并行执行所有工具调用 ──────────────────────────────────────────
    tasks = [_execute_tool_call(tc, state) for tc in tool_calls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # ── 整合结果 ────────────────────────────────────────────────────
    tool_messages: list[ToolMessage] = []
    amap_places_new: list = []
    rag_chunks_new: list = []

    for tc, result in zip(tool_calls, results):
        if isinstance(result, Exception):
            tool_output = json.dumps(
                {"status": "error", "message": str(result)}, ensure_ascii=False
            )
            print(f"[ToolExecutor] 工具 {tc['name']} 执行异常：{result}")
        else:
            tool_output, extra_places, extra_chunks = result
            amap_places_new.extend(extra_places)
            rag_chunks_new.extend(extra_chunks)

        tool_messages.append(ToolMessage(
            content=tool_output,
            tool_call_id=tc["id"],
            name=tc["name"],
        ))

    # ── 累积状态（去重合并，不覆盖历史结果） ─────────────────────────
    existing_places = list(state.get("amap_places", []))
    existing_chunks = list(state.get("rag_chunks", []))

    existing_place_ids = {p.place_id for p in existing_places}
    merged_places = existing_places + [
        p for p in amap_places_new if p.place_id not in existing_place_ids
    ]

    existing_chunk_keys = {(c["note_id"], c.get("chunk_idx", 0)) for c in existing_chunks}
    merged_chunks = existing_chunks + [
        c for c in rag_chunks_new
        if (c["note_id"], c.get("chunk_idx", 0)) not in existing_chunk_keys
    ]

    print(
        f"[ToolExecutor] 完成：+{len(amap_places_new)} 地点, +{len(rag_chunks_new)} chunks | "
        f"累计：{len(merged_places)} 地点, {len(merged_chunks)} chunks"
    )

    return {
        "messages": tool_messages,
        "amap_places": merged_places,
        "rag_chunks": merged_chunks,
    }


async def _execute_tool_call(
    tool_call: dict,
    state: AgentState,
) -> tuple[str, list, list]:
    """
    执行单个工具调用，返回 (tool_output_str, places, chunks)

    直接调用底层函数（不经过 @tool 包装器），每次调用结果独立，无全局状态共享。
    """
    name = tool_call["name"]
    args = dict(tool_call.get("args", {}))
    trip_city = state.get("trip_city") or "成都"

    # LLM 未传 city 时自动填充 trip_city
    if not args.get("city"):
        args["city"] = trip_city

    # ── search_places → 调用高德底层函数 ────────────────────────────
    if name == "search_places":
        from app.tools.amap_tool import _run_amap_search

        places = await _run_amap_search(
            query=args.get("query", ""),
            city=args["city"],
            category=args.get("category", ""),
        )

        if places:
            summary = [
                {
                    "name": p.name,
                    "category": p.category.value if p.category else "未知",
                    "rating": p.amap_rating,
                    "place_id": p.place_id,
                }
                for p in places[:8]
            ]
            tool_output = json.dumps(
                {"status": "ok", "count": len(places), "places": summary},
                ensure_ascii=False,
            )
        else:
            tool_output = json.dumps(
                {"status": "no_results", "message": f"未找到：{args.get('query', '')}"},
                ensure_ascii=False,
            )
        return tool_output, places, []

    # ── search_travel_notes → 调用 RAG 底层函数 ─────────────────────
    elif name == "search_travel_notes":
        from app.tools.rag_tool import _run_rag_search

        chunks = await _run_rag_search(
            query=args.get("query", ""),
            city=args["city"],
        )

        if chunks:
            snippets = [
                {
                    "excerpt": c["content"][:200],
                    "relevance": round(c.get("similarity", 0), 3),
                }
                for c in chunks[:5]
            ]
            tool_output = json.dumps(
                {"status": "ok", "count": len(chunks), "snippets": snippets},
                ensure_ascii=False,
            )
        else:
            tool_output = json.dumps(
                {"status": "no_results", "message": "暂无相关游记"},
                ensure_ascii=False,
            )
        return tool_output, [], chunks

    # ── get_weather → 调用天气工具 ───────────────────────────────────
    elif name == "get_weather":
        from app.tools.weather_tool import get_weather

        result = await get_weather.ainvoke(args)
        return result, [], []

    # ── 未知工具 ─────────────────────────────────────────────────────
    else:
        output = json.dumps(
            {"status": "error", "message": f"未知工具：{name}"},
            ensure_ascii=False,
        )
        return output, [], []
