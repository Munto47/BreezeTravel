import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, optimize, room, recommend, weather, evidence, tasks, memories
from app.api import audits, imports, repairs, suggestions, templates, trip_workspaces, members
from app.api import auth as auth_api
from app.api import e2e as e2e_api
from app.api import user_profile
from app.api import places_persist
from app.api import cities
from app.api import themes
from app.api import edit as edit_api
from app.config import get_settings, settings
from app.db import connection as db_connection
from app.agents import graph as agent_graph
from app import metrics as _m
from app.observability.metrics import metrics as prometheus_metrics
from app.suggestions.frozen_snapshot import (
    suggestion_provider_health,
    validate_suggestion_provider_configuration,
)

# ── LangSmith 可观测性（Sprint 5）─────────────────────────────────────────
# 在任何 LangChain/LangGraph 对象创建之前设置环境变量，
# 确保追踪 SDK 能正确拦截所有调用链路
_langsmith_enabled = bool(settings.langsmith_api_key and settings.runtime_profile != "test")
if _langsmith_enabled:
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
    print(f"[Observability] LangSmith 追踪已启用，项目：{settings.langsmith_project}")
else:
    # 测试进程即使从本地 .env 读到 Key 也禁止外发 trace。
    os.environ["LANGCHAIN_TRACING_V2"] = "false"

# 指标存储统一在 app.metrics 模块管理，此处仅保留 startup_time 写入

