"""The same ordered, valid POIs for automatic matching and user search."""
from __future__ import annotations

import re

from app.constraints.amap_types import classify_amap_type_signals
from app.schemas.place import PlaceCategory
from app.trip_understanding.amap_place import (
    _CITY_BOUNDS, _MatchedCandidate, _admin_matches, _coordinates,
    _expected_category, _name_match_tier, _visitor_type_compatible, _lexical_category, _evaluate_candidates,
    _product_semantic_technical_category_is_compatible,
)
from app.trip_understanding.city_knowledge import get_city_knowledge
from app.trip_understanding.landmark_hints import landmark_hint, verified_technical_landmark
from app.trip_understanding.pipeline import atomic_place_rejection_reason


async def ranked_candidates(provider, *, city: str, query: str, category_hint: str | None):
    city = city.strip().removesuffix("市")
    query = query.strip()
    empty = {"external_calls": 0, "status": "NO_VALID_CANDIDATE"}
    if not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff·（）()—_ -]{1,40}", query):
        return [], empty
    expected = _expected_category(category_hint) or _lexical_category(query)
    gate = re.search(r"(?:大学|公园|景区|寺|博物馆)[-（(]?([东西南北]+\d*门)[）)]?$", query)
    if gate or query.endswith("大学"):
        expected = PlaceCategory.ATTRACTION
    if query.endswith(("体育场", "体育馆", "游泳中心")):
        expected = PlaceCategory.ATTRACTION
    if query.endswith(("路", "街", "巷", "胡同")) and expected is None:
        expected = PlaceCategory.ATTRACTION
    hint = landmark_hint(city, query) if expected in {None, PlaceCategory.ATTRACTION} else None
    reviewed = get_city_knowledge().query_lookup(city=city, name=query)
    entry = reviewed.unique
    if entry is not None and (len(entry.canonical_name) > 40 or expected not in {None, PlaceCategory(entry.category)}):
        entry = None
    if reviewed.matches:
        hint = None
    canonical = entry.canonical_name if entry else hint.name if hint else query
    aliases = entry.aliases if entry else hint.aliases if hint else ()
    scope = await provider.city_scope(city) if city not in _CITY_BOUNDS else None
    if city not in _CITY_BOUNDS and scope is None:
        return [], empty
    rows, receipt = await provider._query_provider(city=city, query_name=canonical,
        original_atomic=query, category_basis="SHARED_SEARCH", typecodes=[], lexicon_binding={})
    places = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        poi_id = str(row.get("id") or "").strip()
        if (not poi_id or atomic_place_rejection_reason("".join(name.split()))
                or not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff·（）()—_ -]{1,40}", name)):
            continue
        if not _admin_matches(row, expected_city=city, expected_district=None, scope=scope):
            continue
        signals = classify_amap_type_signals(str(row.get("typecode") or ""), str(row.get("type") or ""))
        if len(str(row.get("typecode") or "").split("|")) != len(str(row.get("type") or "").split("|")):
            continue
        category = signals.category
        tier = _name_match_tier(row, canonical_name=canonical, safe_aliases=aliases, city=city)
        compact = lambda value: re.sub(r"[\s（）()·—_-]", "", value)
        # A gate is a distinct destination. A nearby bus stop, department or
        # monument cannot supply its identity even when search ranks it first.
        if gate and compact(name) != compact(query):
            continue
        # A named neighbourhood is a valid destination, including an area
        # specified for lunch. It is never represented as a restaurant.
        area_match = (row.get("typecode") == "190700"
            and row.get("type") == "地名地址信息;热点地名;热点地名"
            and name == query.removesuffix("商圈")
            and expected in {None, PlaceCategory.FOOD})
        visitor = expected in {None, PlaceCategory.ATTRACTION} and _visitor_type_compatible(row, canonical)
        if expected == PlaceCategory.ATTRACTION and compact(name) == compact(query):
            visitor = visitor or (query.endswith("大学") and row.get("typecode") == "141201"
                and row.get("type") == "科教文化服务;学校;高等院校")
            visitor = visitor or (bool(gate) and row.get("typecode") == "991000"
                and row.get("type") == "通行设施;建筑物门;建筑物门")
        visitor = visitor or (expected in {None, PlaceCategory.ATTRACTION}
            and _product_semantic_technical_category_is_compatible(row, atomic=canonical,
                expected_category=PlaceCategory.ATTRACTION))
        if visitor:
            category = PlaceCategory.ATTRACTION
        if area_match:
            category = PlaceCategory.UNKNOWN
        if signals.conflict and not visitor:
            continue
        if not visitor and not area_match and (not signals.complete or category == PlaceCategory.UNKNOWN):
            if expected in {None, PlaceCategory.ATTRACTION} and verified_technical_landmark(row, city=city, name=query):
                category = PlaceCategory.ATTRACTION
            else:
                continue
        if not area_match and expected is not None and category != expected:
            continue
        coordinates = _coordinates(row.get("location"))
        west, east, south, north = scope.bounds if scope else _CITY_BOUNDS[city]
        if not coordinates or not (west <= coordinates[0] <= east and south <= coordinates[1] <= north):
            continue
        tier = "CANONICAL_EXACT" if area_match else tier or "RELATED"
        places.setdefault(poi_id, _MatchedCandidate(raw=row, category=category, coordinates=coordinates,
            tier=tier, category_compatibility_basis="SHARED_SEARCH"))
    tiers = {"CANONICAL_EXACT": 0, "SAFE_ALIAS_EXACT": 1, "VENUE_SUFFIX_EQUIVALENT": 2}
    ordered = sorted(places.values(), key=lambda place: tiers.get(place.tier, 3))[:6]
    assessment = _evaluate_candidates(rows, city=city, canonical_name=canonical, safe_aliases=aliases,
        expected_category=expected, expected_district=None, atomic=query, scope=scope)
    if ordered and assessment.selected and ordered[0].raw.get("id") == assessment.selected.raw.get("id"):
        ordered[0] = assessment.selected
    return ordered, {**receipt, **assessment.metrics, "selection_policy": "FIRST_VALID_SEARCH_RESULT",
                     "candidate_count": len(ordered)}
