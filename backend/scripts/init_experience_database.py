"""Explicit, additive initialization for a local/CI experience database."""
import asyncio
from pathlib import Path

import asyncpg

from app.config import get_settings
from app.db.connection import close_pool, run_migrations


async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    connection = await asyncpg.connect(dsn)
    try:
        if not await connection.fetchval("SELECT to_regclass('public.users') IS NOT NULL"):
            await connection.execute((Path(__file__).parents[1] / "app/db/init.sql").read_text(encoding="utf-8"))
    finally:
        await connection.close()
    try:
        await run_migrations()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
