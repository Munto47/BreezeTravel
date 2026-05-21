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

# ── 全局指标计数器（/metrics 端点用）────────────────────────────────────────
_metrics: dict = {
    "total_chat_requests": 0,
    "total_optimize_requests": 0,
    "startup_time": None,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────
    _metrics["startup_time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
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

    指标说明：
    - total_chat_requests    : 累计 /api/chat 请求次数
    - total_optimize_requests: 累计 /api/optimize 请求次数
    - langsmith_enabled      : LangSmith 追踪是否已启用
    - startup_time           : 服务启动时间（UTC）
    """
    return {
        "total_chat_requests": _metrics["total_chat_requests"],
        "total_optimize_requests": _metrics["total_optimize_requests"],
        "langsmith_enabled": bool(settings.langsmith_api_key),
        "langsmith_project": settings.langsmith_project if settings.langsmith_api_key else None,
        "demo_mode": settings.demo_mode,
        "amap_mock": settings.amap_mock,
        "startup_time": _metrics["startup_time"],
        "version": app.version,
    }


# ── 请求计数中间件 ─────────────────────────────────────────────────────────
@app.middleware("http")
async def count_requests(request, call_next):
    """轻量中间件：统计核心路由请求次数，用于 /metrics 端点"""
    response = await call_next(request)
    path = request.url.path
    if path == "/api/chat":
        _metrics["total_chat_requests"] += 1
    elif path == "/api/optimize":
        _metrics["total_optimize_requests"] += 1
    return response
