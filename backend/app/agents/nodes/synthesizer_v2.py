"""Synthesizer v2：推荐升级版（SPEC §5 / Phase B）

升级要点（相比 synthesizer.py v1）：
1. 强制 chunk_id 引用：reason / avoid_tips 必须引自 RAG context，否则留空
2. 生成结构化 PlaceRecommendation（含 alternatives 1–2 个）
3. 输出 recommendations 列表到 AgentState，供前端卡片背面渲染
4. 解析阶段过滤无效 chunk_id（Critic 负责二次验证）

Prompt 约束（SPEC §5.2）：
  "reason 和 avoid_tips 必须引自下方 RAG 上下文，每条注明 chunk_id；
   如 RAG 未命中则不出该字段，宁缺勿瞎编"
"""

import asyncio
import json
import re
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.state import AgentState
from app.config import settings
from app.memory.working import format_for_prompt
from app.schemas.place import Place, PlaceCategory, PlaceRAGMeta
from app.schemas.recommendation import Alternative, PlaceRecommendation

# ─── Prompt（v2，强制 chunk_id 引用） ─────────────────────────────────────────

SYNTHESIZER_SYSTEM_V2 = (
    "你是旅行规划助手，返回格式严格的 JSON，不要加 markdown 代码块。\n"
    "核心约束：\n"
    "1. reason 和 avoid_tips 字段必须引自下方提供的游记 RAG 上下文，"
    "每条必须在 source_chunk_ids 中注明对应的 chunk_id；\n"
    "2. 如果 RAG 上下文中没有该地点的相关内容，则 reason 留空字符串，source_chunk_ids 留空列表，宁缺勿瞎编；\n"
    "3. alternatives 提供 1–2 个同品类替代方案，说明替代理由（更便宜/排队少/更适合带娃等）；\n"
    "4. 严格遵守：只回答用户明确询问的品类/范围，不要节外生枝。"
)

SYNTHESIZER_PROMPT_V2 = """根据以下地点数据和游记摘录，生成个性化旅行推荐。

高德 POI 数据（客观）：
{amap_places_json}

游记经验摘录（主观，带 chunk_id，可能为空）：
{rag_chunks_text}

{working_memory}

任务：
1. 为每个 POI 生成 description（20-40字）和 tags（2-4个标签）
2. 若游记摘录中有该 POI 的相关内容：
   - reason：一句话说明为什么推荐，必须引自游记，在 source_chunk_ids 注明 chunk_id
   - avoid_tips：避坑提示，同上要求
   - 若无游记命中：reason 留空字符串，source_chunk_ids 留 []
3. alternatives：1–2 个同品类替代方案（place_id 可随机生成，name 真实，说明替代理由）
4. suitable_for：适合人群标签（情侣/亲子/摄影/文化等）
5. confidence：high（有游记多处命中）/ medium（有游记少量命中）/ low（无游记命中）
6. tip_snippets：从游记原文提取 1-3 条建议（供地点卡展示）
7. estimated_duration：建议游览时长（分钟），参考品类默认值
8. 生成个性化推荐说明（response_text，150字以内）

返回合法 JSON（不加 markdown 代码块）：
{{
  "response_text": "...",
  "place_updates": [
    {{
      "place_id": "...",
      "description": "...",
      "tags": ["..."],
      "reason": "...",
      "avoid_tips": ["..."],
      "source_chunk_ids": ["chunk_001"],
      "alternatives": [{{"place_id": "...", "name": "...", "why_alternative": "..."}}],
      "suitable_for": ["情侣"],
      "confidence": "medium",
      "tip_snippets": ["..."],
      "sentiment_score": 0.8,
      "estimated_duration": 120
    }}
  ]
}}
不要包含任何其他文字。"""

# ─── 常量（沿用 v1） ──────────────────────────────────────────────────────────

_PER_CATEGORY_CAP = 5
_TOTAL_CAP = 15


# ─── 解析函数（可独立测试） ───────────────────────────────────────────────────

