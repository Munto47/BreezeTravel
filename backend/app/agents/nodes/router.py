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
4. 充分利用 LLM 原生 function calling 能力，无需自实现路由层

防无限循环
----------
react_iterations 字段记录循环次数，超过 MAX_ITERATIONS 强制进入 synthesizer
"""

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from app.agents.state import AgentState
from app.config import settings
from app.memory.working import extract_from_messages, format_for_prompt
from app.tools import ALL_TOOLS
from app.agents.routing_policy import plan_simple_tools, plan_tools
from app import metrics as _metrics
from app.constraints.location import extract_district_from_messages

MAX_REACT_ITERATIONS = 3  # 最多 3 轮工具调用（防止无限循环）

_REACT_SYSTEM = """你是一个专业的旅行规划助手，帮助用户发现适合的旅行地点。

你拥有以下工具：
- search_places      : 搜索高德地图 POI（景点/餐厅/住宿/娱乐），返回结构化地点数据（place_id/坐标/评分）
- search_travel_notes: 检索真实游记攻略和避坑经验（RAG 语义搜索），只返回文字描述，无结构化地点数据
- get_weather        : 查询目的地天气预报

⚠️ 核心规则——必须严格遵守：
【规则 A】只要用户询问推荐地点（美食/景点/酒店/住宿/娱乐/打卡），必须调用 search_places。
  - "有哪些好吃的" "推荐景点" "必吃美食" "哪里好玩" "住哪里" → 全部必须调用 search_places
  - search_travel_notes 只提供文字描述，前端地图卡片依赖 search_places 的结构化数据
  - 不调 search_places = 用户看到空列表，绝对不允许
【规则 B】有攻略/避坑/口碑等主观需求时，在调用 search_places 的同时也调用 search_travel_notes。
【规则 C】纯天气/行程安排问题 → 调用 get_weather；但如果用户同时问地点，仍需调用 search_places。
【规则 D】工具返回结果后，信息已足够则不要重复调用同一工具。

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

    # The first deterministic search plus one model-guided expansion normally
    # provides enough grounded options.  Continuing to a third round after we
    # already have category diversity adds latency and can starve Synthesizer
    # under the request-wide deadline.
    if iterations >= 2 and _has_sufficient_place_evidence(state.get("amap_places", [])):
        print(
            f"[ReActAgent] 已有 {len(state.get('amap_places', []))} 个多品类地点，"
            "停止重复检索并进入 Synthesizer"
        )
        return {"react_iterations": iterations}

    # 超出最大循环次数，强制结束（通过不输出 tool_calls 让图路由到 synthesizer）
    if iterations >= MAX_REACT_ITERATIONS:
        print(f"[ReActAgent] 已达最大迭代次数 {MAX_REACT_ITERATIONS}，进入 Synthesizer")
        return {"react_iterations": iterations}

    # Demo 模式：直接给出 intent（向后兼容）
    if settings.demo_mode:
        return {"intent": "amap", "query_rewrite": _get_last_human_query(messages)}

    # P0 mixed-intent guard.  Make the minimum complete tool plan explicit
    # before the optional local classifier or LLM gets a chance to omit one.
    last_query = _get_last_human_query(messages)
    trip_district = state.get("trip_district") or extract_district_from_messages(messages)
    forced_plan = plan_tools(last_query) if iterations == 0 else None
    if forced_plan is None and iterations == 0 and settings.deterministic_routing_enabled:
        forced_plan = plan_simple_tools(last_query)
    if forced_plan:
        tool_calls = []
        for index, name in enumerate(forced_plan.tools, start=1):
            args = {"query": last_query, "city": trip_city}
            if name == "search_places" and trip_district:
                args["district"] = trip_district
            tool_calls.append({"name": name, "args": args, "id": f"policy-{index}"})
        print(f"[ReActAgent] deterministic tool policy: {forced_plan.signals}")
        return {
            "messages": [AIMessage(content="", tool_calls=tool_calls)], "intent": forced_plan.intent,
            "query_rewrite": last_query, "routing_signals": list(forced_plan.signals),
            "react_iterations": iterations + 1,
        }

    # Sprint 3 — 微调分类器 fast path
    # 本地 LoRA 模型快速判断意图，命中则跳过 DeepSeek tool calling
    # 降级：模型未加载 / 推理失败 → 继续走 ReAct 路径（透明 fallback）
    if settings.ft_router_enabled and iterations == 0:
        from app.agents.nodes.router_classifier import classify
        ft_result = classify(last_query, trip_city, settings.ft_router_model_path)
        if ft_result is not None:
            print(f"[ReActAgent] FT Router 命中: intent={ft_result['intent']}")
            intent_tools = {
                "amap": ("search_places",),
                "rag": ("search_travel_notes",),
                "both": ("search_places", "search_travel_notes"),
                "weather": ("get_weather",),
            }
            tools = intent_tools.get(ft_result["intent"])
            if tools:
                # The graph routes by actual tool_calls.  Returning an intent
                # alone used to skip tool_executor entirely for the local FT
                # fast path, producing plausible but ungrounded answers.
                return {
                    "messages": [AIMessage(content="", tool_calls=[
                        {"name": name, "args": {"query": last_query, "city": trip_city}, "id": f"ft-{index}"}
                        for index, name in enumerate(tools, start=1)
                    ])],
                    "intent": ft_result["intent"],
                    "query_rewrite": ft_result["query_rewrite"] or last_query,
                    "react_iterations": iterations + 1,
                }
            return {
                "intent": ft_result["intent"],
                "query_rewrite": ft_result["query_rewrite"] or last_query,
                "react_iterations": iterations + 1,
            }

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
        usage = getattr(response, "usage_metadata", None) or {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        _metrics.observe("model_usage", f"{settings.llm_model_router}:input_tokens", input_tokens)
        _metrics.observe("model_usage", f"{settings.llm_model_router}:output_tokens", output_tokens)
        estimated = (input_tokens * settings.router_input_cost_per_million + output_tokens * settings.router_output_cost_per_million) / 1_000_000
        _metrics.inc("estimated_llm_cost_usd", estimated)

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


def _has_sufficient_place_evidence(places: list) -> bool:
    """Return true once retrieval has enough unique, category-diverse POIs."""
    unique_ids: set[str] = set()
    categories: set[str] = set()
    for place in places:
        if isinstance(place, dict):
            place_id = place.get("place_id")
            category = place.get("category")
        else:
            place_id = getattr(place, "place_id", None)
            category = getattr(place, "category", None)
        if place_id:
            unique_ids.add(str(place_id))
        if category is not None:
            categories.add(str(getattr(category, "value", category)))
    return len(unique_ids) >= 8 and len(categories) >= 2


def _get_last_human_query(messages: list) -> str:
    """提取最后一条用户消息文本"""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return str(m.content)
    return "旅游景点推荐"
