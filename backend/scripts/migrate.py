"""Explicit migration job. Application startup only checks the schema."""

import asyncio
import sys

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.config import get_settings
from app.db.connection import close_pool, run_migrations


async def main() -> None:
    try:
        await run_migrations()
        dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
        async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
            # Checkpointer DDL belongs to this explicit migration job, never
            # to application startup.
            await checkpointer.setup()
    finally:
        await close_pool()


def run() -> None:
    """Run with an event loop supported by psycopg on every platform."""
    if sys.platform == "win32":
        # psycopg async connections reject Windows' default Proactor loop.
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(main())
        return
    asyncio.run(main())


if __name__ == "__main__":
    run()
