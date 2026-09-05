"""Small runtime for the text itinerary experience; legacy entrypoint is retained."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.agents import graph as agent_graph
from app.api import (
    auth,
    chat,
    memory_share_v3,
    optimize,
    places_persist,
    room,
    tasks,
    trip_understandings_v3,
    user_profile,
    weather,
)
from app.api.rate_limit import _redis_allowed
from app.config import get_settings
from app.db import connection
from app.schemas.api import ExperienceOptimizeResponse, OptimizeRequest
from app.trip_understanding.access_log import install_trip_understanding_access_log_filter
from app.trip_understanding.map_worker import (
    MapRenderWorker,
    build_configured_renderer,
    build_configured_stay_engine,
)
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.worker import TripUnderstandingWorker, build_configured_full_pipeline
from app.utils.auth import get_optional_user

logger = logging.getLogger(__name__)
install_trip_understanding_access_log_filter()
PRIVATE_HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
}


def _message(code: int, message: str, *, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=code, content={"detail": {"message": message}}, headers={**PRIVATE_HEADERS, **(headers or {})})


def _subset(router: APIRouter, allowed: set[tuple[str, str]]) -> APIRouter:
    selected = APIRouter()
    for route in router.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", set()) or set()
        if path and methods and all((method, path) in allowed for method in methods):
            selected.routes.append(route)
    return selected


async def _work_loop(worker, stop: asyncio.Event, poll_seconds: float) -> None:
    worker_id = f"experience-{uuid4().hex}"
    while not stop.is_set():
        try:
            processed = await worker.run_once(worker_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Provider/database exception text can contain private input or credentials.
            logger.warning("background operation unavailable")
            processed = False
        if not processed:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                pass


async def _maintain(repository, stop: asyncio.Event, interval: float) -> None:
    while not stop.is_set():
        try:
            observed_at = datetime.now(timezone.utc)
            await repository.purge_expired_private_data(now=observed_at, limit=100)
            await repository.expire_retained_trips(now=observed_at, limit=100)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("private data maintenance unavailable")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    cache = Redis.from_url(cfg.redis_url, socket_connect_timeout=2, socket_timeout=2)
    tasks: list[asyncio.Task] = []
    pipeline = None
    graph_initialized = False
    stop = asyncio.Event()
    app.state.ready = False
    app.state.cache = cache
    app.state.worker_tasks = tasks
    try:
        await connection.get_pool()
        if cfg.require_schema_check:
            await connection.check_schema_version()
        await cache.ping()
        await agent_graph.init_persistent_graph()
        graph_initialized = True
        if cfg.experience_workers_enabled:
            repository = PostgresTripUnderstandingRepository()
            pipeline = build_configured_full_pipeline(cfg)
            understanding = TripUnderstandingWorker(
                repository, full_pipeline=pipeline,
                lease_seconds=cfg.trip_understanding_job_lease_seconds,
            )
            maps = MapRenderWorker(
                repository, renderer=build_configured_renderer(cfg),
                stay_engine=build_configured_stay_engine(cfg),
                demo_source_routing=True,
                lease_seconds=cfg.map_render_job_lease_seconds,
            )
            tasks.extend([
                asyncio.create_task(_work_loop(understanding, stop, cfg.trip_understanding_worker_poll_seconds)),
                asyncio.create_task(_work_loop(maps, stop, cfg.map_render_worker_poll_seconds)),
                asyncio.create_task(_maintain(repository, stop, cfg.experience_maintenance_seconds)),
            ])
        app.state.ready = True
        yield
    finally:
        app.state.ready = False
        stop.set()
        # Cancellation preserves dispatched-call uncertainty through the existing leases.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if pipeline is not None:
            await pipeline.aclose()
        if graph_initialized:
            await agent_graph.close_checkpointer()
        await cache.aclose()
        await connection.close_pool()


def create_app() -> FastAPI:
    application = FastAPI(
        title="行程查", version="1.0.0", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    application.add_middleware(
        CORSMiddleware, allow_origin_regex=get_settings().cors_origin_regex,
        allow_credentials=True, allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "If-Match", "Idempotency-Key", "Last-Event-ID"],
        expose_headers=["ETag", "Retry-After"],
    )
    application.include_router(trip_understandings_v3.router, prefix="/api")
    application.include_router(trip_understandings_v3.account_router, prefix="/api")
    application.include_router(_subset(memory_share_v3.router, {
        ("GET", "/v3/me/data-consents"),
        ("PUT", "/v3/me/data-consents/{purpose}"),
        ("GET", "/v3/me/travel-preferences"),
        ("PUT", "/v3/me/travel-preferences"),
        ("DELETE", "/v3/me/travel-preferences"),
    }), prefix="/api")
    application.include_router(_subset(auth.router, {
        ("POST", "/auth/email-register"),
        ("POST", "/auth/email-login"),
    }), prefix="/api")
    application.include_router(_subset(user_profile.router, {
        ("GET", "/user/me"),
        ("PUT", "/user/profile"),
        ("GET", "/user/rooms"),
    }), prefix="/api")
    application.include_router(_subset(room.router, {
        ("POST", "/room"),
        ("POST", "/room/{room_id}/join"),
        ("GET", "/room/{room_id}/state"),
        ("GET", "/room/{room_id}/members"),
        ("POST", "/room/{room_id}/ws-token"),
    }), prefix="/api")
    application.include_router(_subset(chat.router, {( "POST", "/chat")}), prefix="/api")
    @application.post("/api/optimize", response_model=ExperienceOptimizeResponse)
    async def optimize_for_experience(
        request: OptimizeRequest,
        current_user: str | None = Depends(get_optional_user),
    ) -> ExperienceOptimizeResponse:
        try:
            result = await optimize.optimize(request, current_user)
        except HTTPException as exc:
            public_messages = {
                400: "请先选择要排线的地点",
                401: "请先登录",
                403: "你无法使用这个协同房间",
                409: "还需要补充或确认关键信息",
                422: "当前地点暂时无法排成可用路线，请调整后重试",
            }
            raise HTTPException(
                status_code=exc.status_code,
                detail={
                    "message": public_messages.get(
                        exc.status_code,
                        "路线暂不可用，请稍后重试",
                    )
                },
            ) from None
        return ExperienceOptimizeResponse(
            itinerary=result.itinerary,
            backup_pool=result.backup_pool,
        )

    application.include_router(_subset(tasks.router, {( "POST", "/room/{room_id}/task/parse")}), prefix="/api")
    application.include_router(_subset(weather.router, {( "GET", "/weather")}), prefix="/api")
    application.include_router(_subset(places_persist.router, {
        ("GET", "/room/{room_id}/places"),
        ("POST", "/room/{room_id}/places/sync"),
        ("GET", "/room/{room_id}/itinerary"),
        ("POST", "/room/{room_id}/itinerary"),
    }), prefix="/api")

    @application.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return _message(422, "请检查填写内容后重试")

    @application.exception_handler(Exception)
    async def unavailable(_request: Request, _exc: Exception):
        logger.warning("request unavailable")
        return _message(503, "服务暂时不可用，请稍后重试")

    @application.middleware("http")
    async def privacy_and_limits(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            cfg = get_settings()
            origin = request.headers.get("origin")
            if origin:
                import re
                if not re.fullmatch(cfg.cors_origin_regex, origin):
                    return _message(403, "请从行程页面重试")
            address = request.client.host if request.client else "unknown"
            fingerprint = hmac.new(cfg.jwt_secret_key.encode(), address.encode(), hashlib.sha256).hexdigest()
            try:
                allowed = await _redis_allowed(
                    request.app.state.cache, f"experience:write:{fingerprint}",
                    cfg.experience_write_requests_per_minute, int(time.time() * 1000),
                )
            except Exception:
                return _message(503, "服务暂时不可用，请稍后重试")
            if not allowed:
                return _message(429, "操作较频繁，请稍后重试", headers={"Retry-After": "60"})
        response = await call_next(request)
        response.headers.update(PRIVATE_HEADERS)
        return response

    @application.get("/health")
    async def health(request: Request):
        try:
            if not request.app.state.ready or any(task.done() for task in request.app.state.worker_tasks):
                raise RuntimeError
            pool = await connection.get_pool()
            await pool.fetchval("SELECT 1")
            await request.app.state.cache.ping()
        except Exception:
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ok"}

    return application


app = create_app()
