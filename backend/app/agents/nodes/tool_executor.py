"""
Tool Executor 节点（ReAct Observe 步骤）

职责
----
1. 读取 ReAct Agent 输出的 tool_calls（来自最后一条 AIMessage）
2. 并行执行所有工具调用
3. 将工具结果写入 AgentState（amap_places / rag_chunks）
4. 将 ToolMessage 追加到 messages（供 LLM 下一轮 Observe）

工具调用与状态更新
-----------------
工具函数（@tool）返回 JSON 字符串，同时通过模块级缓存存储完整数据对象：
  - search_places       → amap_tool._amap_results_cache → state.amap_places（累积）
  - search_travel_notes → rag_tool._rag_results_cache  → state.rag_chunks（累积）
  - get_weather         → ToolMessage 只返回字符串，不更新结构化状态

累积语义：多次工具调用的结果合并（不覆盖），供 Synthesizer 整合所有信息。
"""

import asyncio
import json
from langchain_core.messages import ToolMessage, AIMessage

from app.agents.state import AgentState


async def run(state: AgentState) -> dict:
    """
    Tool Executor 节点入口

    执行 ReAct Agent 的工具调用，更新状态并返回 ToolMessages。
    """
    messages = state.get("messages", [])
    last_message = messages[-1] if messages else None

    # 检查最后一条消息是否有 tool_calls
    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        print("[ToolExecutor] 无工具调用，跳过")
        return {}

    tool_calls = last_message.tool_calls
    print(f"[ToolExecutor] 执行 {len(tool_calls)} 个工具调用: {[tc['name'] for tc in tool_calls]}")

    # 并行执行所有工具
    tasks = [_execute_tool_call(tc, state) for tc in tool_calls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 整合结果
    tool_messages: list[ToolMessage] = []
    amap_places_new: list = []
    rag_chunks_new: list = []

    for tc, result in zip(tool_calls, results):
        if isinstance(result, Exception):
            tool_output = json.dumps({"status": "error", "message": str(result)}, ensure_ascii=False)
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

    # 累积状态（不覆盖，而是追加）
    existing_places = list(state.get("amap_places", []))
    existing_chunks = list(state.get("rag_chunks", []))

    # 去重：按 place_id 去重地点
    existing_ids = {p.place_id for p in existing_places}
    merged_places = existing_places + [p for p in amap_places_new if p.place_id not in existing_ids]

    # chunk 按 note_id+chunk_idx 去重
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

    tool_output_str: 返回给 LLM 的字符串（加入 ToolMessage）
    places         : 完整 Place 对象列表（写入 amap_places）
    chunks         : 完整 chunk 字典列表（写入 rag_chunks）
    """
    name = tool_call["name"]
    args = tool_call.get("args", {})
    trip_city = state.get("trip_city") or "成都"

    # 如果 LLM 没有传入 city，自动填充 trip_city
    if "city" in args and not args["city"]:
        args["city"] = trip_city
    elif "city" not in args:
        args["city"] = trip_city

    if name == "search_places":
        from app.tools.amap_tool import search_places, get_cached_amap_results, clear_amap_cache
        clear_amap_cache()
        result = await search_places.ainvoke(args)
        places = get_cached_amap_results()
        return result, places, []

    elif name == "search_travel_notes":
        from app.tools.rag_tool import search_travel_notes, get_cached_rag_results, clear_rag_cache
        clear_rag_cache()
        result = await search_travel_notes.ainvoke(args)
        chunks = get_cached_rag_results()
        return result, [], chunks

    elif name == "get_weather":
        from app.tools.weather_tool import get_weather
        result = await get_weather.ainvoke(args)
        return result, [], []

    else:
        unknown_result = json.dumps({"status": "error", "message": f"未知工具：{name}"}, ensure_ascii=False)
        return unknown_result, [], []
