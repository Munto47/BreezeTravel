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
import re

from langchain_core.tools import tool


@tool
async def search_places(
    query: Annotated[str, "搜索关键词，应包含口味/菜系/风格等修饰词。示例：'素食餐厅''韩国料理''网红咖啡''连锁火锅''熊猫基地''精品酒店'"],
    city: Annotated[str, "目的地城市，如'成都''北京'"],
    district: Annotated[str, "用户明确限定的行政区，如'闵行区'；未限定时留空"] = "",
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
    district: str = "",
    category: str = "",
    prefer_trending: bool = False,
    prefer_chain: bool = False,
    typecodes: list[str] | None = None,
) -> list:
    places, _ = await _run_amap_search_with_audit(
        query=query,
        city=city,
        district=district,
        category=category,
        prefer_trending=prefer_trending,
        prefer_chain=prefer_chain,
        typecodes=typecodes,
    )
    return places


async def _run_amap_search_with_audit(
    query: str,
    city: str,
    district: str = "",
    category: str = "",
    prefer_trending: bool = False,
    prefer_chain: bool = False,
    slot_id: str = "",
    anchor_place: str = "",
    radius_m: int = 0,
    typecodes: list[str] | None = None,
) -> tuple[list, list[dict]]:
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
    provider_query = query
    if anchor_place:
        provider_query = provider_query.replace(anchor_place, "").replace("附近", "").strip()
    provider_query = _compile_provider_keyword(provider_query, category)
    parts = [provider_query or category]
    if prefer_chain and "连锁" not in query:
        parts.append("连锁")
    # Category is carried by a closed Amap typecode and post-filter contract;
    # it is not appended to the keyword. Provider keywords stay short and do
    # not absorb administrative or natural-language constraints.
    search_query = " ".join(parts).strip()

    ctx = default_working_context()
    ctx["prefer_chain"] = prefer_chain
    ctx["prefer_trending"] = prefer_trending

    mock_state: AgentState = {
        "messages": [HumanMessage(content=search_query)],
        "thread_id": "tool-call",
        "user_id": "tool-call",
        "trip_city": city,
        "trip_district": district or None,
        "intent": "amap",
        "query_rewrite": search_query,
        "amap_places": [],
        "rag_chunks": [],
        "retrieval_audits": [],
        "synthesized_places": [],
        "final_response": None,
        "itinerary": None,
        "selected_place_ids": [],
        "working_context": ctx,
        "user_long_term_prefs": None,
        "react_iterations": 0,
        "search_anchor": anchor_place or None,
        "search_radius_m": radius_m or None,
        "search_typecodes": list(typecodes or []),
    }

    result = await amap_node.run(mock_state)
    places = result.get("amap_places", [])
    audits = result.get("retrieval_audits", [])

    # prefer_trending：按评分降序重排（高德 weight 排序仅真实模式有效）
    if prefer_trending:
        places = sorted(places, key=lambda p: p.amap_rating or 0, reverse=True)

    from app.constraints.location import filter_human_suitable_places, filter_places_by_district
    from app.constraints.recommendation_intent import filter_places_for_request, rank_places_for_request
    places = filter_human_suitable_places(filter_places_by_district(places, district))
    places = filter_places_for_request(places, query, category)
    places = rank_places_for_request(places, query)
    if slot_id:
        places = [
            place.model_copy(update={
                "recommendation_slot_ids": list(dict.fromkeys([
                    *place.recommendation_slot_ids, slot_id,
                ])),
            })
            for place in places
        ]
    if audits:
        audits[-1] = {**audits[-1], "slot_id": slot_id or None, "result_count": len(places)}
    return places, audits


def _compile_provider_keyword(query: str, category: str) -> str:
    """Keep semantic constraints in the slot; send Amap short POI keywords."""
    compact = re.sub(r"\s+", " ", str(query or "")).strip()
    if "住宿" in category or "酒店" in category:
        # Room type, accessibility, shuttle, pet and quietness are not Amap POI
        # keyword capabilities. They remain field-level evidence constraints.
        if "四合院" in compact:
            return "四合院 酒店"
        if "历史建筑" in compact or "老洋房" in compact:
            return "历史建筑 酒店"
        if "客栈" in compact or "民宿" in compact:
            return "客栈 民宿"
        if "精品" in compact:
            return "精品酒店"
        if "公寓式" in compact:
            return "公寓式酒店"
        if "经济型" in compact:
            return "经济型酒店"
        return "酒店"
    if "美食" in category or "餐饮" in category:
        cuisines = [term for term in (
            "北京菜", "烤鸭", "清真", "素食", "川菜", "湘菜", "粤菜",
            "本帮菜", "杭帮菜", "生煎", "小笼", "片儿川", "火锅", "烧烤",
            "日料", "咖啡", "茶馆", "甜品", "早餐", "夜宵", "豆汁", "炒肝",
            "卤煮", "锅贴", "粢饭", "面馆", "北京小吃", "上海小吃", "杭州小吃",
            "24小时餐厅", "亲子餐厅", "家常菜", "粥店", "素菜馆", "社区小馆",
            "商务餐厅", "小吃快餐",
        ) if term in compact]
        return " ".join(cuisines[:2]) or "餐厅"
    if "交通" in category:
        return "交通枢纽"
    return compact or "景点"
