from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from app.config import get_settings


ALLOWED_CATEGORIES = {"travel_style", "budget", "food", "pace", "accessibility", "exclusion", "general"}
_INJECTION_PATTERNS = (
    r"ignore\s+(all\s+)?previous",
    r"忽略.{0,8}(指令|规则|系统)",
    r"system\s*prompt",
    r"调用.{0,8}工具",
    r"泄露.{0,8}(提示词|密钥|token)",
)
_STABLE_SIGNALS = ("喜欢", "偏好", "不喜欢", "从不", "通常", "一直", "过敏", "素食", "预算", "慢节奏", "无障碍")
_ONE_OFF_SIGNALS = ("这次", "今天", "明天", "本次", "这趟", "当前行程")


def content_hash(content: str) -> str:
    normalised = " ".join(content.lower().split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def contains_injection_signal(content: str) -> bool:
    return any(re.search(pattern, content, re.IGNORECASE) for pattern in _INJECTION_PATTERNS)


def is_stable_preference(content: str) -> bool:
    if not content or contains_injection_signal(content):
        return False
    if any(signal in content for signal in _ONE_OFF_SIGNALS):
        return False
    return any(signal in content for signal in _STABLE_SIGNALS)


def infer_category(content: str) -> str:
    mapping = {
        "budget": ("预算", "省钱", "价格"),
        "food": ("美食", "素食", "过敏", "餐"),
        "pace": ("节奏", "早起", "晚睡", "步行"),
        "accessibility": ("无障碍", "轮椅", "老人"),
        "exclusion": ("不喜欢", "排除", "从不"),
        "travel_style": ("亲子", "文化", "自然", "打卡", "旅行风格"),
    }
    return next((category for category, words in mapping.items() if any(word in content for word in words)), "general")


async def memory_enabled(user_id: str, pool) -> bool:
    async with pool.acquire() as conn:
        value = await conn.fetchval("SELECT enabled FROM user_memory_settings WHERE user_id = $1", user_id)
    return get_settings().memory_enabled_default if value is None else bool(value)


async def list_memories(user_id: str, pool):
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT id, content, category, confidence, source_message_ids,
                   created_at, updated_at, expires_at
            FROM user_preferences
            WHERE user_id = $1 AND active = TRUE
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY updated_at DESC
            """,
            user_id,
        )


async def update_memory(user_id: str, memory_id, values: dict, pool):
    allowed = {key: value for key, value in values.items() if key in {"content", "category", "confidence", "expires_at"} and value is not None}
    if "category" in allowed and allowed["category"] not in ALLOWED_CATEGORIES:
        raise ValueError("unsupported memory category")
    if "content" in allowed:
        if contains_injection_signal(allowed["content"]):
            raise ValueError("memory content contains an instruction-like payload")
        allowed["content_hash"] = content_hash(allowed["content"])
    if not allowed:
        return None
    columns = []
    params = []
    for index, (key, value) in enumerate(allowed.items(), 1):
        columns.append(f"{key} = ${index}")
        params.append(value)
    params.extend([user_id, memory_id])
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE user_preferences SET {', '.join(columns)}, updated_at = NOW()
            WHERE user_id = ${len(params) - 1} AND id = ${len(params)} AND active = TRUE
            RETURNING id, content, category, confidence, source_message_ids, created_at, updated_at, expires_at
            """,
            *params,
        )
        if row:
            await conn.execute(
                "INSERT INTO memory_audit_log(user_id, preference_id, action, reason_code) VALUES ($1, $2, 'update', 'user_correction')",
                user_id,
                memory_id,
            )
    return row


async def delete_memory(user_id: str, memory_id, pool) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE user_preferences SET active = FALSE, updated_at = NOW() WHERE user_id = $1 AND id = $2 AND active = TRUE",
            user_id,
            memory_id,
        )
        deleted = result.endswith("1")
        if deleted:
            await conn.execute(
                "INSERT INTO memory_audit_log(user_id, preference_id, action, reason_code) VALUES ($1, $2, 'delete', 'user_request')",
                user_id,
                memory_id,
            )
    return deleted


async def set_memory_enabled(user_id: str, enabled: bool, pool):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            INSERT INTO user_memory_settings(user_id, enabled, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = NOW()
            RETURNING enabled, updated_at
            """,
            user_id,
            enabled,
        )


async def expire_memories(pool) -> int:
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE user_preferences SET active = FALSE, updated_at = NOW() WHERE active = TRUE AND expires_at <= NOW()"
        )
    return int(result.split()[-1])


def default_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=get_settings().memory_ttl_days)
