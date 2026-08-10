"""
游记攻略检索工具（RAG）

封装为 LangChain @tool，供 ReAct Agent 通过 LLM tool calling 触发。

设计说明
--------
@tool 函数只负责两件事：
  1. 向 LLM 暴露清晰的函数签名，便于 LLM 决策何时调用
  2. 返回人类可读的 JSON 字符串（LLM Observe 阶段读取推理）

实际执行逻辑（获取完整 chunk 对象）由 tool_executor.py 直接调用 _run_rag_search()，
不再经过 @tool 包装器，彻底避免了模块级缓存带来的并发竞态条件。
"""

from typing import Annotated

from langchain_core.tools import tool


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

    返回相关游记片段，JSON 格式。
    注意：完整检索结果由系统内部处理，此处返回摘要供 LLM 推理。
    """
    # 仅用于 LLM schema 暴露，实际数据由 tool_executor 调用 _run_rag_search() 获取
    return f'{{"status": "ok", "query": "{query}", "city": "{city}"}}'


async def _run_rag_search(query: str, city: str) -> list:
    """
    底层 RAG 检索执行函数（供 tool_executor 直接调用，无竞态风险）

    执行完整 Advanced RAG Pipeline：
    HyDE 扩展 → 混合检索（Dense+Sparse）→ RRF 融合 → Reranker 精排

    Args:
        query : 搜索内容
        city  : 目的地城市

    Returns:
        chunk 字典列表
    """
    from app.agents.nodes import rag_retrieval as rag_node
    from app.agents.state import AgentState, default_working_context
    from langchain_core.messages import HumanMessage

    mock_state: AgentState = {
        "messages": [HumanMessage(content=f"在{city}，{query}")],
        "thread_id": "tool-call",
        "user_id": "tool-call",
        "trip_city": city,
        "intent": "rag",
        "query_rewrite": query,
        "routing_signals": [],
        "amap_places": [],
        "rag_chunks": [],
        "citations": [],
        "tool_failures": [],
        "synthesized_places": [],
        "final_response": None,
        "itinerary": None,
        "selected_place_ids": [],
        "working_context": default_working_context(),
        "user_long_term_prefs": None,
        "react_iterations": 0,
    }

    try:
        result = await rag_node.run(mock_state)
        return result.get("rag_chunks", [])
    except Exception as exc:
        print(f"[RagTool] 检索失败：{exc}")
        return []
