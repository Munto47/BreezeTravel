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
    query: Annotated[str, "搜索关键词，应包含口味/菜系/风格等修饰词。示例：'素食餐厅''韩国料理''网红咖啡''连锁火锅''熊猫基地''精品酒店'"],
    city: Annotated[str, "目的地城市，如'成都''北京'"],
    category: Annotated[str, "可选品类过滤：'景点'|'美食'|'住宿'，留空则不过滤"] = "",
    prefer_trending: Annotated[bool, "是否优先返回热门网红/高人气地点（用于'想去网红店''流行的'等诉求）"] = False,
    prefer_chain: Annotated[bool, "是否优先返回连锁品牌（用于'靠谱''有保障''连锁'等诉求）"] = False,
) -> str:
    """
    搜索高德地图 POI 地点（景点、餐厅、住宿等）。

    偏好感知示例（LLM 应据此构建搜索词）：
    - 用户说"我是韩国人" → query="韩国料理 韩式烤肉", prefer_chain=True
    - 用户说"素食主义者" → query="素食餐厅 蔬食"
    - 用户说"想吃流行的网红店" → query="热门餐厅", prefer_trending=True
    - 用户说"推荐靠谱的早餐" → query="早餐 连锁", prefer_chain=True
    - 用户说"不吃辣，想找清淡点的" → query="清淡餐厅 粤菜 淮扬菜"

    当用户询问：
    - 客观属性（评分、位置、营业时间、推荐景点）→ 调用此工具
    - 找具体类型地点（"附近有什么火锅""找个酒店"）→ 调用此工具

    返回找到的地点列表（名称、评分、地址、类型），JSON 格式。
    """
    return f'{{"status": "ok", "query": "{query}", "city": "{city}", "prefer_trending": {str(prefer_trending).lower()}, "prefer_chain": {str(prefer_chain).lower()}}}'


async def _run_amap_search(
    query: str,
    city: str,
    category: str = "",
    prefer_trending: bool = False,
    prefer_chain: bool = False,
) -> list:
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

    # 根据连锁/热门偏好追加修饰词，提升高德关键词精准度
    parts = [query]
    if prefer_chain and "连锁" not in query:
        parts.append("连锁")
    if category:
        parts.append(category)
    search_query = " ".join(parts).strip()

    ctx = default_working_context()
    ctx["prefer_chain"] = prefer_chain
    ctx["prefer_trending"] = prefer_trending

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
        "working_context": ctx,
        "user_long_term_prefs": None,
        "react_iterations": 0,
    }

    try:
        result = await amap_node.run(mock_state)
        places = result.get("amap_places", [])

        # prefer_trending：按评分降序重排（高德 weight 排序仅真实模式有效）
        if prefer_trending:
            places = sorted(places, key=lambda p: p.amap_rating or 0, reverse=True)

        return places
    except Exception as exc:
        print(f"[AmapTool] 搜索失败：{exc}")
        return []