def _parse_llm_response(
    data: dict,
    places: list[Place],
    rag_chunks: list[dict],
) -> tuple[list[Place], str, list[PlaceRecommendation]]:
    """从 LLM JSON 输出解析 places（enriched）、response_text、recommendations。

    - source_chunk_ids 中无效的 chunk_id（不在本次 rag_chunks 中）会被过滤
    - 所有 source_chunk_ids 被过滤光后，reason 置为空字符串
    - alternatives 截断到 2 个
    """
    valid_chunk_ids = {c["chunk_id"] for c in rag_chunks if "chunk_id" in c}
    updates: dict[str, dict] = {u["place_id"]: u for u in data.get("place_updates", [])}
    response_text: str = data.get("response_text", "")

    enriched: list[Place] = []
    recommendations: list[PlaceRecommendation] = []

    for place in places:
        u = updates.get(place.place_id)
        if not u:
            enriched.append(place)
            continue

        update_fields: dict = {}

        # description / tags / duration
        if u.get("description"):
            update_fields["description"] = u["description"]
        if u.get("tags"):
            update_fields["tags"] = u["tags"][:4]
        dur = u.get("estimated_duration")
        if isinstance(dur, (int, float)) and 15 <= dur <= 600:
            update_fields["estimated_duration"] = int(dur)
            update_fields["duration_basis"] = "llm"

        # tip_snippets → rag_meta
        if u.get("tip_snippets"):
            update_fields["rag_meta"] = PlaceRAGMeta(
                tip_snippets=u["tip_snippets"][:3],
                sentiment_score=float(u.get("sentiment_score", 0.0)),
                source_note_ids=[
                    c["note_id"] for c in rag_chunks
                    if place.place_id in c.get("place_ids", [])
                ],
            )

        if update_fields:
            place = place.model_copy(update=update_fields)
        enriched.append(place)

        # ── PlaceRecommendation ──────────────────────────────────────────
        raw_chunk_ids: list[str] = u.get("source_chunk_ids", [])
        valid_ids = [cid for cid in raw_chunk_ids if cid in valid_chunk_ids]

        # 无有效 chunk → reason 置空
        reason = u.get("reason", "") if valid_ids else ""
        avoid_tips = u.get("avoid_tips", []) if valid_ids else []

        # alternatives 截断到 2 个
        raw_alts = u.get("alternatives", [])[:2]
        alts = [
            Alternative(
                place_id=a.get("place_id", ""),
                name=a.get("name", ""),
                why_alternative=a.get("why_alternative", ""),
            )
            for a in raw_alts
            if isinstance(a, dict)
        ]

        rec = PlaceRecommendation(
            place_id=place.place_id,
            name=place.name,
            category_l1=u.get("category_l1", ""),
            category_l2=u.get("category_l2", ""),
            reason=reason,
            suitable_for=u.get("suitable_for", []),
            avoid_tips=avoid_tips,
            source_chunk_ids=valid_ids,
            alternatives=alts,
            confidence=u.get("confidence", "low"),
        )
        recommendations.append(rec)

    return enriched, response_text, recommendations


def _parse_llm_response_raw(
    raw: str,
    places: list[Place],
    rag_chunks: list[dict],
) -> tuple[list[Place], str, list[PlaceRecommendation]]:
    """从 LLM 原始字符串解析；失败时安全降级（返回原始 places，空推荐）"""
    raw = raw.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
    raw = raw.strip()
    try:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            data = json.loads(m.group())
            return _parse_llm_response(data, places, rag_chunks)
    except Exception as e:
        print(f"[SynthesizerV2] JSON 解析失败：{e}")
    return places, "为您找到了相关地点，请查看列表。", []


def _build_demo_response_v2(
    places: list[Place],
    city: str,
    working_ctx: Optional[dict],
    rag_chunks: list[dict],
) -> dict:
    """Demo 模式：生成包含 recommendations 的结构化响应"""
    from app.schemas.place import PlaceCategory as PC

    attractions = [p for p in places if p.category == PC.ATTRACTION]
    foods = [p for p in places if p.category == PC.FOOD]
    hotels = [p for p in places if p.category == PC.HOTEL]

    highlights = []
    if attractions:
        t = attractions[0]
        highlights.append(f"**{t.name}**{' — ' + t.description[:20] if t.description else ''}")
    if foods:
        t = foods[0]
        price = f"人均 {int(t.amap_price)} 元" if t.amap_price else "平价"
        highlights.append(f"**{t.name}**（{price}）")
    if hotels:
        highlights.append(f"住宿推荐 **{hotels[0].name}**")

    parts = [f"✨ 已为您精选 {city} 打卡地：\n"] + [f"• {h}" for h in highlights]
    cat_desc = "、".join(
        f for f, c in zip(
            [f"{len(attractions)}个景点", f"{len(foods)}道美食", f"{len(hotels)}处住宿"],
            [len(attractions), len(foods), len(hotels)],
        ) if c > 0
    )
    parts.append(f"\n共 **{len(places)}** 个地点（{cat_desc}），点击卡片「为什么推荐」查看游记原文 →")

    style = working_ctx.get("travel_style") if working_ctx else None
    if style:
        parts.append(f"\n（已根据「{style}」风格优化推荐顺序）")

    # Demo 模式生成简单 PlaceRecommendation（无 chunk 引用）
    recs: list[PlaceRecommendation] = []
    chunk_set = {c["chunk_id"] for c in rag_chunks}
    for p in places:
        matched = [c for c in rag_chunks if p.place_id in c.get("place_ids", [])]
        if matched:
            rec = PlaceRecommendation(
                place_id=p.place_id,
                name=p.name,
                reason=matched[0]["content"][:80] + "…" if len(matched[0]["content"]) > 80 else matched[0]["content"],
                source_chunk_ids=[matched[0]["chunk_id"]],
                confidence="medium",
            )
        else:
            rec = PlaceRecommendation(
                place_id=p.place_id,
                name=p.name,
                reason="",
                source_chunk_ids=[],
                confidence="low",
            )
        recs.append(rec)

    return {
        "response_text": "\n".join(parts),
        "recommendations": recs,
    }


