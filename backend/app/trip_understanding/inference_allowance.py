"""Task-local dispatch guard; the repository owns the durable allowance."""
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Awaitable, Callable


class InferenceAllowanceExceeded(TimeoutError):
    """No request was sent. A provider may preserve its validated partial reply."""


_reservation: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "trip_inference_reservation", default=None,
)


@contextmanager
def inference_allowance(reserve: Callable[[], Awaitable[None]]):
    token = _reservation.set(reserve)
    try:
        yield
    finally:
        _reservation.reset(token)


async def reserve_model_call() -> None:
    reserve = _reservation.get()
    if reserve is not None:
        await reserve()
