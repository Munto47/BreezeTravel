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
    ANCHOR_NOT_FOUND = "anchor_not_found"
    CONFIGURATION_ERROR = "configuration_error"
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
    slot_id: str = Field(default="", max_length=80)
    anchor_place: str = Field(default="", max_length=120)
    radius_m: int = Field(default=0, ge=0, le=50000)
    typecodes: list[str] = Field(default_factory=list, max_length=8)


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
    provider: Optional[str] = None
    circuit_state: Literal["closed", "open", "half_open"] = "closed"
    circuit_failure_count: int = 0
    half_open_probe: bool = False


class ToolRuntimeError(RuntimeError):
    def __init__(self, message: str, receipt: ToolReceipt):
        super().__init__(message)
        self.receipt = receipt


class _Circuit:
    def __init__(self):
        self.failures = 0
        self.opened_at = 0.0
        self.half_open_in_flight = False
        self.lock = asyncio.Lock()


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

    async def _acquire_circuit_permission(
        self, provider: str,
    ) -> tuple[bool, bool, Literal["closed", "open", "half_open"], int]:
        circuit = self._circuits[provider]
        cfg = get_settings()
        async with circuit.lock:
            if circuit.failures < cfg.provider_failure_threshold:
                return True, False, "closed", circuit.failures
            if time.monotonic() - circuit.opened_at < cfg.provider_circuit_open_seconds:
                return False, False, "open", circuit.failures
            if circuit.half_open_in_flight:
                return False, False, "half_open", circuit.failures
            circuit.half_open_in_flight = True
            return True, True, "half_open", circuit.failures

    async def _record_outcome(
        self,
        provider: str,
        category: ToolErrorCategory | None,
    ) -> tuple[Literal["closed", "open"], int]:
        """Record only actual provider-health failures.

        Empty results, invalid business arguments, unresolved anchors and local
        application errors must never open a shared provider circuit. A
        non-health response also breaks a run of consecutive provider failures.
        """
        circuit = self._circuits[provider]
        threshold = get_settings().provider_failure_threshold
        async with circuit.lock:
            circuit.half_open_in_flight = False
            if category not in _PROVIDER_HEALTH_FAILURES:
                circuit.failures = 0
                circuit.opened_at = 0.0
                return "closed", 0
            circuit.failures += 1
            if circuit.failures >= threshold:
                circuit.failures = threshold
                circuit.opened_at = time.monotonic()
                return "open", circuit.failures
            return "closed", circuit.failures

    async def _release_half_open_probe(self, provider: str) -> None:
        circuit = self._circuits[provider]
        async with circuit.lock:
            circuit.half_open_in_flight = False

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
        allowed, half_open_probe, circuit_state, circuit_failures = (
            await self._acquire_circuit_permission(provider)
        )
        if not allowed:
            receipt = _receipt(
                envelope, started, "error", ToolErrorCategory.CIRCUIT_OPEN,
                provider=provider, circuit_state=circuit_state,
                circuit_failure_count=circuit_failures,
            )
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
                    circuit_state, circuit_failures = await self._record_outcome(provider, None)
                    count = len(result[1]) + len(result[2]) if isinstance(result, tuple) and len(result) >= 3 else 1
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
                        provider=provider,
                        circuit_state=circuit_state,
                        circuit_failure_count=circuit_failures,
                        half_open_probe=half_open_probe,
                    )
                except asyncio.CancelledError:
                    await self._release_half_open_probe(provider)
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

        category = classify_error(last_error or RuntimeError("tool failed"))
        circuit_state, circuit_failures = await self._record_outcome(provider, category)
        receipt = _receipt(
            envelope, started, "error", category, attempts,
            provider=provider, circuit_state=circuit_state,
            circuit_failure_count=circuit_failures, half_open_probe=half_open_probe,
        )
        raise ToolRuntimeError("tool provider failed", receipt) from last_error


def _receipt(
    envelope,
    started,
    status,
    category,
    attempts=1,
    *,
    provider=None,
    circuit_state="closed",
    circuit_failure_count=0,
    half_open_probe=False,
):
    return ToolReceipt(
        call_id=envelope.call_id,
        trace_id=envelope.trace_id,
        tool=envelope.tool,
        status=status,
        duration_ms=int((time.monotonic() - started) * 1000),
        attempts=attempts,
        error_category=category,
        degraded=status != "ok",
        provider=provider,
        circuit_state=circuit_state,
        circuit_failure_count=circuit_failure_count,
        half_open_probe=half_open_probe,
    )


def classify_error(exc: BaseException) -> ToolErrorCategory:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (asyncio.TimeoutError, TimeoutError)):
            return ToolErrorCategory.TIMEOUT
        status = getattr(current, "status", None) or getattr(current, "status_code", None)
        if status == 429:
            return ToolErrorCategory.RATE_LIMITED
        if isinstance(status, int) and status >= 500:
            return ToolErrorCategory.PROVIDER_5XX
        audit = getattr(current, "audit", None)
        if audit is not None:
            audit_status = (
                audit.get("status") if isinstance(audit, dict)
                else getattr(audit, "status", None)
            )
            fallback = (
                audit.get("fallback_reason") if isinstance(audit, dict)
                else getattr(audit, "fallback_reason", None)
            )
            if fallback == "anchor_not_found":
                return ToolErrorCategory.ANCHOR_NOT_FOUND
            if audit_status == "empty":
                return ToolErrorCategory.EMPTY_RESULT
            if audit_status == "configuration_error":
                return ToolErrorCategory.CONFIGURATION_ERROR
        current = current.__cause__ or current.__context__
    return ToolErrorCategory.INTERNAL


_PROVIDER_HEALTH_FAILURES = {
    ToolErrorCategory.TIMEOUT,
    ToolErrorCategory.RATE_LIMITED,
    ToolErrorCategory.PROVIDER_5XX,
}


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
