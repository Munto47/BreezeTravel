"""
高德地图 POI 搜索工具

封装为 LangChain @tool，供 ReAct Agent 通过 LLM tool calling 触发。

设计说明
--------
@tool 函数只负责两件事：
  1. 向 LLM 暴露清晰的函数签名（参数名、docstring），便于 LLM 决策何时调用
  2. 返回人类可读的 JSON 字符串（LLM 在 Observe 阶段读取推理）

实际执行逻辑（获取完整 Place 对象）由 tool_executor.py 直接调用底层节点函数，
不再经过 @tool 包装器，彻底避免了模块级缓存带来的并发竞态条件。
"""

from typing import Annotated

from langchain_core.tools import tool


@tool
async def search_places(
    query: Annotated[str, "搜索关键词，如'火锅''熊猫基地''精品酒店'"],
    city: Annotated[str, "目的地城市，如'成都''北京'"],
    category: Annotated[str, "可选品类过滤：'景点'|'美食'|'住宿'，留空则不过滤"] = "",
) -> str:
    """
    搜索高德地图 POI 地点（景点、餐厅、住宿等）。

    当用户询问：
    - 客观属性（评分、位置、营业时间、推荐景点）
    - 找具体类型地点（"附近有什么火锅""找个酒店"）
    时，调用此工具。

    返回找到的地点列表（名称、评分、地址、类型），JSON 格式。
    注意：完整地点数据由系统内部处理，此处返回摘要供 LLM 推理。
    """
    # 仅用于 LLM schema 暴露，实际数据由 tool_executor 调用 _run_amap_search() 获取
    return f'{{"status": "ok", "query": "{query}", "city": "{city}"}}'


async def _run_amap_search(query: str, city: str, category: str = "") -> list:
    """
    底层高德搜索执行函数（供 tool_executor 直接调用，无竞态风险）

    Args:
        query    : 搜索关键词
        city     : 目的地城市
        category : 可选品类过滤

    Returns:
        Place 对象列表
    """
    from app.agents.nodes import amap_search as amap_node
    from app.agents.state import AgentState, default_working_context
    from langchain_core.messages import HumanMessage

    search_query = f"{query} {category}".strip()

    mock_state: AgentState = {
        "messages": [HumanMessage(content=search_query)],
        "thread_id": "tool-call",
        "user_id": "tool-call",
        "trip_city": city,
        "intent": "amap",
        "query_rewrite": search_query,
        "amap_places": [],
        "rag_chunks": [],
        "synthesized_places": [],
        "final_response": None,
        "itinerary": None,
        "selected_place_ids": [],
        "working_context": default_working_context(),
        "user_long_term_prefs": None,
        "react_iterations": 0,
    }

    try:
        result = await amap_node.run(mock_state)
        return result.get("amap_places", [])
    except Exception as exc:
        print(f"[AmapTool] 搜索失败：{exc}")
        return []
