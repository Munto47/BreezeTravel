"""Small supervised task registry for non-request writes such as Memory."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


_tasks: set[asyncio.Task] = set()


def schedule(coro_factory: Callable[[], Awaitable[None]], *, timeout_seconds: float = 10.0) -> None:
    async def runner():
        try:
            async with asyncio.timeout(timeout_seconds):
                await coro_factory()
        except (TimeoutError, Exception):
            # The caller records domain outcomes; background failures never
            # masquerade as successful writes or fail the completed response.
            return

    task = asyncio.create_task(runner(), name="breezetravel-controlled-background")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def shutdown(timeout_seconds: float = 2.0) -> None:
    if not _tasks:
        return
    done, pending = await asyncio.wait(_tasks, timeout=timeout_seconds)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def active_count() -> int:
    return len(_tasks)
