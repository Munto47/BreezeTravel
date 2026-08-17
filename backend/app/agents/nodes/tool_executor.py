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
多轮 ReAct 循环中，工具结果累积（不覆盖），地点按 canonical POI、文档按
(note_id, chunk_idx) 去重。
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from langchain_core.messages import ToolMessage, AIMessage

from app.agents.state import AgentState
from app.config import settings
from app.tools.runtime import (
    TOOL_SCOPES, ToolCallEnvelope, ToolRuntimeError, enforce_tool_budget,
    get_provider_runtime,
)
from app import metrics as _metrics
from app.observability.metrics import metrics as _prom_metrics
from app.memory.governance import contains_injection_signal


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
    # A request has a finite work budget even when the model emits duplicate
    # calls.  The graph retains successful siblings if one tool times out.
    tool_calls, rejected_calls = enforce_tool_budget(tool_calls, settings.chat_max_tool_calls)
    tasks = [_execute_with_runtime(tc, state) for tc in tool_calls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # ── 整合结果 ────────────────────────────────────────────────────
    tool_messages: list[ToolMessage] = []
    amap_places_new: list = []
    rag_chunks_new: list = []
    citations_new: list[dict] = []
    failures_new: list[dict] = []
    receipts_new: list[dict] = []
    retrieval_audits_new: list[dict] = []
    retrieval_snapshots_new: list[dict] = []

    for rejected in rejected_calls:
        failures_new.append({"tool": rejected.get("name", "unknown"), "reason": "tool_budget_exceeded"})
        _prom_metrics.inc(
            "agent_tool_failure_total",
            tool=rejected.get("name", "unknown"),
            error_category="tool_budget_exceeded",
        )

    for tc, result in zip(tool_calls, results):
        if isinstance(result, Exception):
            failure_receipt = None
            tool_output = json.dumps(
                {"status": "error", "message": "工具暂时不可用，请基于已获取信息继续回答"}, ensure_ascii=False
            )
            print(f"[ToolExecutor] 工具 {tc['name']} 执行异常：{result}")
            if isinstance(result, ToolRuntimeError):
                failure_receipt = result.receipt.model_dump(mode="json")
                receipts_new.append(failure_receipt)
                reason = result.receipt.error_category.value if result.receipt.error_category else "tool_exception"
                _prom_metrics.inc("agent_tool_failure_total", tool=tc["name"], error_category=reason)
                _prom_metrics.observe("agent_tool_duration_seconds", result.receipt.duration_ms / 1000, tool=tc["name"], status="error")
            else:
                reason = "tool_timeout" if isinstance(result, TimeoutError) else "tool_exception"
            failures_new.append({"tool": tc["name"], "reason": reason})
            failure_audit = _retrieval_audit_from_exception(result, tc, state)
            if failure_audit:
                retrieval_audits_new.append(failure_audit)
            if tc.get("name") == "search_places":
                retrieval_snapshots_new.append(_retrieval_snapshot(
                    tc, [], [failure_audit] if failure_audit else [],
                    failure_receipt,
                ))
            _metrics.observe("tool_outcomes", f"{tc['name']}:{reason}")
            _metrics.observe("error_categories", reason)
        else:
            (tool_output, extra_places, extra_chunks, extra_audits), receipt = result
            if any(contains_injection_signal(str(chunk.get("content", ""))) for chunk in extra_chunks):
                receipt.injection_signal = True
            receipts_new.append(receipt.model_dump(mode="json"))
            _prom_metrics.observe("agent_tool_duration_seconds", receipt.duration_ms / 1000, tool=tc["name"], status=receipt.status)
            amap_places_new.extend(extra_places)
            rag_chunks_new.extend(extra_chunks)
            citations_new.extend(_citations_from_chunks(extra_chunks))
            retrieval_audits_new.extend(extra_audits)
            if tc.get("name") == "search_places":
                retrieval_snapshots_new.append(_retrieval_snapshot(
                    tc, extra_places, extra_audits, receipt.model_dump(mode="json"),
                ))
            outcome = (
                receipt.error_category.value
                if receipt.degraded and receipt.error_category
                else "ok"
            )
            _metrics.observe("tool_outcomes", f"{tc['name']}:{outcome}")

        tool_messages.append(ToolMessage(
            content=tool_output,
            tool_call_id=tc["id"],
            name=tc["name"],
        ))

    # ── 累积状态（去重合并，不覆盖历史结果） ─────────────────────────
    existing_places = list(state.get("amap_places", []))
    existing_chunks = list(state.get("rag_chunks", []))
    existing_citations = list(state.get("citations", []))
    existing_failures = list(state.get("tool_failures", []))
    existing_receipts = list(state.get("tool_receipts", []))
    existing_retrieval_audits = list(state.get("retrieval_audits", []))
    existing_retrieval_snapshots = list(state.get("retrieval_snapshots", []))

    merged_places = _merge_unique_places(existing_places, amap_places_new)
    latest_user_request = next(
        (
            str(getattr(message, "content", ""))
            for message in reversed(state.get("messages", []))
            if getattr(message, "type", "") in {"human", "user"}
        ),
        "",
    )
    eligible_places = merged_places
    coverage = dict(state.get("slot_coverage", {}))
    bound_recommendation_plan = state.get("recommendation_plan")
    if state.get("recommendation_plan"):
        from app.constraints.candidate_selection import select_eligible_places
        from app.constraints.geo_routes import enrich_geo_route_evidence
        from app.constraints.location import extract_explicit_district_from_messages
        from app.constraints.recommendation_plan import bind_geo_anchor_evidence, slot_coverage
        from app.constraints.selection_policy import select_evidence_eligible_candidates

        district = (
            state.get("trip_district")
            or extract_explicit_district_from_messages(state.get("messages", []))
            or ""
        )
        all_retrieval_audits = existing_retrieval_audits + retrieval_audits_new
        bound_recommendation_plan = bind_geo_anchor_evidence(
            state["recommendation_plan"], all_retrieval_audits,
        )
        eligible_places = select_eligible_places(
            merged_places,
            latest_user_request,
            district,
            bound_recommendation_plan,
        )
        eligible_places = await enrich_geo_route_evidence(
            eligible_places,
            bound_recommendation_plan,
            all_retrieval_audits,
        )
        eligible_places = select_evidence_eligible_candidates(eligible_places)
        coverage = slot_coverage(bound_recommendation_plan, eligible_places)

    existing_chunk_keys = {(c["note_id"], c.get("chunk_idx", 0)) for c in existing_chunks}
    merged_chunks = existing_chunks + [
        c for c in rag_chunks_new
        if (c["note_id"], c.get("chunk_idx", 0)) not in existing_chunk_keys
    ]
    existing_source_ids = {c["source_id"] for c in existing_citations}
    merged_citations = existing_citations + [
        c for c in citations_new if c["source_id"] not in existing_source_ids
    ]

    print(
        f"[ToolExecutor] 完成：+{len(amap_places_new)} 地点, +{len(rag_chunks_new)} chunks | "
        f"累计：{len(merged_places)} 地点, {len(merged_chunks)} chunks"
    )

    return {
        "messages": tool_messages,
        "amap_places": merged_places,
        "eligible_amap_places": eligible_places,
        "eligible_candidates_computed": True,
        "rag_chunks": merged_chunks,
        "citations": merged_citations,
        "tool_failures": existing_failures + failures_new,
        "tool_receipts": existing_receipts + receipts_new,
        "retrieval_audits": existing_retrieval_audits + retrieval_audits_new,
        "retrieval_snapshots": existing_retrieval_snapshots + retrieval_snapshots_new,
        "recommendation_plan": (
            bound_recommendation_plan.model_dump(mode="json")
            if hasattr(bound_recommendation_plan, "model_dump")
            else bound_recommendation_plan
        ),
        "slot_coverage": coverage,
    }


def _merge_unique_places(existing: list, incoming: list) -> list:
    """Stable entity-level dedupe across prior state and parallel searches."""
    from app.constraints.place_identity import deduplicate_places

    return deduplicate_places([*existing, *incoming])


def _retrieval_audit_from_exception(
    exc: BaseException,
    tool_call: dict | None = None,
    state: AgentState | None = None,
) -> dict | None:
    """Recover a provider receipt through ToolRuntimeError exception chaining."""
    receipt = getattr(exc, "receipt", None)
    error_category = getattr(getattr(receipt, "error_category", None), "value", None)
    provider_health_failure = error_category in {"timeout", "provider_429", "provider_5xx"}
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        audit = getattr(current, "audit", None)
        if audit is not None:
            value = audit.model_dump(mode="json") if hasattr(audit, "model_dump") else dict(audit)
            value["error_category"] = error_category or value.get("error_category")
            value["provider_health_failure"] = provider_health_failure
            value.setdefault("attempted", True)
            return value
        current = current.__cause__ or current.__context__
    if not tool_call or tool_call.get("name") != "search_places":
        return None
    args = dict(tool_call.get("args") or {})
    runtime_state = state or {}
    attempted = error_category not in {"circuit_open", "invalid_payload", "unauthorized"}
    return {
        "slot_id": args.get("slot_id") or None,
        "query": str(args.get("query") or ""),
        "city": str(args.get("city") or runtime_state.get("trip_city") or ""),
        "district": args.get("district") or runtime_state.get("trip_district") or None,
        "location": None,
        "radius_m": int(args.get("radius_m") or 0) or None,
        "typecodes": list(args.get("typecodes") or []),
        "provider": "amap",
        "execution_mode": "fixture" if settings.amap_mock or settings.demo_mode else "live",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "response_hash": None,
        "result_count": 0,
        "fallback_reason": error_category or "tool_exception",
        "status": "blocked" if error_category == "circuit_open" else "error",
        "attempted": attempted,
        "error_category": error_category or "tool_exception",
        "provider_health_failure": provider_health_failure,
    }


def _retrieval_snapshot(
    tool_call: dict,
    places: list,
    audits: list[dict],
    receipt: dict | None,
) -> dict:
    """Freeze one provider call without generated descriptions or judge data."""
    args = dict(tool_call.get("args") or {})
    return {
        "tool_call_id": str(tool_call.get("id") or ""),
        "request": {
            key: args.get(key)
            for key in (
                "query", "city", "district", "category", "slot_id",
                "anchor_place", "radius_m", "typecodes",
            )
            if args.get(key) not in (None, "", [], 0)
        },
        "places": [
            place.model_dump(mode="json") if hasattr(place, "model_dump") else dict(place)
            for place in places
        ],
        "audits": [audit for audit in audits if audit],
        "receipt": receipt,
    }


async def _execute_with_runtime(tool_call: dict, state: AgentState):
    name = tool_call.get("name", "")
    if name not in TOOL_SCOPES:
        raise ValueError(f"未知工具：{name}")
    args = dict(tool_call.get("args", {}))
    if not args.get("city"):
        args["city"] = state.get("trip_city") or "成都"
    tool_timeout = (
        settings.amap_tool_timeout_seconds
        if name == "search_places"
        else settings.tool_timeout_seconds
    )
    deadline = min(
        state.get("deadline_monotonic") or (time.monotonic() + tool_timeout),
        time.monotonic() + tool_timeout,
    )
    envelope = ToolCallEnvelope(
        call_id=str(tool_call.get("id") or ""),
        trace_id=state.get("trace_id") or "untraced",
        room_id=state.get("room_id"),
        actor_user_id=state.get("user_id") or "anonymous",
        tool=name,
        arguments=args,
        authorization_scope=TOOL_SCOPES[name],
        deadline_monotonic=deadline,
        idempotency_key=f"{state.get('trace_id', 'untraced')}:{tool_call.get('id', '')}",
    )
    return await get_provider_runtime().execute(
        envelope,
        lambda validated_args: _execute_tool_call({**tool_call, "args": validated_args}, state),
    )


def _citations_from_chunks(chunks: list[dict]) -> list[dict]:
    """Keep source metadata out of prompts, but make it available to the UI."""
    return [
        {
            "source_id": f"{chunk['note_id']}:{chunk.get('chunk_idx', 0)}",
            "title": chunk.get("title") or chunk["note_id"],
            "url": chunk.get("source_url"),
            "excerpt": (chunk.get("content") or "")[:320],
            "score": float(chunk.get("rerank_score") or chunk.get("rrf_score") or 0.0),
            "retrieval_sources": chunk.get("retrieval_sources", ["dense"]),
            "published_at": chunk.get("source_published_at"),
            "retrieved_at": chunk.get("source_retrieved_at"),
            "license": chunk.get("source_license"),
            "revision": chunk.get("source_revision"),
            "attribution": chunk.get("source_attribution"),
            "corpus_kind": chunk.get("corpus_kind") or "synthetic",
        }
        for chunk in chunks
    ]


async def _execute_tool_call(
    tool_call: dict,
    state: AgentState,
) -> tuple[str, list, list, list[dict]]:
    """
    执行单个工具调用，返回 (tool_output_str, places, chunks, retrieval_audits)

    直接调用底层函数（不经过 @tool 包装器），每次调用结果独立，无全局状态共享。
    """
    name = tool_call["name"]
    args = dict(tool_call.get("args", {}))
    trip_city = state.get("trip_city") or "成都"
    from app.constraints.location import extract_explicit_district_from_messages
    trip_district = state.get("trip_district") or extract_explicit_district_from_messages(state.get("messages", []))

    # LLM 未传 city 时自动填充 trip_city
    if not args.get("city"):
        args["city"] = trip_city

    # ── search_places → 调用高德底层函数 ────────────────────────────
    if name == "search_places":
        from app.tools.amap_tool import _run_amap_search_with_audit
        from app.constraints.recommendation_intent import requested_category_argument

        original_query = ""
        for message in reversed(state.get("messages", [])):
            if getattr(message, "type", "") in {"human", "user"}:
                original_query = str(getattr(message, "content", ""))
                break
        hard_category = requested_category_argument(original_query)
        slot_category = str(args.get("category") or "") if args.get("slot_id") else ""

        places, retrieval_audits = await _run_amap_search_with_audit(
            query=args.get("query", ""),
            city=args["city"],
            district=args.get("district") or trip_district or "",
            category=slot_category or hard_category or args.get("category", ""),
            prefer_trending=bool(args.get("prefer_trending", False)),
            prefer_chain=bool(args.get("prefer_chain", False)),
            slot_id=str(args.get("slot_id") or ""),
            anchor_place=str(args.get("anchor_place") or ""),
            radius_m=int(args.get("radius_m") or 0),
            typecodes=list(args.get("typecodes") or []),
        )
        slot_id = str(args.get("slot_id") or "")
        if slot_id and state.get("recommendation_plan"):
            from app.schemas.recommendation_plan import RecommendationPlan

            plan = RecommendationPlan.model_validate(state["recommendation_plan"])
            slot = next((item for item in plan.slots if item.slot_id == slot_id), None)
            if slot and slot.provider_match_aliases:
                compact_aliases = {
                    "".join(alias.lower().split()) for alias in slot.provider_match_aliases
                }
                exact = [
                    place for place in places
                    if "".join(place.name.lower().split()) in compact_aliases
                ]
                if exact:
                    places = exact[:slot.min_results]
                else:
                    contains = [
                        place for place in places
                        if any(
                            alias in "".join(place.name.lower().split())
                            or "".join(place.name.lower().split()) in alias
                            for alias in compact_aliases
                        )
                    ]
                    contains.sort(key=lambda place: len("".join(place.name.split())))
                    places = contains[:slot.min_results]
            if slot and slot.entity_name:
                aliases = list(dict.fromkeys([slot.entity_name, *slot.entity_aliases]))
                compact_aliases = {"".join(alias.lower().split()) for alias in aliases}
                exact = [
                    place for place in places
                    if "".join(place.name.lower().split()) in compact_aliases
                ]
                if exact:
                    places = exact[:slot.min_results]
                else:
                    contains = [
                        place for place in places
                        if any(
                            alias in "".join(place.name.lower().split())
                            or "".join(place.name.lower().split()) in alias
                            for alias in compact_aliases
                        )
                    ]
                    contains.sort(key=lambda place: len("".join(place.name.split())))
                    places = contains[:slot.min_results]
                places = [
                    place.model_copy(update={
                        "canonical_entity_names": list(dict.fromkeys([
                            *place.canonical_entity_names, slot.entity_name,
                        ])),
                    })
                    for place in places
                ]

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
        return tool_output, places, [], retrieval_audits

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
        return tool_output, [], chunks, []

    # ── get_weather → 调用天气工具 ───────────────────────────────────
    elif name == "get_weather":
        from app.tools.weather_tool import get_weather

        result = await get_weather.ainvoke(args)
        return result, [], [], []

    # ── 未知工具 ─────────────────────────────────────────────────────
    else:
        output = json.dumps(
            {"status": "error", "message": f"未知工具：{name}"},
            ensure_ascii=False,
        )
        return output, [], [], []
