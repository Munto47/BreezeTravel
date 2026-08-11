"""Policy-gated tool runtime with deadlines, retries, isolation and receipts."""

from __future__ import annotations

import asyncio
import random
import time
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings


class ToolErrorCategory(str, Enum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "provider_429"
    PROVIDER_5XX = "provider_5xx"
    INVALID_PAYLOAD = "invalid_payload"
    EMPTY_RESULT = "empty_result"
    UNAUTHORIZED = "unauthorized"
    CIRCUIT_OPEN = "circuit_open"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


class SearchPlacesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=200)
    city: str = Field(min_length=1, max_length=80)
    district: str = Field(default="", max_length=80)
    category: str = Field(default="", max_length=80)
    prefer_trending: bool = False
    prefer_chain: bool = False


class SearchTravelNotesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=300)
    city: str = Field(min_length=1, max_length=80)


class WeatherArgs(BaseModel):
    model_config = ConfigDict(extra="allow")
    city: str = Field(min_length=1, max_length=80)


TOOL_SCHEMAS = {
    "search_places": SearchPlacesArgs,
    "search_travel_notes": SearchTravelNotesArgs,
    "get_weather": WeatherArgs,
}
TOOL_SCOPES = {
    "search_places": {"room:read", "poi:read"},
    "search_travel_notes": {"room:read", "rag:read"},
    "get_weather": {"room:read", "weather:read"},
}
TOOL_PROVIDER = {"search_places": "amap", "search_travel_notes": "embedding", "get_weather": "weather"}


class ToolCallEnvelope(BaseModel):
    call_id: str = Field(default_factory=lambda: str(uuid4()))
    trace_id: str
    room_id: Optional[str] = None
    actor_user_id: str
    tool: str
    arguments: dict[str, Any]
    authorization_scope: set[str]
    deadline_monotonic: float
    idempotency_key: str


class ToolReceipt(BaseModel):
    call_id: str
    trace_id: str
    tool: str
    status: Literal["ok", "degraded", "error", "cancelled"]
    duration_ms: int
    result_count: int = 0
    attempts: int = 1
    error_category: Optional[ToolErrorCategory] = None
    degraded: bool = False
    injection_signal: bool = False


class ToolRuntimeError(RuntimeError):
    def __init__(self, message: str, receipt: ToolReceipt):
        super().__init__(message)
        self.receipt = receipt


class _Circuit:
    def __init__(self):
        self.failures = 0
        self.opened_at = 0.0


class ProviderRuntime:
    def __init__(self):
        cfg = get_settings()
        self._semaphores = {
            "llm": asyncio.Semaphore(cfg.llm_max_concurrency),
            "amap": asyncio.Semaphore(cfg.amap_max_concurrency),
            "weather": asyncio.Semaphore(cfg.weather_max_concurrency),
            "embedding": asyncio.Semaphore(cfg.embedding_max_concurrency),
        }
        self._circuits = {name: _Circuit() for name in self._semaphores}

    def _allow(self, provider: str) -> bool:
        circuit = self._circuits[provider]
        cfg = get_settings()
        if circuit.failures < cfg.provider_failure_threshold:
            return True
        if time.monotonic() - circuit.opened_at >= cfg.provider_circuit_open_seconds:
            circuit.failures = 0
            return True
        return False

    def _record(self, provider: str, ok: bool) -> None:
        circuit = self._circuits[provider]
        if ok:
            circuit.failures = 0
            return
        circuit.failures += 1
        if circuit.failures == get_settings().provider_failure_threshold:
            circuit.opened_at = time.monotonic()

    async def execute(
        self,
        envelope: ToolCallEnvelope,
        operation: Callable[[dict[str, Any]], Awaitable[Any]],
    ) -> tuple[Any, ToolReceipt]:
        started = time.monotonic()
        if envelope.tool not in TOOL_SCHEMAS:
            receipt = _receipt(envelope, started, "error", ToolErrorCategory.INVALID_PAYLOAD)
            raise ToolRuntimeError("tool is not allowlisted", receipt)
        provider = TOOL_PROVIDER[envelope.tool]
        if not TOOL_SCOPES[envelope.tool].issubset(envelope.authorization_scope):
            receipt = _receipt(envelope, started, "error", ToolErrorCategory.UNAUTHORIZED)
            raise ToolRuntimeError("tool scope rejected", receipt)
        try:
            parsed = TOOL_SCHEMAS[envelope.tool].model_validate(envelope.arguments).model_dump()
        except Exception as exc:
            receipt = _receipt(envelope, started, "error", ToolErrorCategory.INVALID_PAYLOAD)
            raise ToolRuntimeError("tool arguments rejected", receipt) from exc
        if not self._allow(provider):
            receipt = _receipt(envelope, started, "error", ToolErrorCategory.CIRCUIT_OPEN)
            raise ToolRuntimeError("provider circuit is open", receipt)

        attempts = 0
        last_error: Optional[BaseException] = None
        async with self._semaphores[provider]:
            while attempts < 2:
                attempts += 1
                remaining = envelope.deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    last_error = TimeoutError("request deadline exhausted")
                    break
                try:
                    result = await asyncio.wait_for(operation(parsed), timeout=remaining)
                    self._record(provider, True)
                    count = len(result[1]) + len(result[2]) if isinstance(result, tuple) and len(result) == 3 else 1
                    return result, ToolReceipt(
                        call_id=envelope.call_id,
                        trace_id=envelope.trace_id,
                        tool=envelope.tool,
                        status="ok" if count else "degraded",
                        duration_ms=int((time.monotonic() - started) * 1000),
                        result_count=count,
                        attempts=attempts,
                        error_category=ToolErrorCategory.EMPTY_RESULT if not count else None,
                        degraded=not bool(count),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    category = classify_error(exc)
                    if category not in {ToolErrorCategory.TIMEOUT, ToolErrorCategory.RATE_LIMITED, ToolErrorCategory.PROVIDER_5XX} or attempts >= 2:
                        break
                    remaining = envelope.deadline_monotonic - time.monotonic()
                    if remaining <= 0.1:
                        break
                    await asyncio.sleep(min(0.25 * (2 ** (attempts - 1)) + random.uniform(0, 0.05), remaining - 0.05))

        self._record(provider, False)
        category = classify_error(last_error or RuntimeError("tool failed"))
        receipt = _receipt(envelope, started, "error", category, attempts)
        raise ToolRuntimeError("tool provider failed", receipt) from last_error


def _receipt(envelope, started, status, category, attempts=1):
    return ToolReceipt(
        call_id=envelope.call_id,
        trace_id=envelope.trace_id,
        tool=envelope.tool,
        status=status,
        duration_ms=int((time.monotonic() - started) * 1000),
        attempts=attempts,
        error_category=category,
        degraded=status != "ok",
    )


def classify_error(exc: BaseException) -> ToolErrorCategory:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ToolErrorCategory.TIMEOUT
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status == 429:
        return ToolErrorCategory.RATE_LIMITED
    if isinstance(status, int) and status >= 500:
        return ToolErrorCategory.PROVIDER_5XX
    return ToolErrorCategory.INTERNAL


_runtime: Optional[ProviderRuntime] = None


def get_provider_runtime() -> ProviderRuntime:
    global _runtime
    if _runtime is None:
        _runtime = ProviderRuntime()
    return _runtime


def reset_provider_runtime() -> None:
    global _runtime
    _runtime = None


def enforce_tool_budget(tool_calls: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return accepted and explicitly rejected calls for request accounting."""
    safe_limit = max(0, int(limit))
    return tool_calls[:safe_limit], tool_calls[safe_limit:]
