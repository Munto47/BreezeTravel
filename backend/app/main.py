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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    await get_pool()                          # 预热 asyncpg 连接池
    await run_migrations()                    # 自动执行待执行的迁移文件
    await agent_graph.init_persistent_graph() # 初始化持久化图（建 checkpoint 表）
    yield
    # shutdown
    await agent_graph.close_checkpointer()
    await close_pool()


app = FastAPI(
    title="BreezeTravel — AI 智能旅行协同规划系统",
    description="基于 LangGraph 多 Agent + Yjs 实时协同的旅行规划 MVP",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router, prefix="/api", tags=["chat"])
app.include_router(optimize.router, prefix="/api", tags=["optimize"])
app.include_router(room.router, prefix="/api", tags=["room"])
app.include_router(recommend.router, prefix="/api", tags=["recommend"])
app.include_router(weather.router, prefix="/api", tags=["weather"])
app.include_router(auth_api.router, prefix="/api", tags=["auth"])
app.include_router(user_profile.router, prefix="/api", tags=["user"])
app.include_router(places_persist.router, prefix="/api", tags=["places"])


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "agentTravel-backend"}
