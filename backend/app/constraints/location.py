"""Deterministic extraction and enforcement of explicit area constraints."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Optional


_DISTRICT_PATTERN = re.compile(r"([\u4e00-\u9fff]{1,8}(?:新区|自治县|区|县|旗))")

# Product-supported cities.  Known names avoid the ambiguity of Chinese text
# without word boundaries (for example, a regex alone can mistake
# ``只在闵行区`` for the district name).
_KNOWN_DISTRICTS = (
    "东城区", "西城区", "朝阳区", "海淀区", "延庆区",
    "黄浦区", "浦东新区", "静安区", "徐汇区", "闵行区",
    "青羊区", "武侯区", "成华区", "锦江区", "高新区", "都江堰市",
    "思明区", "集美区",
    "荔湾区", "海珠区", "越秀区", "白云区", "天河区",
    "大鹏新区", "罗湖区", "南山区", "福田区", "宝安区", "龙华区",
    "西湖区", "余杭区", "上城区", "拱墅区",
)
_LEADING_AREA_WORDS = (
    "最终只在", "最后只在", "改成", "改到", "限定在", "限制在", "只安排在",
    "只推荐", "只考虑", "只在", "位于", "住在", "安排在", "推荐", "不要", "在", "去",
)


def normalise_area(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def extract_district_constraint(text: str) -> Optional[str]:
    """Return the last explicit Chinese district/county mentioned by the user.

    Taking the last match makes a correction such as ``不要浦东，改成闵行区``
    resolve to 闵行区 instead of silently retaining the stale area.
    """

    value = text or ""
    known_hits = [(value.rfind(district), district) for district in _KNOWN_DISTRICTS if district in value]
    if known_hits:
        return max(known_hits, key=lambda item: item[0])[1]

    matches = _DISTRICT_PATTERN.findall(value)
    if not matches:
        return None
    candidate = matches[-1]
    changed = True
    while changed:
        changed = False
        for prefix in _LEADING_AREA_WORDS:
            if candidate.startswith(prefix) and len(candidate) > len(prefix) + 1:
                candidate = candidate[len(prefix):]
                changed = True
                break
    return candidate


def extract_district_from_messages(messages: Iterable[Any]) -> Optional[str]:
    """Read human messages newest-first so the latest area correction wins."""

    for message in reversed(list(messages or [])):
        message_type = getattr(message, "type", "")
        if message_type not in {"human", "user"} and message.__class__.__name__ != "HumanMessage":
            continue
        district = extract_district_constraint(str(getattr(message, "content", "")))
        if district:
            return district
    return None


def place_matches_district(place: Any, district: str) -> bool:
    target = normalise_area(district)
    if not target:
        return True
    if hasattr(place, "model_dump"):
        raw = place.model_dump()
    elif isinstance(place, dict):
        raw = place
    else:
        raw = {}
    location_text = normalise_area(f"{raw.get('district', '')} {raw.get('address', '')}")
    if target not in location_text:
        return False

    # Reject visibly contradictory POI names such as ``玩木工坊(浦东店)``
    # even when an upstream record claims district=闵行区.  A person reads the
    # branch name first, so allowing it would make the hard area constraint
    # look ignored.
    name = normalise_area(raw.get("name", ""))
    for known in _KNOWN_DISTRICTS:
        known_normalised = normalise_area(known)
        if known_normalised == target:
            continue
        aliases = {known_normalised}
        for suffix in ("自治县", "新区", "区", "县", "旗", "市"):
            if known_normalised.endswith(suffix):
                short = known_normalised[: -len(suffix)]
                if len(short) >= 2:
                    aliases.add(short)
                break
        if any(alias in name for alias in aliases):
            return False
    return True


def filter_places_by_district(places: Iterable[Any], district: Optional[str]) -> list[Any]:
    items = list(places)
    if not district:
        return items
    return [place for place in items if place_matches_district(place, district)]


_RETAIL_NOT_ATTRACTION = ("孩子王", "母婴店", "便利店", "药房", "房产中介", "停车场")


def place_is_human_suitable(place: Any) -> bool:
    """Reject obvious upstream category mistakes that look absurd to users."""
    if hasattr(place, "model_dump"):
        raw = place.model_dump()
    elif isinstance(place, dict):
        raw = place
    else:
        return True
    category = raw.get("category")
    category = getattr(category, "value", category)
    name = str(raw.get("name", ""))
    if category == "attraction" and any(term in name for term in _RETAIL_NOT_ATTRACTION):
        return False
    return True


def filter_human_suitable_places(places: Iterable[Any]) -> list[Any]:
    return [place for place in places if place_is_human_suitable(place)]
