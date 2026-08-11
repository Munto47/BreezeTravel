"""Deterministic first-pass parser for travel task requirements.

An LLM may propose a structured object upstream, but this module remains the
authoritative validation/fallback path.  It never invents missing hard fields.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Optional

from app.schemas.task_spec import (
    BudgetSpec,
    ConstraintSource,
    DateRange,
    HardConstraint,
    NamedRequirement,
    SoftPreference,
    TaskParseResult,
    Travelers,
    TripTaskSpec,
)
from app.constraints.location import extract_district_constraint


_KNOWN_CITIES = (
    "北京", "上海", "广州", "深圳", "杭州", "成都", "厦门", "南京", "苏州",
    "西安", "重庆", "长沙", "武汉", "青岛", "昆明", "大理", "三亚", "天津",
)
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _number(raw: str) -> int:
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    if raw in _CN_NUM:
        return _CN_NUM[raw]
    if raw.startswith("十"):
        return 10 + _CN_NUM.get(raw[1:], 0)
    if raw.endswith("十"):
        return _CN_NUM.get(raw[0], 1) * 10
    return 0


def _first_match(pattern: str, text: str) -> Optional[str]:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip("，。；;,. ") if match else None


def _extract_named_requirements(text: str) -> tuple[list[NamedRequirement], list[NamedRequirement]]:
    must: list[NamedRequirement] = []
    excluded: list[NamedRequirement] = []
    must_patterns = [
        r"(?:必须|一定要|务必)(?:去|包含|安排)?([^，。；;]{1,20}?)(?=但|并|且|，|。|；|;|$)",
        r"(?:保留|想去)([^，。；;]{1,20}?)(?=但|并|且|，|。|；|;|$)",
    ]
    exclude_patterns = [
        r"(?:不要去|不去|排除|避开)([^，。；;]{1,20}?)(?=但|并|且|，|。|；|;|$)",
        r"(?:不想要|不要)([^，。；;]{1,20}?)(?=但|并|且|，|。|；|;|$)",
    ]
    for pattern in must_patterns:
        for value in re.findall(pattern, text):
            value = value.strip()
            # Collaboration/degradation policies are not literal POI names.
            if value and value not in {"多数投票地点", "投票地点", "实时地点", "已选地点"}:
                must.append(NamedRequirement(value=value, source=ConstraintSource.USER_EXPLICIT))
    for pattern in exclude_patterns:
        for value in re.findall(pattern, text):
            value = value.strip()
            if value:
                excluded.append(NamedRequirement(value=value, source=ConstraintSource.USER_EXPLICIT))
    return _dedupe(must), _dedupe(excluded)


def _dedupe(items: Iterable[NamedRequirement]) -> list[NamedRequirement]:
    seen: set[tuple[str, str]] = set()
    result: list[NamedRequirement] = []
    for item in items:
        key = (item.kind, "".join(item.value.lower().split()))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def parse_task_spec(
    text: str,
    *,
    room_id: str,
    default_city: str = "",
    default_days: int = 0,
    current_revision: int = 0,
    memory_preferences: Optional[list[str]] = None,
    start_date: Optional[date] = None,
) -> TaskParseResult:
    clean = " ".join(text.strip().split())
    city = next((item for item in _KNOWN_CITIES if item in clean), default_city)

    days_raw = _first_match(r"([0-9一二两三四五六七八九十]{1,3})\s*(?:天|日)(?:游|行程)?", clean)
    days = _number(days_raw) if days_raw else default_days

    adults = 1
    children = 0
    seniors = 0
    people_raw = _first_match(r"([0-9一二两三四五六七八九十]{1,3})\s*(?:人|位)", clean)
    if people_raw:
        adults = max(1, _number(people_raw))
    children_raw = _first_match(r"([0-9一二两三四五六七八九十]{1,3})\s*(?:个)?(?:孩子|儿童|小孩)", clean)
    if children_raw:
        children = _number(children_raw)
        adults = max(1, adults - children) if people_raw else adults
    seniors_raw = _first_match(r"([0-9一二两三四五六七八九十]{1,3})\s*(?:位|个)?(?:老人|长辈|老年人)", clean)
    if seniors_raw:
        seniors = _number(seniors_raw)
        adults = max(1, adults - seniors) if people_raw else adults

    budget = None
    budget_match = re.search(r"(?:预算|不超过|控制在)\s*(?:人民币|¥|￥)?\s*([0-9]+(?:\.[0-9]+)?)\s*(万|元|块)?", clean)
    if budget_match:
        amount = float(budget_match.group(1)) * (10000 if budget_match.group(2) == "万" else 1)
        if "每人每天" in clean or "人均每天" in clean:
            scope = "per_person_per_day"
        elif "人均" in clean or "每人" in clean:
            scope = "per_person"
        elif "每天" in clean or "每日" in clean:
            scope = "per_day"
        else:
            scope = "total"
        budget = BudgetSpec(amount=amount, scope=scope)

    must_include, excluded = _extract_named_requirements(clean)
    hard: list[HardConstraint] = []
    district = extract_district_constraint(clean)
    if district:
        hotel_only = bool(re.search(
            rf"(?:酒店|住宿|住处|住在)[^，。；;]{{0,16}}{re.escape(district)}|"
            rf"{re.escape(district)}[^，。；;]{{0,16}}(?:酒店|住宿|住处)",
            clean,
        )) and not any(word in clean for word in ("只在", "限定", "范围", "全程", "活动", "行程"))
        hard.append(HardConstraint(
            id="c_hotel_area" if hotel_only else "c_trip_area",
            type="hotel_area" if hotel_only else "trip_area",
            operator="eq",
            value=district,
            scope="hotel" if hotel_only else "trip",
        ))
    travel_match = re.search(r"(?:每天|每日)?(?:交通|通勤)(?:时间)?(?:不超过|最多|控制在)\s*([0-9]+)\s*(分钟|小时)", clean)
    if travel_match:
        minutes = int(travel_match.group(1)) * (60 if travel_match.group(2) == "小时" else 1)
        hard.append(HardConstraint(
            id="c_max_daily_travel",
            type="max_daily_travel_minutes",
            operator="lte",
            value=minutes,
            unit="minute",
            scope="per_day",
        ))
    capacity_match = re.search(r"(?:每天|每日)(?:最多|不超过)\s*([0-9]+)\s*(?:个)?(?:地点|景点)", clean)
    if capacity_match:
        hard.append(HardConstraint(
            id="c_max_daily_places",
            type="max_daily_places",
            operator="lte",
            value=int(capacity_match.group(1)),
            unit="place",
            scope="per_day",
        ))
    if any(word in clean for word in ("雨天不要户外", "下雨不去户外", "雨天必须室内")):
        hard.append(HardConstraint(
            id="c_avoid_outdoor_rain",
            type="avoid_outdoor_on_rain",
            operator="eq",
            value=True,
            scope="per_day",
        ))
    if "保留多数投票地点" in clean:
        hard.append(HardConstraint(
            id="c_preserve_majority_vote",
            type="preserve_majority_voted",
            operator="eq",
            value=True,
            scope="trip",
        ))
    # BreezeTravel 的路线产品按“每天结束于住宿点”展示；这不是让模型
    # 猜酒店，而是要求规划器使用用户已选择的酒店作为每日夜间锚点。
    if days > 0:
        hard.append(HardConstraint(
            id="c_daily_hotel",
            type="daily_hotel",
            operator="eq",
            value=True,
            scope="per_day",
        ))

    soft: list[SoftPreference] = []
    if any(word in clean for word in ("亲子", "带娃", "孩子")):
        soft.append(SoftPreference(id="p_family", type="family_friendly", value=True, weight=0.9, source=ConstraintSource.USER_EXPLICIT))
    if any(word in clean for word in ("老人", "长辈", "老年人")):
        soft.append(SoftPreference(id="p_senior", type="senior_friendly", value=True, weight=0.9, source=ConstraintSource.USER_EXPLICIT))
    if any(word in clean for word in ("少走路", "步行别太累", "不要太累", "轻松一点")):
        soft.append(SoftPreference(id="p_low_walking", type="low_walking", value=True, weight=0.9, source=ConstraintSource.USER_EXPLICIT))
    if "低预算" in clean or "省钱" in clean:
        soft.append(SoftPreference(id="p_budget", type="prefer_low_cost", value=True, weight=0.8, source=ConstraintSource.USER_EXPLICIT))
    if "失败时保留" in clean or "明确降级" in clean:
        soft.append(SoftPreference(
            id="p_degradation_policy",
            type="explicit_partial_result_degradation",
            value=True,
            weight=1.0,
            source=ConstraintSource.USER_EXPLICIT,
        ))
    from app.memory.governance import is_stable_preference
    for index, preference in enumerate(memory_preferences or []):
        if not is_stable_preference(preference):
            continue
        soft.append(SoftPreference(
            id=f"p_memory_{index + 1}",
            type="memory_preference",
            value=preference,
            weight=0.4,
            source=ConstraintSource.MEMORY,
        ))

    spec = TripTaskSpec(
        room_id=room_id,
        task_revision=current_revision + 1,
        city=city,
        date_range=DateRange(start=start_date, days=days),
        travelers=Travelers(adults=adults, children=children, seniors=seniors),
        budget=budget,
        must_include=must_include,
        exclude=excluded,
        hard_constraints=hard,
        soft_preferences=soft,
    )
    fields = (spec.conflicts + spec.missing_fields)[:3]
    message = None
    if fields:
        labels = {"city": "目的地城市", "date_range.days": "旅行天数"}
        message = "还需要确认：" + "、".join(labels.get(item, item) for item in fields)
    return TaskParseResult(
        task_spec=spec,
        needs_clarification=spec.needs_clarification,
        clarification_fields=fields,
        clarification_message=message,
    )
