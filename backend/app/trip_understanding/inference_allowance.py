"""Task-local dispatch guard; the repository owns the durable allowance."""
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Awaitable, Callable
import time


class InferenceAllowanceExceeded(TimeoutError):
    """No request was sent. A provider may preserve its validated partial reply."""


_reservation: ContextVar[Callable[[], Awaitable[float | None]] | None] = ContextVar(
    "trip_inference_reservation", default=None,
)
_call_deadline: ContextVar[float | None] = ContextVar("trip_inference_call_deadline", default=None)


@contextmanager
def inference_allowance(reserve: Callable[[], Awaitable[float | None]]):
    token = _reservation.set(reserve)
    deadline_token = _call_deadline.set(None)
    try:
        yield
    finally:
        _reservation.reset(token)
        _call_deadline.reset(deadline_token)


async def reserve_model_call() -> None:
    reserve = _reservation.get()
    if reserve is not None:
        remaining = await reserve()
        _call_deadline.set(time.monotonic() + remaining if remaining is not None else None)


def remaining_call_seconds() -> float | None:
    deadline = _call_deadline.get()
    return max(0, deadline - time.monotonic()) if deadline is not None else None
