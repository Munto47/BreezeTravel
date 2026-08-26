"""EditorAgent（SPEC §4.2 / C3）

处理复杂编辑意图（需要 LLM 推理）：
  - replace_place：换掉某地点
  - add_place：加入新地点
  - rebuild_day：按新模板重建某天

架构：
  1. 接收 user_msg + 当前 itinerary（JSON）
  2. LLM 输出 ItineraryPatch（structured JSON）
  3. 调用 fast_path 或 Planner 局部重跑
  4. Critic 验证
"""

from __future__ import annotations

import json
import re
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import settings
from app.schemas.itinerary import Itinerary
from app.schemas.patch import ItineraryPatch

_EDITOR_SYSTEM = """你是旅行行程编辑助手。
用户会描述他/她想对行程做的修改，你需要将意图转换为结构化 JSON Patch 指令。

支持的操作（op 字段）：
- replace_place    换掉某天某个地点
- add_place        往某天加入新地点
- remove_place     删除某天某个地点
- swap_days        互换两天的所有安排
- rebuild_day      重新规划某天（按新模板）

输出格式（必须合法 JSON，不加 markdown 代码块）：
{
  "op": "replace_place",
  "day_index": 1,
  "slot_index": 0,
  "target_place_id": "被替换的 place_id 或 null",
  "new_place_id": "新 place_id 或 null",
  "new_place_query": "如果没有 new_place_id，描述想找什么类型的地点",
  "new_template_id": "rebuild_day 才用，如 T_FAMILY_LIGHT",
  "rationale": "用一句话解释这个变更",
  "affects_global": false
}

注意：
- day_index 从 0 开始（第一天=0）
- swap_days 时把 target_place_id 设为另一天的 day_index 字符串
- 不确定 place_id 时，把 target_place_id 设为 null，new_place_query 描述意图
- affects_global 仅在替换涉及酒店/质心变化时设为 true
"""


def _get_llm():
    api_key = settings.effective_llm_api_key
    if not api_key:
        return None
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=settings.llm_model_router,
        api_key=api_key,
        base_url=settings.effective_llm_api_url,
        max_tokens=400,
        temperature=0.1,
    )


async def parse_edit_intent(
    user_msg: str,
    itinerary: Itinerary,
) -> Optional[ItineraryPatch]:
    """LLM 将用户消息解析为 ItineraryPatch。失败时返回 None。"""
    llm = _get_llm()
    if llm is None:
        return None

    # 把行程摘要（只含地点名和 day_index）喂给 LLM，避免 token 爆炸
    itin_summary = _summarize_itinerary(itinerary)

    prompt = f"""当前行程摘要：
{itin_summary}

用户请求：{user_msg}

请输出一个 JSON Patch 指令："""

    _MAX_RETRIES = 2
    for attempt in range(1, _MAX_RETRIES + 2):
        extra = "" if attempt == 1 else f"\n\n【第 {attempt} 次】上次 JSON 格式有误，请重新输出。"
        try:
            response = await llm.ainvoke([
                SystemMessage(content=_EDITOR_SYSTEM),
                HumanMessage(content=prompt + extra),
            ])
            raw = response.content.strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
            raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE).strip()

            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if not m:
                raise ValueError("未找到 JSON")
            data = json.loads(m.group())
            patch = ItineraryPatch(**data)
            return patch
        except Exception as e:
            print(f"[EditorAgent] attempt {attempt} 解析失败：{e}")
            if attempt > _MAX_RETRIES:
                return None

    return None


def _summarize_itinerary(itinerary: Itinerary) -> str:
    """把行程压缩成简洁文本供 LLM 理解"""
    lines = [f"城市：{itinerary.city}，共 {len(itinerary.days)} 天"]
    for day in itinerary.days:
        slot_names = [
            f"{s.place_id}({s.place.get('name','?')})" if s.place else s.place_id
            for s in day.slots
            if s.place_id
        ]
        lines.append(f"  第 {day.day_index + 1} 天（day_index={day.day_index}）：{' → '.join(slot_names)}")
    return "\n".join(lines)