# A deployment ``INSTANCE_ID`` identifies a replica and therefore commonly
# stays stable across process restarts.  The restart gate needs a different,
# process-scoped witness: these values are created once at module import and
# cannot be supplied by a request or inherited from a previous process.
_BOOT_GENERATION_ID = str(uuid.uuid4())
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()
_PROCESS_ID = os.getpid()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────
    _m.set_val("startup_time", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    cfg = get_settings()
    # Validate the byte-exact frozen Suggestion artifact before opening any
    # service dependency.  Each Suggestion request validates it again, so a
    # post-startup file replacement also fails closed.
    validate_suggestion_provider_configuration(cfg)
    await db_connection.get_pool()
    if cfg.auto_migrate:
        await db_connection.run_migrations()
    elif cfg.require_schema_check and not (cfg.demo_mode or cfg.runtime_profile == "test"):
        await db_connection.check_schema_version()
    if cfg.checkpoint_bootstrap_on_start:
        await agent_graph.init_persistent_graph()
    yield
    # ── shutdown ─────────────────────────────────────────────────────────
    from app.services.background_tasks import shutdown as shutdown_background_tasks
    await shutdown_background_tasks()
    await agent_graph.close_checkpointer()
    await db_connection.close_pool()


app = FastAPI(
    title="BreezeTravel — AI 智能旅行协同规划系统",
    description=(
        "基于 LangGraph ReAct + Critic + Advanced RAG + MCP Server 的旅行规划系统。\n\n"
        "Sprint 5 新增：Critic 反思节点 / LangSmith 追踪 / MCP Server / 本地显式验证"
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 业务路由 ──────────────────────────────────────────────────────────────
app.include_router(chat.router, prefix="/api", tags=["chat"])
app.include_router(optimize.router, prefix="/api", tags=["optimize"])
app.include_router(themes.router, prefix="/api", tags=["themes"])
app.include_router(edit_api.router, prefix="/api", tags=["edit"])
# user_profile 必须在 room 之前注册，否则 /user/{user_id} 会抢先匹配 /user/rooms 等路径
app.include_router(user_profile.router, prefix="/api", tags=["user"])
app.include_router(room.router, prefix="/api", tags=["room"])
app.include_router(recommend.router, prefix="/api", tags=["recommend"])
app.include_router(weather.router, prefix="/api", tags=["weather"])
app.include_router(auth_api.router, prefix="/api", tags=["auth"])
app.include_router(places_persist.router, prefix="/api", tags=["places"])
app.include_router(cities.router, prefix="/api", tags=["cities"])
app.include_router(evidence.router, prefix="/api", tags=["evidence"])
app.include_router(e2e_api.router, prefix="/api", tags=["e2e"])
app.include_router(tasks.router, prefix="/api", tags=["tasks"])
app.include_router(memories.router, prefix="/api", tags=["memory"])
app.include_router(trip_workspaces.router, prefix="/api", tags=["trip-workspaces"])
app.include_router(audits.router, prefix="/api", tags=["audits"])
app.include_router(imports.router, prefix="/api", tags=["imports"])
app.include_router(repairs.router, prefix="/api", tags=["repairs"])
app.include_router(members.router, prefix="/api", tags=["members"])
app.include_router(templates.router, prefix="/api", tags=["route-templates"])
app.include_router(suggestions.router, prefix="/api", tags=["suggestions"])


# ── 运维端点 ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health_check():
    """
    健康检查端点（Docker healthcheck / ECS 存活探针）

    返回 200 即表示服务正常，可处理请求。
    """
    return {
        "status": "ok",
        "suggestion_provider": suggestion_provider_health(get_settings()),
        "version": app.version,
        "service": "breezetravel-backend",
        "instance_id": os.getenv("INSTANCE_ID", "single"),
        "boot_generation": {
            "instance_id": _BOOT_GENERATION_ID,
            "started_at": _PROCESS_STARTED_AT,
            "pid": _PROCESS_ID,
        },
        "runtime_profile": settings.runtime_profile,
        "demo_mode": settings.demo_mode,
        "amap_mock": settings.amap_mock,
        "amap_configured": bool(settings.amap_api_key),
    }


@app.get("/metrics", tags=["ops"])
async def metrics():
    """
    业务指标端点（轻量版，可对接 Prometheus 抓取）

    请求级指标：
    - total_chat_requests     : 累计 /api/chat 请求次数
    - total_optimize_requests : 累计 /api/optimize 请求次数

    Agent 级指标（Phase 2 新增）：
    - agent_success_rate      : 有效地点输出率（synthesized_places ≥ 1）
    - critic_trigger_rate     : Critic 反思触发率（低质结果重检索比例）
    - avg_react_iterations    : 平均 ReAct 循环次数
    - tool_call_distribution  : 各工具调用次数分布
    """
    m = _m.snapshot()
    total_agent = m["agent_success_count"] + m["agent_failure_count"]

    cfg = get_settings()
    return {
        # ── 请求级 ────────────────────────────────────────────────
        "total_chat_requests": m["total_chat_requests"],
        "total_optimize_requests": m["total_optimize_requests"],
        # ── Agent 级 ──────────────────────────────────────────────
        "agent_success_count": m["agent_success_count"],
        "agent_failure_count": m["agent_failure_count"],
        "agent_success_rate": round(
            m["agent_success_count"] / total_agent, 4
        ) if total_agent else None,
        "critic_trigger_count": m["critic_trigger_count"],
        "critic_trigger_rate": round(
            m["critic_trigger_count"] / total_agent, 4
        ) if total_agent else None,
        "avg_react_iterations": round(
            m["total_react_iterations"] / total_agent, 2
        ) if total_agent else None,
        "tool_call_distribution": {
            "total": m["tool_calls_total"],
            "search_places": m["tool_calls_amap"],
            "search_travel_notes": m["tool_calls_rag"],
            "get_weather": m["tool_calls_weather"],
        },
        "reliability": {
            "agent_degraded_count": m["agent_degraded_count"],
            "rag_empty_count": m["rag_empty_count"],
            "tool_error_count": m["tool_error_count"],
            "sse_disconnect_count": m["sse_disconnect_count"],
        },
        "model_usage": m["labelled"].get("model_usage", {}),
        "model_calls": m["labelled"].get("model_calls", {}),
        "tool_outcomes": m["labelled"].get("tool_outcomes", {}),
        "error_categories": m["labelled"].get("error_categories", {}),
        "estimated_llm_cost_usd": round(m["estimated_llm_cost_usd"], 8),
        # ── 系统信息 ──────────────────────────────────────────────
        "langsmith_enabled": _langsmith_enabled,
        "langsmith_project": settings.langsmith_project if _langsmith_enabled else None,
        "demo_mode": settings.demo_mode,
        "amap_mock": settings.amap_mock,
        "suggestion_provider": suggestion_provider_health(cfg),
        "startup_time": m["startup_time"],
        "version": app.version,
    }


@app.get("/metrics/prometheus", response_class=PlainTextResponse, tags=["ops"])
async def prometheus_metrics_endpoint():
    return PlainTextResponse(prometheus_metrics.render(), media_type="text/plain; version=0.0.4")


# ── 请求计数中间件 ─────────────────────────────────────────────────────────
@app.middleware("http")
async def count_requests(request, call_next):
    """轻量中间件：统计核心路由请求次数，用于 /metrics 端点"""
    response = await call_next(request)
    path = request.url.path
    if path == "/api/chat":
        _m.inc("total_chat_requests")
    elif path == "/api/optimize":
        _m.inc("total_optimize_requests")
    return response
