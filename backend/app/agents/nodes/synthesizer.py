"""
Synthesizer 节点：数据合并 + 回复生成 + 偏好提取触发

Sprint 2 变更：
- LLM 切换为 DeepSeek（settings.effective_llm_api_key / url）
- 注入 Working Memory（用户当前偏好）优化推荐质量
- 完成后异步触发长期偏好提取（后台任务，不阻塞响应）
"""

import asyncio
import json
import re
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.state import AgentState
from app.config import settings
from app.memory.working import format_for_prompt
from app.schemas.place import Place, PlaceRAGMeta

SYNTHESIZER_SYSTEM = "你是旅行规划助手，返回格式严格的 JSON，不要加 markdown 代码块。"

SYNTHESIZER_PROMPT = """根据以下地点数据和游记摘录，生成个性化的旅行推荐。

高德 POI 数据（客观）：
{amap_places_json}

游记经验摘录（主观，可能为空）：
{rag_chunks_text}

{working_memory}

任务：
1. 为每个 POI 生成 description（一句话特点描述，20-40字）和 tags（2-4个标签，如"亲子""情侣约会""拍照打卡""本地人推荐""连锁品牌""网红打卡"）
2. 如果有游记数据，为相关 POI 提取 1-3 条避坑/推荐语
3. 推断每个地点合理的建议游览时长（estimated_duration，单位分钟），参考：
   - 足浴/按摩/SPA → 180-240；大型主题公园/5A景区 → 300-420；博物馆 → 120-180
   - 古镇/历史街区 → 90-150；普通景点/公园 → 60-90；快餐/咖啡 → 30-45；正餐/火锅 → 60-90
   - 根据地点名称和描述综合判断，不确定时返回 null
4. 结合用户偏好（如果有）：
   - 若用户是素食/清真/特定饮食需求 → 在 response_text 中特别说明推荐原因
   - 若用户国籍/偏好特定菜系 → 优先描述对应菜系的地点
   - 若用户偏好连锁品牌 → 标注"连锁品牌"标签
5. 生成个性化推荐说明（150字以内，友好亲切）
6. 必须返回合法 JSON（不加 markdown 代码块）：
   {{"response_text": "...", "place_updates": [{{"place_id": "...", "description": "...", "tags": ["..."], "tip_snippets": [...], "sentiment_score": 0.8, "estimated_duration": 120}}]}}
不要包含任何其他文字。"""


def _get_llm():
    """获取 LLM 实例（优先 DeepSeek，回退 OpenAI）"""
    api_key = settings.effective_llm_api_key
    if not api_key:
        return None
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=settings.llm_model_synthesizer,
        api_key=api_key,
        base_url=settings.effective_llm_api_url,
        max_tokens=1200,
        temperature=0.3,
    )


async def run(state: AgentState) -> dict:
    """Synthesizer 节点入口"""
    amap_places: list[Place] = state.get("amap_places", [])
    rag_chunks: list[dict] = state.get("rag_chunks", [])
    trip_city: str = state.get("trip_city") or "该城市"
    working_ctx = state.get("working_context")

    if not amap_places:
        return {
            "synthesized_places": [],
            "final_response": "抱歉，暂时没有找到相关地点，请换个描述方式试试。",
        }

    # Demo 模式
    if settings.demo_mode:
        return {
            "synthesized_places": amap_places,
            "final_response": f"为您找到了 {len(amap_places)} 个{trip_city}相关地点，请查看右侧地点列表。",
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
        rag_text = "\n\n".join(c["content"] for c in rag_chunks[:5]) if rag_chunks else "（无游记数据）"

        # 注入工作记忆
        working_mem_section = ""
        if working_ctx:
            wm_text = format_for_prompt(working_ctx)
            if wm_text:
                working_mem_section = f"\n{wm_text}\n"

        response = await llm.ainvoke([
            SystemMessage(content=SYNTHESIZER_SYSTEM),
            HumanMessage(content=SYNTHESIZER_PROMPT.format(
                amap_places_json=amap_json,
                rag_chunks_text=rag_text,
                working_memory=working_mem_section,
            )),
        ])

        raw = response.content.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
        raw = raw.strip()

        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            updates = {u["place_id"]: u for u in result.get("place_updates", [])}
            enriched = []
            for place in amap_places:
                if place.place_id in updates:
                    u = updates[place.place_id]
                    update_fields = {}
                    if u.get("tip_snippets"):
                        update_fields["rag_meta"] = PlaceRAGMeta(
                            tip_snippets=u.get("tip_snippets", [])[:3],
                            sentiment_score=u.get("sentiment_score", 0.0),
                            source_note_ids=[
                                c["note_id"] for c in rag_chunks
                                if place.place_id in c.get("place_ids", [])
                            ],
                        )
                    if u.get("description"):
                        update_fields["description"] = u["description"]
                    if u.get("tags"):
                        update_fields["tags"] = u["tags"][:4]
                    dur = u.get("estimated_duration")
                    if isinstance(dur, (int, float)) and 15 <= dur <= 600:
                        update_fields["estimated_duration"] = int(dur)
                        update_fields["duration_basis"] = "llm"
                    if update_fields:
                        place = place.model_copy(update=update_fields)
                enriched.append(place)

            response_text = result.get("response_text", f"为您找到了 {len(enriched)} 个相关地点。")

            # 后台触发长期偏好提取（不等待，不阻塞响应）
            _schedule_preference_extraction(state)

            return {
                "synthesized_places": enriched,
                "final_response": response_text,
            }

    except Exception as exc:
        print(f"[Synthesizer] LLM 调用失败，直接返回高德数据：{exc}")

    return {
        "synthesized_places": amap_places,
        "final_response": f"为您找到了 {len(amap_places)} 个{trip_city}地点，请查看地点列表。",
    }


def _schedule_preference_extraction(state: AgentState) -> None:
    """
    后台异步触发长期偏好提取（fire-and-forget）

    在当前事件循环中创建独立 task，不阻塞 Synthesizer 的返回。
    失败时静默忽略（已在 longterm.py 内部处理）。
    """
    user_id = state.get("user_id", "")
    messages = state.get("messages", [])
    trip_city = state.get("trip_city")

    if not user_id or user_id in ("anonymous", "tool-call", "eval"):
        return

    async def _extract():
        from app.memory.longterm import save_conversation_preferences
        await save_conversation_preferences(user_id, messages, trip_city)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_extract())
    except Exception:
        pass  # 偏好提取失败不影响主流程
