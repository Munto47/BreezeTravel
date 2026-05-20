"""
TipsGenerator：为每个行程时间段生成上下文感知的温馨提示

在 /api/optimize 完成排线后调用，为每个 TimeSlot 生成 0-3 条贴心提示：
- 热门景区 → 取号/排队建议
- 餐厅 → 招牌菜推荐（若 RAG 游记或 POI 数据有记录）
- 酒店 check-in → 楼层/停车场提示
- 天气有雨 → 带伞提醒
- 大型景区 → 穿着/体力提示
- 夜间活动 → 营业时间说明
- 有老人小孩 → 安全注意事项
"""

import json
import re
from typing import Optional

from app.config import settings
from app.schemas.itinerary import Itinerary, TimeSlot


_TIPS_SYSTEM = "你是贴心的旅行顾问，输出格式严格的 JSON，不要加 markdown 代码块。"

_TIPS_PROMPT = """为以下行程的每个地点生成温馨提示（每个地点 0-3 条，每条 15-40 字）。

只在有实际价值时才添加提示，不要强行填充。优先考虑：
1. 需要取号/排队的热门景区 → "建议提前到，高峰期排队约X分钟，可关注官方公众号取号"
2. 知名餐厅 → 推荐1-2道招牌菜（如 POI 标签或游记中提到过）
3. 酒店 check-in → "前台通常在X楼，可提前确认停车位"（仅当有相关信息时）
4. 当天天气有雨（condition 含"雨"）→ "今日有雨，出门记得带伞"
5. estimated_duration > 180 分钟的大型景区 → "全程步行较多，建议穿舒适平底鞋并携带饮用水"
6. 夜间营业地点（start_time >= 20:00）→ 说明营业特点
7. 有体力限制（老人小孩）→ 提醒注意安全/休息

行程数据（包含地点名称、分类、标签、描述、开放时间、估计时长等）：
{itinerary_json}

用户偏好：{preferences}

输出 JSON（数组，每项对应一个 slot，按 place_id 索引）：
[{{"place_id": "...", "tips": ["提示1", "提示2"]}}, ...]
不包含其他文字。"""

# 规则化提示（不调用 LLM，直接生成）
_QUEUE_KEYWORDS = r"大熊猫|故宫|颐和园|西湖|鸟巢|兵马俑|黄山|张家界|布达拉宫|武侯祠|乐山大佛|九寨沟"
_RAINY_KEYWORDS = r"雨|雷|阵雨|暴雨"


def _rule_based_tips(slot: TimeSlot, weather_condition: Optional[str]) -> list[str]:
    """不依赖 LLM 的快速规则提示（兜底）"""
    tips: list[str] = []
    place = slot.place
    name = place.get("name", "")
    tags = place.get("tags", [])
    category = place.get("category", "")
    duration = place.get("estimated_duration") or 0

    # 天气有雨
    if weather_condition and re.search(_RAINY_KEYWORDS, weather_condition):
        if not any("雨" in t for t in tips):
            tips.append("今日有降雨，出门记得带伞，注意路面防滑")

    # 超长景区
    if category == "attraction" and duration >= 180:
        tips.append("景区较大，全程步行较多，建议穿舒适平底鞋并多补水")

    # 热门打卡地
    if re.search(_QUEUE_KEYWORDS, name):
        tips.append("热门景区，建议提前出发，避开节假日高峰排队")

    # 夜间活动
    try:
        h = int(slot.start_time.split(":")[0])
        if h >= 20:
            tips.append(f"夜间场所，通常营业至深夜，注意回程交通安排")
    except Exception:
        pass

    return tips[:2]


async def generate_tips(
    itinerary: Itinerary,
    preferences: str = "",
) -> Itinerary:
    """为 Itinerary 中每个 TimeSlot 生成温馨提示，返回更新后的 Itinerary。

    优先调用 LLM 生成高质量提示；无 API Key 或调用失败时降级到规则提示。
    """
    api_key = settings.effective_llm_api_key
    use_llm = bool(api_key) and not settings.demo_mode

    if use_llm:
        try:
            return await _llm_generate_tips(itinerary, preferences)
        except Exception as exc:
            print(f"[TipsGenerator] LLM 调用失败，降级到规则提示：{exc}")

    return _rule_generate_tips(itinerary)


def _rule_generate_tips(itinerary: Itinerary) -> Itinerary:
    """规则化提示（兜底路径）"""
    updated_days = []
    for day in itinerary.days:
        weather_cond = day.weather_summary.condition if day.weather_summary else None
        updated_slots = []
        for slot in day.slots:
            if slot.place_id.startswith("__meal_"):
                updated_slots.append(slot)
                continue
            tips = _rule_based_tips(slot, weather_cond)
            updated_slots.append(slot.model_copy(update={"tips": tips}))
        updated_days.append(day.model_copy(update={"slots": updated_slots}))
    return itinerary.model_copy(update={"days": updated_days})


async def _llm_generate_tips(itinerary: Itinerary, preferences: str) -> Itinerary:
    """LLM 生成温馨提示"""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=settings.llm_model_synthesizer,
        api_key=settings.effective_llm_api_key,
        base_url=settings.effective_llm_api_url,
        max_tokens=1500,
        temperature=0.4,
    )

    # 精简序列化（只保留 LLM 需要的字段）
    itinerary_slim = []
    for day in itinerary.days:
        weather_cond = day.weather_summary.condition if day.weather_summary else None
        for slot in day.slots:
            if slot.place_id.startswith("__meal_"):
                continue
            p = slot.place
            itinerary_slim.append({
                "place_id": slot.place_id,
                "name": p.get("name", ""),
                "category": p.get("category", ""),
                "tags": p.get("tags", [])[:4],
                "description": p.get("description", ""),
                "opening_hours": p.get("opening_hours", ""),
                "estimated_duration": p.get("estimated_duration"),
                "start_time": slot.start_time,
                "weather": weather_cond,
            })

    if not itinerary_slim:
        return itinerary

    response = await llm.ainvoke([
        SystemMessage(content=_TIPS_SYSTEM),
        HumanMessage(content=_TIPS_PROMPT.format(
            itinerary_json=json.dumps(itinerary_slim, ensure_ascii=False, indent=2),
            preferences=preferences or "（无特殊偏好）",
        )),
    ])

    raw = response.content.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)

    arr_match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not arr_match:
        return _rule_generate_tips(itinerary)

    tips_list: list[dict] = json.loads(arr_match.group())
    tips_map = {item["place_id"]: item.get("tips", []) for item in tips_list if "place_id" in item}

    # 将 LLM 生成的 tips 合并回 Itinerary
    updated_days = []
    for day in itinerary.days:
        weather_cond = day.weather_summary.condition if day.weather_summary else None
        updated_slots = []
        for slot in day.slots:
            if slot.place_id.startswith("__meal_"):
                updated_slots.append(slot)
                continue
            llm_tips = tips_map.get(slot.place_id, [])
            # 合并规则提示（去重）
            rule_tips = _rule_based_tips(slot, weather_cond)
            merged = list(dict.fromkeys(llm_tips + [t for t in rule_tips if t not in llm_tips]))[:3]
            updated_slots.append(slot.model_copy(update={"tips": merged}))
        updated_days.append(day.model_copy(update={"slots": updated_slots}))

    return itinerary.model_copy(update={"days": updated_days})
