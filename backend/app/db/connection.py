"""
asyncpg 连接池初始化，含 pgvector 自动注册 + 迁移自动执行。
"""

import asyncpg
import logging
import os
from pgvector.asyncpg import register_vector
from app.config import get_settings

logger = logging.getLogger(__name__)

_pool = None


async def get_pool() -> asyncpg.Pool:
    """获取全局连接池（懒初始化）"""
    global _pool
    if _pool is None:
        # asyncpg 使用 postgresql:// 格式（不带 +asyncpg）
        settings = get_settings()
        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        # init=register_vector 使所有连接自动支持 pgvector 类型，无需每次手动注册
        _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10, init=register_vector)
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def run_migrations():
    """
    按文件名序号顺序执行 db/migrations/*.sql，跳过已记录的迁移。
    使用简单的 applied_migrations 表追踪执行状态。
    """
    pool = await get_pool()
    migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
    if not os.path.isdir(migrations_dir):
        return

    async with pool.acquire() as conn:
        # 创建迁移记录表（如果不存在）
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applied_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )

        applied = {r["filename"] for r in await conn.fetch("SELECT filename FROM applied_migrations")}

        sql_files = sorted(
            f for f in os.listdir(migrations_dir) if f.endswith(".sql")
        )

        for filename in sql_files:
            if filename in applied:
                continue
            filepath = os.path.join(migrations_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                sql = f.read()
            try:
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO applied_migrations (filename) VALUES ($1)", filename
                )
                logger.info(f"[Migration] Applied: {filename}")
            except Exception:
                logger.exception("[Migration] Failed to apply %s", filename)
                raise


async def check_schema_version(required_migration: str | None = None) -> None:
    """Fail startup unless every migration shipped in this image is applied."""
    required = required_migration or get_settings().required_migration
    migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
    expected = sorted(
        filename
        for filename in os.listdir(migrations_dir)
        if filename.endswith(".sql")
    )
    if required not in expected:
        raise RuntimeError(f"required migration is absent from image: {required}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('public.applied_migrations') IS NOT NULL")
        if not exists:
            raise RuntimeError("database schema is uninitialised; run python -m scripts.migrate")
        applied = {
            row["filename"]
            for row in await conn.fetch(
                "SELECT filename FROM applied_migrations WHERE filename = ANY($1::text[])",
                expected,
            )
        }
    missing = [filename for filename in expected if filename not in applied]
    if missing:
        raise RuntimeError(
            "database schema is behind; missing migrations " + ", ".join(missing)
        )