# ─── LLM 工具 ─────────────────────────────────────────────────────────────────

def _get_llm():
    api_key = settings.effective_llm_api_key
    if not api_key:
        return None
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=settings.llm_model_synthesizer,
        api_key=api_key,
        base_url=settings.effective_llm_api_url,
        max_tokens=1800,
        temperature=0.3,
    )


# ─── 入口 ─────────────────────────────────────────────────────────────────────

async def run(state: AgentState) -> dict:
    """Synthesizer v2 节点入口"""
    from app.agents.nodes.synthesizer import (
        _cap_places,
        _extract_user_cuisine_constraint,
        _filter_food_by_cuisine,
        _schedule_preference_extraction,
    )

    amap_places: list[Place] = state.get("amap_places", [])
    rag_chunks: list[dict] = state.get("rag_chunks", [])
    trip_city: str = state.get("trip_city") or "该城市"
    working_ctx = state.get("working_context")

    # 品类硬约束过滤（沿用 v1 逻辑）
    msgs = state.get("messages", []) or []
    last_user_msg = ""
    for m in reversed(msgs):
        if getattr(m, "type", "") == "human" or m.__class__.__name__ == "HumanMessage":
            last_user_msg = str(m.content)
            break

    cuisine_kws = _extract_user_cuisine_constraint(last_user_msg)
    if cuisine_kws:
        amap_places = _filter_food_by_cuisine(amap_places, cuisine_kws)

    amap_places = _cap_places(amap_places)

    if not amap_places:
        return {
            "synthesized_places": [],
            "final_response": "抱歉，暂时没有找到相关地点，请换个描述方式试试。",
            "recommendations": [],
        }

    # Demo 模式
    if settings.demo_mode:
        demo = _build_demo_response_v2(amap_places, trip_city, working_ctx, rag_chunks)
        return {
            "synthesized_places": amap_places,
            "final_response": demo["response_text"],
            "recommendations": demo["recommendations"],
        }

    try:
        llm = _get_llm()
        if llm is None:
            raise RuntimeError("无可用 LLM")

        amap_json = json.dumps(
            [p.model_dump(exclude={"rag_meta", "cluster_id", "visit_order"}) for p in amap_places],
            ensure_ascii=False,
            indent=2,
        )

        # RAG chunks：附带 chunk_id，供 LLM 引用
        rag_text = ""
        if rag_chunks:
            parts = []
            for c in rag_chunks[:8]:
                chunk_id = c.get("chunk_id", "unknown")
                content = c.get("content", "")
                parts.append(f"[{chunk_id}] {content}")
            rag_text = "\n\n".join(parts)
        else:
            rag_text = "（无游记数据，reason 请留空）"

        working_mem_section = ""
        if working_ctx:
            wm_text = format_for_prompt(working_ctx)
            if wm_text:
                working_mem_section = f"\n{wm_text}\n"

        response = await llm.ainvoke([
            SystemMessage(content=SYNTHESIZER_SYSTEM_V2),
            HumanMessage(content=SYNTHESIZER_PROMPT_V2.format(
                amap_places_json=amap_json,
                rag_chunks_text=rag_text,
                working_memory=working_mem_section,
            )),
        ])

        enriched, response_text, recs = _parse_llm_response_raw(
            response.content, amap_places, rag_chunks
        )

        _schedule_preference_extraction(state)

        return {
            "synthesized_places": enriched,
            "final_response": response_text,
            "recommendations": recs,
        }

    except Exception as exc:
        print(f"[SynthesizerV2] LLM 调用失败，降级返回高德数据：{exc}")

    return {
        "synthesized_places": amap_places,
        "final_response": f"为您找到了 {len(amap_places)} 个{trip_city}地点。",
        "recommendations": [],
    }
