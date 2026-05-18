"""
游记攻略检索工具（RAG）

封装了 rag_retrieval 节点的 Advanced RAG Pipeline 为 LangChain @tool。
ReAct Agent 通过 LLM tool calling 调用此工具。

工具输入：query（搜索内容）、city（城市）
工具输出：JSON 字符串（游记片段摘要），供 LLM 读取推理；
         同时缓存完整 chunk 对象，供 tool_executor 节点写入 AgentState.rag_chunks。
"""

import json
from typing import Annotated

from langchain_core.tools import tool

_rag_results_cache: list = []


def get_cached_rag_results() -> list:
    """获取最近一次 search_travel_notes 调用的完整 chunk 列表"""
    return _rag_results_cache.copy()


def clear_rag_cache():
    """清空缓存"""
    _rag_results_cache.clear()


@tool
async def search_travel_notes(
    query: Annotated[str, "搜索内容，如'成都带老人注意事项''锦里避坑攻略'"],
    city: Annotated[str, "目的地城市，如'成都''北京'"],
) -> str:
    """
    检索真实旅行者的游记、攻略和避坑经验（RAG 语义检索）。

    当用户询问：
    - 主观体验（哪里人少、适合什么人、当地人推荐）
    - 具体避坑经验（怎么排队、什么时候去、注意什么）
    - 口碑和感受（值不值得去、好不好玩）
    时，调用此工具。

    返回：相关游记片段，包含真实旅行者的体验和建议，JSON 格式。
    """
    from app.agents.nodes import rag_retrieval as rag_node
    from app.agents.state import AgentState
    from langchain_core.messages import HumanMessage

    mock_state: AgentState = {
        "messages": [HumanMessage(content=f"在{city}，{query}")],
        "thread_id": "tool-call",
        "user_id": "tool-call",
        "trip_city": city,
        "intent": "rag",
        "query_rewrite": query,
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
        result = await rag_node.run(mock_state)
        chunks = result.get("rag_chunks", [])

        # 缓存完整 chunk 对象
        global _rag_results_cache
        _rag_results_cache = chunks

        if not chunks:
            return json.dumps({"status": "no_results", "message": "暂无相关游记，建议直接用高德搜索"}, ensure_ascii=False)

        # 返回给 LLM 的摘要（截取前 200 字）
        snippets = [
            {
                "excerpt": c["content"][:200],
                "relevance": round(c.get("similarity", 0), 3),
                "sources": c.get("retrieval_sources", []),
            }
            for c in chunks[:5]
        ]
        return json.dumps(
            {"status": "ok", "count": len(chunks), "snippets": snippets},
            ensure_ascii=False, indent=2,
        )

    except Exception as exc:
        _rag_results_cache = []
        return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)
