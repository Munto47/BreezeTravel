"""
Long-term Memory（长期记忆）

跨会话的用户偏好持久化。

架构
----
数据库：user_preferences 表（PostgreSQL + pgvector）
  - 每条记录是一段偏好摘要文本（+ embedding）
  - 同一用户可有多条记录（按时间/主题分段存储）

读取（对话开始时）
------------------
  1. load_user_preferences(user_id) → 返回相关偏好文本
  2. 注入 ReAct Agent 的 system prompt

写入（对话结束时，异步后台）
----------------------------
  1. extract_preferences_from_conversation(messages) → LLM 提取偏好摘要
  2. embed_text(summary) → 生成向量
  3. upsert 到 user_preferences 表

降级
----
  - DB 不可用：静默跳过，不影响主流程
  - LLM 不可用：跳过偏好提取
  - 新用户（无历史记录）：返回空字符串
"""

import asyncio
import json
from typing import Optional

from app.config import settings
from app.db.connection import get_pool

# 每次加载的最大偏好条数
_MAX_PREFS_TO_LOAD = 5
# 偏好文本的最大总长度（避免注入太多 token）
_MAX_PREFS_TEXT_LEN = 500

_EXTRACT_PROMPT = """根据以下对话，提取用户的旅行偏好，用 JSON 格式输出。

对话内容：
{conversation}

请提取以下信息（没有的字段填 null）：
{{
  "preferred_cities": ["用户提到感兴趣的城市"],
  "preferred_styles": ["旅行风格，如亲子、情侣、独行"],
  "preferred_categories": ["偏好品类，如美食、文化、自然"],
  "excluded_types": ["不喜欢的类型"],
  "budget_preference": "高/中/低/null",
  "special_interests": ["特殊兴趣，如打卡拍照、夜市、古迹"],
  "summary": "一句话偏好摘要（20字以内）"
}}

只返回 JSON，不要其他内容。"""


# ── 读取接口 ─────────────────────────────────────────────────────────────────

async def load_user_preferences(user_id: str) -> str:
    """
    加载用户的长期偏好，返回格式化文本注入 system prompt。

    Args:
        user_id: 用户唯一标识

    Returns:
        偏好摘要文本；如果没有历史记录或 DB 不可用，返回空字符串
    """
    if not user_id or user_id == "anonymous":
        return ""

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT content, created_at
                FROM user_preferences
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                user_id,
                _MAX_PREFS_TO_LOAD,
            )

        if not rows:
            return ""

        # 合并多条记录
        prefs_text = "\n".join(r["content"] for r in rows)
        # 截断到最大长度
        if len(prefs_text) > _MAX_PREFS_TEXT_LEN:
            prefs_text = prefs_text[:_MAX_PREFS_TEXT_LEN] + "..."

        return f"该用户历史旅行偏好：\n{prefs_text}"

    except Exception as exc:
        print(f"[LongTermMemory] 加载偏好失败（静默跳过）：{exc}")
        return ""


# ── 写入接口 ─────────────────────────────────────────────────────────────────

async def save_conversation_preferences(
    user_id: str,
    messages: list,
    trip_city: Optional[str] = None,
) -> None:
    """
    从对话中提取偏好并异步存储（在 Synthesizer 完成后后台调用）。

    Args:
        user_id  : 用户标识
        messages : 完整对话历史
        trip_city: 目的地城市
    """
    if not user_id or user_id == "anonymous":
        return

    has_llm_key = bool(settings.effective_llm_api_key)
    if not has_llm_key:
        return

    try:
        # 提取偏好摘要
        summary = await _extract_preferences(messages, trip_city)
        if not summary:
            return

        # 生成向量
        embedding = await _embed_preference(summary)

        # 写入数据库
        await _upsert_preference(user_id, summary, embedding, trip_city)
        print(f"[LongTermMemory] 用户 {user_id[:8]}... 偏好已更新")

    except Exception as exc:
        print(f"[LongTermMemory] 保存偏好失败（静默跳过）：{exc}")


# ── 内部实现 ─────────────────────────────────────────────────────────────────

async def _extract_preferences(messages: list, trip_city: Optional[str]) -> str:
    """用 LLM 从对话中提取偏好，返回摘要文本"""
    from openai import AsyncOpenAI
    from langchain_core.messages import HumanMessage, AIMessage

    # 构建对话文本（只用 human/ai 消息，忽略 tool 消息）
    convo_lines = []
    for m in messages[-10:]:  # 只取最近 10 条
        if isinstance(m, HumanMessage):
            convo_lines.append(f"用户：{str(m.content)[:100]}")
        elif isinstance(m, AIMessage) and m.content:
            convo_lines.append(f"助手：{str(m.content)[:100]}")

    if not convo_lines:
        return ""

    convo_text = "\n".join(convo_lines)
    if trip_city:
        convo_text = f"目的地：{trip_city}\n\n" + convo_text

    client = AsyncOpenAI(
        api_key=settings.effective_llm_api_key,
        base_url=settings.effective_llm_api_url,
    )

    resp = await client.chat.completions.create(
        model=settings.llm_model_router,  # 用 router 模型（低 cost）
        messages=[{"role": "user", "content": _EXTRACT_PROMPT.format(conversation=convo_text)}],
        max_tokens=300,
        temperature=0,
    )

    raw = resp.choices[0].message.content.strip()

    # 解析 JSON
    import re
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return ""

    try:
        prefs = json.loads(m.group())
        summary = prefs.get("summary", "")
        details = []
        if prefs.get("preferred_styles"):
            details.append(f"风格：{'、'.join(prefs['preferred_styles'])}")
        if prefs.get("preferred_categories"):
            details.append(f"偏好：{'、'.join(prefs['preferred_categories'])}")
        if prefs.get("budget_preference") and prefs["budget_preference"] != "null":
            details.append(f"预算：{prefs['budget_preference']}等")
        if prefs.get("excluded_types"):
            details.append(f"排除：{'、'.join(prefs['excluded_types'])}")

        if trip_city:
            details.insert(0, f"城市：{trip_city}")

        return (summary + "；" if summary else "") + "；".join(details)

    except Exception:
        return ""


async def _embed_preference(text: str) -> list[float]:
    """将偏好文本转化为向量"""
    try:
        from app.rag.embedder import embed_text
        return await embed_text(text)
    except Exception:
        return [0.0] * 1536


async def _upsert_preference(
    user_id: str,
    content: str,
    embedding: list[float],
    trip_city: Optional[str],
) -> None:
    """插入新的偏好记录（每次对话生成一条，不删除旧记录）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_preferences (user_id, content, embedding, category)
            VALUES ($1, $2, $3::vector, $4)
            """,
            user_id,
            content,
            embedding,
            trip_city or "general",
        )
