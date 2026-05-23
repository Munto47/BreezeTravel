import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, optimize, room, recommend, weather
from app.api import auth as auth_api
from app.api import user_profile
from app.api import places_persist
from app.config import settings
from app.db.connection import get_pool, close_pool, run_migrations
from app.agents import graph as agent_graph
from app import metrics as _m

# ── LangSmith 可观测性（Sprint 5）─────────────────────────────────────────
# 在任何 LangChain/LangGraph 对象创建之前设置环境变量，
# 确保追踪 SDK 能正确拦截所有调用链路
if settings.langsmith_api_key:
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
    print(f"[Observability] LangSmith 追踪已启用，项目：{settings.langsmith_project}")
else:
    # 未配置时确保不意外开启追踪（避免环境变量污染）
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")

# 指标存储统一在 app.metrics 模块管理，此处仅保留 startup_time 写入


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────
    _m.set_val("startup_time", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    await get_pool()                          # 预热 asyncpg 连接池
    await run_migrations()                    # 自动执行待执行的迁移文件
    await agent_graph.init_persistent_graph() # 初始化持久化图（建 checkpoint 表）
    yield
    # ── shutdown ─────────────────────────────────────────────────────────
    await agent_graph.close_checkpointer()
    await close_pool()


app = FastAPI(
    title="BreezeTravel — AI 智能旅行协同规划系统",
    description=(
        "基于 LangGraph ReAct + Critic + Advanced RAG + MCP Server 的旅行规划系统。\n\n"
        "Sprint 5 新增：Critic 反思节点 / LangSmith 追踪 / MCP Server / GitHub Actions CI"
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
# user_profile 必须在 room 之前注册，否则 /user/{user_id} 会抢先匹配 /user/rooms 等路径
app.include_router(user_profile.router, prefix="/api", tags=["user"])
app.include_router(room.router, prefix="/api", tags=["room"])
app.include_router(recommend.router, prefix="/api", tags=["recommend"])
app.include_router(weather.router, prefix="/api", tags=["weather"])
app.include_router(auth_api.router, prefix="/api", tags=["auth"])
app.include_router(places_persist.router, prefix="/api", tags=["places"])


# ── 运维端点 ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health_check():
    """
    健康检查端点（Docker healthcheck / ECS 存活探针）

    返回 200 即表示服务正常，可处理请求。
    """
    return {
        "status": "ok",
        "version": app.version,
        "service": "breezetravel-backend",
    }


@app.get("/metrics", tags=["ops"])
async def metrics():
    """
    业务指标端点（轻量版，可对接 Prometheus 抓取）

    请求级指标：
    - total_chat_requests     : 累计 /api/chat 请求次数
    - total_optimize_requests : 累计 /api/optimize 请求次数

    Agent 级指标（Phase 2 新增，面试可展示）：
    - agent_success_rate      : 有效地点输出率（synthesized_places ≥ 1）
    - critic_trigger_rate     : Critic 反思触发率（低质结果重检索比例）
    - avg_react_iterations    : 平均 ReAct 循环次数
    - tool_call_distribution  : 各工具调用次数分布
    """
    m = _m.snapshot()
    total_agent = m["agent_success_count"] + m["agent_failure_count"]

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
        # ── 系统信息 ──────────────────────────────────────────────
        "langsmith_enabled": bool(settings.langsmith_api_key),
        "langsmith_project": settings.langsmith_project if settings.langsmith_api_key else None,
        "demo_mode": settings.demo_mode,
        "amap_mock": settings.amap_mock,
        "startup_time": m["startup_time"],
        "version": app.version,
    }


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
