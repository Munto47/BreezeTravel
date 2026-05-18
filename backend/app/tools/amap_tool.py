"""
高德地图 POI 搜索工具

封装了 amap_search 节点的核心逻辑，暴露为 LangChain @tool。
ReAct Agent 通过 LLM tool calling 调用此工具。

工具输入：query（搜索关键词）、city（城市）、category（品类过滤）
工具输出：JSON 字符串（地点列表摘要），供 LLM 读取推理；
         同时通过 _amap_results_cache 缓存完整 Place 对象，
         供 tool_executor 节点写入 AgentState.amap_places。
"""

import json
from typing import Annotated

from langchain_core.tools import tool

# 跨节点数据传递缓存（tool 函数无法直接写入 AgentState）
# tool_executor 节点会读取这里的数据写入状态
_amap_results_cache: list = []


def get_cached_amap_results() -> list:
    """获取最近一次 search_places 调用的完整 Place 对象列表"""
    return _amap_results_cache.copy()


def clear_amap_cache():
    """清空缓存"""
    _amap_results_cache.clear()


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

    返回：找到的地点列表（名称、评分、地址、类型），JSON 格式。
    """
    from app.agents.nodes import amap_search as amap_node
    from app.agents.state import AgentState
    from langchain_core.messages import HumanMessage

    # 构造最小化的 AgentState（amap_search.run 只需这几个字段）
    mock_state: AgentState = {
        "messages": [HumanMessage(content=query)],
        "thread_id": "tool-call",
        "user_id": "tool-call",
        "trip_city": city,
        "intent": "amap",
        "query_rewrite": f"{query} {category}".strip(),
        "amap_places": [],
        "rag_chunks": [],
        "synthesized_places": [],
        "final_response": None,
        "itinerary": None,
        "selected_place_ids": [],
        "working_context": None,
        "user_long_term_prefs": None,
        "react_iterations": 0,
    }

    try:
        result = await amap_node.run(mock_state)
        places = result.get("amap_places", [])

        # 缓存完整 Place 对象（tool_executor 会读取）
        global _amap_results_cache
        _amap_results_cache = places

        # 返回给 LLM 的摘要（简洁可读）
        if not places:
            return json.dumps({"status": "no_results", "message": f"未找到相关地点：{query}"}, ensure_ascii=False)

        summary = [
            {
                "name": p.name,
                "category": p.category.value if p.category else "未知",
                "rating": p.amap_rating,
                "address": p.address,
                "place_id": p.place_id,
            }
            for p in places[:8]
        ]
        return json.dumps(
            {"status": "ok", "count": len(places), "places": summary},
            ensure_ascii=False, indent=2,
        )

    except Exception as exc:
        _amap_results_cache = []
        return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)
