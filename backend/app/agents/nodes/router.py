"""
ReAct Agent 节点（原 Router 升级版）

架构升级：规则分类 → LLM Native Tool Calling
─────────────────────────────────────────────────────────────────
旧版：Router 用 LLM 输出 {"intent": "amap"/"rag"/"both"} JSON
      然后图按 intent 硬编码路由到对应节点

新版：ReAct Agent 让 LLM 直接通过 tool calling 选择调用哪些工具
      - Think：LLM 分析用户意图
      - Act  ：LLM 输出 tool_calls（search_places / search_travel_notes / get_weather）
      - Observe：tool_executor 节点执行工具，结果以 ToolMessage 返回
      - 下一轮：LLM 观察工具结果，决定是否再调用其他工具或直接结束

优势
----
1. 不再依赖硬编码路由规则，LLM 自主推理
2. 支持多工具串联调用（先搜景点 + 再查天气 + 再查游记攻略）
3. ReAct 链路在前端 ThinkingSteps 可视化展示
4. 面试展示 LLM native function calling 能力

防无限循环
----------
react_iterations 字段记录循环次数，超过 MAX_ITERATIONS 强制进入 synthesizer
"""

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from app.agents.state import AgentState
from app.config import settings
from app.memory.working import extract_from_messages, format_for_prompt
from app.tools import ALL_TOOLS

MAX_REACT_ITERATIONS = 3  # 最多 3 轮工具调用（防止无限循环）

_REACT_SYSTEM = """你是一个专业的旅行规划助手，帮助用户发现适合的旅行地点。

你拥有以下工具：
- search_places      : 搜索高德地图 POI（景点/餐厅/住宿/娱乐）
- search_travel_notes: 检索真实游记攻略和避坑经验（RAG 语义搜索）
- get_weather        : 查询目的地天气预报

工作原则：
1. 根据用户问题，选择调用一个或多个工具
2. 客观属性（找景点/餐厅/评分）→ 优先 search_places
3. 主观体验（避坑/攻略/适合什么人）→ 优先 search_travel_notes
4. 综合推荐（口碑好的景点）→ 同时调用两个工具
5. 询问天气/出行安排 → 包含 get_weather
6. 工具返回结果后，如果信息已足够则不要重复调用

{working_memory}

{long_term_prefs}

目的地城市：{city}"""


def _get_llm_with_tools():
    """获取绑定了工具的 LLM"""
    api_key = settings.effective_llm_api_key
    if not api_key:
        return None

    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(
        model=settings.llm_model_router,
        api_key=api_key,
        base_url=settings.effective_llm_api_url,
        max_tokens=500,
        temperature=0,
    )
    return llm.bind_tools(ALL_TOOLS)


async def run(state: AgentState) -> dict:
    """
    ReAct Agent 节点入口

    LLM 通过 tool calling 选择调用哪些工具。
    如果 LLM 输出 tool_calls → 图路由到 tool_executor
    如果 LLM 无 tool_calls → 图路由到 synthesizer
    """
    messages = state.get("messages", [])
    trip_city = state.get("trip_city") or "成都"
    iterations = state.get("react_iterations", 0)

    # 超出最大循环次数，强制结束（通过不输出 tool_calls 让图路由到 synthesizer）
    if iterations >= MAX_REACT_ITERATIONS:
        print(f"[ReActAgent] 已达最大迭代次数 {MAX_REACT_ITERATIONS}，进入 Synthesizer")
        return {"react_iterations": iterations}

    # Demo 模式：直接给出 intent（向后兼容）
    if settings.demo_mode:
        return {"intent": "amap", "query_rewrite": _get_last_human_query(messages)}

    llm_with_tools = _get_llm_with_tools()
    if llm_with_tools is None:
        print("[ReActAgent] 无 API Key，回退到默认 amap 搜索")
        return {
            "intent": "amap",
            "query_rewrite": _get_last_human_query(messages),
            "react_iterations": iterations,
        }

    # ── 更新工作记忆 ─────────────────────────────────────────────────
    current_working_ctx = state.get("working_context")
    updated_ctx = extract_from_messages(messages, current_working_ctx)

    # ── 构建注入了记忆的 System Prompt ───────────────────────────────
    working_mem_text = format_for_prompt(updated_ctx)
    long_term_text = state.get("user_long_term_prefs") or ""

    system_content = _REACT_SYSTEM.format(
        working_memory=working_mem_text if working_mem_text else "（本次对话暂无提取到偏好）",
        long_term_prefs=long_term_text if long_term_text else "（该用户暂无历史偏好记录）",
        city=trip_city,
    )

    # ── 调用 LLM（ReAct Think 步骤） ─────────────────────────────────
    try:
        # 构造消息：system + 历史消息（过滤掉 system 消息避免重复）
        invoke_messages = [SystemMessage(content=system_content)] + [
            m for m in messages
            if not isinstance(m, SystemMessage)
        ]

        response: AIMessage = await llm_with_tools.ainvoke(invoke_messages)

        tool_names = [tc["name"] for tc in (response.tool_calls or [])]
        if tool_names:
            print(f"[ReActAgent] iteration={iterations + 1}, 调用工具: {tool_names}")
        else:
            print(f"[ReActAgent] iteration={iterations + 1}, 无工具调用，进入 Synthesizer")

        return {
            "messages": [response],
            "working_context": updated_ctx,
            "react_iterations": iterations + 1,
            # 向后兼容：如果 LLM 没有 tool_calls，保留上一次的 query_rewrite
            "query_rewrite": state.get("query_rewrite") or _get_last_human_query(messages),
        }

    except Exception as exc:
        print(f"[ReActAgent] LLM 调用失败，回退到默认搜索：{exc}")
        return {
            "intent": "amap",
            "query_rewrite": _get_last_human_query(messages),
            "react_iterations": iterations,
            "working_context": updated_ctx,
        }


def _get_last_human_query(messages: list) -> str:
    """提取最后一条用户消息文本"""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return str(m.content)
    return "旅游景点推荐"
