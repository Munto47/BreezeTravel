from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from contextvars import ContextVar
from time import monotonic

from app.observability.logging import log_event


current_trace_id: ContextVar[str] = ContextVar("current_trace_id", default="untraced")


def room_hash(room_id: str | None) -> str | None:
    return hashlib.sha256(room_id.encode("utf-8")).hexdigest()[:12] if room_id else None


@asynccontextmanager
async def traced_operation(logger, event: str, trace_id: str, **safe_fields):
    token = current_trace_id.set(trace_id)
    started = monotonic()
    try:
        yield
        log_event(logger, event, trace_id=trace_id, duration_ms=int((monotonic() - started) * 1000), status="ok", **safe_fields)
    except Exception as exc:
        log_event(logger, event, trace_id=trace_id, duration_ms=int((monotonic() - started) * 1000), error_category=type(exc).__name__, status="error", **safe_fields)
        raise
    finally:
        current_trace_id.reset(token)
