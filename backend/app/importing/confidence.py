from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


def normalize_place_name(value: str) -> str:
    normalized = re.sub(r"[\s·•\-—_（）()]+", "", (value or "").lower())
    return re.sub(r"(?:景区|风景区|旅游区)$", "", normalized)


def candidate_confidence(
    raw_name: str,
    candidate: dict[str, Any],
    *,
    city: str,
    expected_category: str | None = None,
) -> tuple[float, list[str]]:
    raw = normalize_place_name(raw_name)
    candidate_name = normalize_place_name(str(candidate.get("name") or ""))
    raw_aliases = candidate.get("aliases")
    aliases = (
        {
            normalize_place_name(item)
            for item in raw_aliases
            if isinstance(item, str) and normalize_place_name(item)
        }
        if isinstance(raw_aliases, list)
        else set()
    )
    ratio = SequenceMatcher(None, raw, candidate_name).ratio() if raw and candidate_name else 0.0
    reasons: list[str] = []
    if raw == candidate_name and raw:
        name_score = 0.78
        reasons.append("NAME_EXACT")
    elif raw and raw in aliases:
        name_score = 0.78
        reasons.append("NAME_ALIAS_EXACT")
    elif raw and candidate_name and (raw in candidate_name or candidate_name in raw):
        name_score = 0.68
        reasons.append("NAME_CONTAINS")
    else:
        name_score = 0.65 * ratio
        if ratio >= 0.75:
            reasons.append("NAME_SIMILAR")

    score = name_score
    candidate_city = str(candidate.get("city") or "")
    if candidate_city == city or city in candidate_city:
        score += 0.15
        reasons.append("CITY_MATCH")
    candidate_category = str(candidate.get("category") or "")
    if expected_category and candidate_category == expected_category:
        score += 0.05
        reasons.append("CATEGORY_MATCH")
    if candidate.get("district"):
        score += 0.02
        reasons.append("DISTRICT_AVAILABLE")
    return min(round(score, 6), 1.0), reasons
