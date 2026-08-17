"""Single deterministic eligibility pipeline for retrieved POI candidates.

Coverage is meaningful only after every hard filter that also governs the
cards delivered to the user. Router, ToolExecutor and Synthesizer share this
module so a raw provider hit cannot prematurely satisfy a plan slot.
"""

from __future__ import annotations

import re
from itertools import combinations, product
from typing import Any

from app.constraints.evidence_resolver import finalize_place_evidence, resolve_candidate_evidence
from app.constraints.location import filter_human_suitable_places, filter_places_by_district
from app.constraints.place_identity import coordinate_distance_meters
from app.constraints.recommendation_intent import (
    cluster_mixed_places_for_request,
    extract_landmark_groups,
    filter_places_for_request,
    rank_places_for_request,
)
from app.constraints.selection_policy import select_evidence_eligible_candidates
from app.schemas.place import EvidenceStatus, GeoEvidence, Place, PlaceCategory


_CUISINE_HARD_KEYWORDS = {
    "火锅": ["火锅"],
    "串串": ["串串", "串"],
    "烤肉": ["烤肉", "烧烤", "烤"],
    "烧烤": ["烧烤", "烤肉"],
    "日料": ["日料", "日本", "寿司", "刺身", "拉面"],
    "韩餐": ["韩餐", "韩国", "烤肉", "石锅"],
    "西餐": ["西餐", "牛排"],
    "意大利": ["意大利", "披萨", "意面"],
    "披萨": ["披萨", "比萨"],
    "咖啡": ["咖啡", "café", "Café", "Coffee", "coffee"],
    "茶馆": ["茶馆", "茶社", "茶舍"],
    "粤菜": ["粤菜", "广式", "茶餐厅"],
    "川菜": ["川菜"],
    "湘菜": ["湘菜", "湖南", "长沙", "潇湘", "湘味", "湘食"],
    "北京菜": ["北京菜", "京菜", "烤鸭"],
    "本帮菜": ["本帮菜", "上海菜", "本帮"],
    "上海菜": ["本帮菜", "上海菜", "本帮", "生煎", "小笼", "馄饨", "蟹黄面"],
    "杭帮菜": ["杭帮菜", "杭州菜", "桐庐菜", "江浙菜"],
    "杭州菜": ["杭帮菜", "杭州菜", "桐庐菜", "江浙菜"],
    "生煎": ["生煎"],
    "小笼": ["小笼", "小笼包"],
    "片儿川": ["片儿川", "杭州面"],
}

_REGIONAL_CUISINE_ALIASES = {
    "北京菜": ("北京菜", "京菜", "烤鸭", "炸酱面", "豆汁", "涮肉", "白水羊头", "北京小吃", "京味"),
    "本帮菜": ("本帮菜", "上海菜", "本帮", "生煎", "小笼"),
    "上海菜": ("本帮菜", "上海菜", "本帮", "生煎", "小笼", "馄饨", "蟹黄面"),
    "杭帮菜": ("杭帮菜", "杭州菜", "桐庐菜", "江浙菜", "片儿川", "杭州面"),
    "杭州菜": ("杭帮菜", "杭州菜", "桐庐菜", "江浙菜", "片儿川", "杭州面"),
}


def extract_user_cuisine_constraint(user_request: str) -> list[str]:
    """Extract explicit cuisine keywords that retrieved food must support."""
    normalized_request = (user_request or "").replace("本帮餐厅", "本帮菜").replace("杭帮餐厅", "杭帮菜")
    hits: list[str] = []
    for trigger, keywords in _CUISINE_HARD_KEYWORDS.items():
        if trigger in normalized_request:
            # Explicit exclusions must never become positive hard filters.
            start = normalized_request.find(trigger)
            prefix = normalized_request[max(0, start - 10):start]
            if any(term in prefix for term in ("不要", "不吃", "别推荐", "排除", "不想")):
                continue
            # "餐厅或咖啡馆" is an allowed alternative, not a mandatory
            # coffee constraint. Requiring 咖啡 here deletes valid restaurants.
            if trigger == "咖啡" and any(
                phrase in normalized_request
                for phrase in (
                    "餐厅或咖啡", "餐厅或者咖啡", "吃饭或咖啡", "吃饭或者咖啡",
                    "茶或咖啡", "茶或者咖啡", "喝茶或咖啡", "喝茶或者咖啡",
                )
            ):
                continue
            hits.extend(keywords)
    expanded: list[str] = []
    for keyword in hits:
        aliases = _REGIONAL_CUISINE_ALIASES.get(keyword)
        expanded.extend(aliases or (keyword,))
    return list(dict.fromkeys(expanded))


def filter_food_by_cuisine(places: list[Place], cuisine_keywords: list[str]) -> list[Place]:
    """Filter FOOD candidates by an explicit cuisine; preserve other slots."""
    if not cuisine_keywords:
        return list(places)
    kept: list[Place] = []
    for place in places:
        if place.category != PlaceCategory.FOOD:
            kept.append(place)
            continue
        haystack = f"{place.name} {' '.join(place.tags or [])} {place.description or ''}"
        if any(keyword in haystack for keyword in cuisine_keywords):
            kept.append(place)
    return kept


def select_eligible_places(
    places: list[Place],
    user_request: str,
    district: str | None = None,
    recommendation_plan: Any = None,
) -> list[Place]:
    """Apply the exact hard-filter/evidence policy used for delivered cards."""
    selected = filter_food_by_cuisine(
        list(places),
        extract_user_cuisine_constraint(user_request),
    )
    selected = filter_human_suitable_places(
        filter_places_by_district(selected, district),
    )
    selected = _filter_category_identity_conflicts(selected)
    plan_categories = None
    if recommendation_plan:
        slots = (
            recommendation_plan.slots
            if hasattr(recommendation_plan, "slots")
            else recommendation_plan.get("slots", [])
        )
        plan_categories = [
            slot.category if hasattr(slot, "category") else slot.get("category")
            for slot in slots
        ]
    selected = filter_places_for_request(
        selected,
        user_request,
        explicit_category=plan_categories or "",
    )
    selected = _filter_semantic_area(selected, user_request)
    selected = _filter_by_requested_open_time(selected, user_request)
    selected = _filter_exact_entity_slots(selected, recommendation_plan)
    selected = resolve_candidate_evidence(
        selected,
        user_request,
        district,
        recommendation_plan,
    )
    selected = _filter_time_sensitive_hub_candidates(selected, user_request)
    selected = _filter_named_area_candidates(selected, user_request)
    selected = _filter_meal_identity_candidates(selected, user_request)
    selected = _filter_low_transfer_candidates(selected, user_request)
    selected = _filter_local_snack_candidates(selected, user_request)
    selected = _filter_city_local_food_candidates(selected, user_request)
    selected = rank_places_for_request(selected, user_request)
    selected = _dedupe_spatially_redundant_attractions(selected, user_request)
    selected = cluster_mixed_places_for_request(selected, user_request)
    # Bind cross-category distance evidence only after the final portfolio is
    # known.  Otherwise a close raw candidate can become the anchor and then
    # be removed by ranking/clustering, leaving confirmation actions that name
    # a place the user never received.
    selected = _attach_shared_anchor_evidence(selected, user_request)
    selected = _attach_delivered_attraction_evidence(selected, user_request)
    selected = _attach_low_transfer_core_evidence(selected, user_request)
    selected = _drop_obviously_remote_meals(selected, user_request)
    return select_evidence_eligible_candidates(selected)


def _filter_semantic_area(places: list[Place], user_request: str) -> list[Place]:
    """Enforce a named semantic area only when POI text proves enough matches."""
    if any(term in user_request for term in ("不想跑远郊", "不去远郊", "别推荐远郊", "不要远郊")):
        central_by_city = {
            "北京": {"东城区", "西城区", "朝阳区", "海淀区", "丰台区", "石景山区"},
            "上海": {"黄浦区", "静安区", "徐汇区", "虹口区", "浦东新区", "长宁区", "普陀区", "杨浦区"},
            "杭州": {"上城区", "西湖区", "拱墅区", "滨江区"},
        }
        city = next((name for name in central_by_city if name in user_request), "")
        central = [place for place in places if place.district in central_by_city.get(city, set())]
        if len(central) >= 2:
            places = central
    if "市中心" in user_request:
        centre_by_city = {
            "北京": {"东城区", "西城区"},
            "上海": {"黄浦区", "静安区", "徐汇区", "长宁区"},
            "杭州": {"上城区", "西湖区", "拱墅区"},
        }
        city = next((name for name in centre_by_city if name in user_request), "")
        central = [place for place in places if place.district in centre_by_city.get(city, set())]
        if len(central) >= 2:
            places = central
    if "西湖边" not in user_request:
        return places
    direct_terms = ("西湖", "湖滨", "南山路", "龙翔桥", "孤山", "北山", "东坡路", "学士路")
    direct = [
        place for place in places
        if any(term in f"{place.name} {place.address}" for term in direct_terms)
    ]
    if len(direct) >= 2:
        return direct
    lake_districts = [
        place for place in places if place.district in {"西湖区", "上城区"}
    ]
    return lake_districts if len(lake_districts) >= 2 else places


def _filter_category_identity_conflicts(places: list[Place]) -> list[Place]:
    """Drop provider POIs whose visible identity contradicts the assigned category."""
    kept: list[Place] = []
    for place in places:
        if place.category == PlaceCategory.HOTEL and any(
            term in place.name for term in ("酒店大堂", "宾馆大堂", "饭店大堂")
        ):
            continue
        if place.category == PlaceCategory.FOOD:
            identity = f"{place.name} {' '.join(place.tags or [])}"
            non_food_terms = (
                "风景名胜区", "景区", "公园", "博物馆", "纪念馆", "展览馆",
                "动物园", "植物园", "故居", "寺院", "寺庙",
            )
            food_terms = (
                "餐厅", "饭店", "菜馆", "面馆", "小吃", "火锅", "烧烤",
                "烤肉", "米线", "馄饨", "饺子", "咖啡", "茶馆",
            )
            if any(term in place.name for term in non_food_terms) and not any(
                term in identity for term in food_terms
            ):
                continue
        kept.append(place)
    return kept


def _minimum_geo_distance(place: Place) -> float | None:
    distances = [
        item.straight_line_distance_km
        for item in place.geo_evidence
        if item.straight_line_distance_km is not None
    ]
    return min(distances) if distances else None


def _verified_route_minutes(place: Place) -> int | None:
    values = [
        item.estimated_travel_minutes
        for item in place.geo_evidence
        if item.status.value == "VERIFIED"
        and item.satisfies_constraint is not False
        and item.estimated_travel_minutes is not None
    ]
    return min(values) if values else None


def _filter_time_sensitive_hub_candidates(places: list[Place], user_request: str) -> list[Place]:
    """Use tight, evidence-backed bounds when a train/flight deadline is explicit."""
    hub_request = any(term in user_request for term in ("火车站", "高铁站", "南站", "虹桥站", "杭州东站"))
    early_departure = hub_request and any(term in user_request for term in ("六点", "七点", "赶车", "坐车"))
    short_layover = hub_request and any(term in user_request for term in ("两小时", "2小时", "误车", "换乘"))
    if not early_departure and not short_layover:
        return places

    kept_non_target: list[Place] = []
    foods: list[Place] = []
    attractions: list[Place] = []
    for place in places:
        if place.category == PlaceCategory.FOOD:
            foods.append(place)
        elif place.category == PlaceCategory.ATTRACTION:
            attractions.append(place)
        else:
            kept_non_target.append(place)

    safe_food = [
        place for place in foods
        if (_verified_route_minutes(place) is not None and _verified_route_minutes(place) <= 10)
        or (_minimum_geo_distance(place) is not None and _minimum_geo_distance(place) <= 1.5)
    ]
    if len(safe_food) >= 2:
        foods = safe_food
    if short_layover:
        safe_attractions = [
            place for place in attractions
            if (_verified_route_minutes(place) is not None and _verified_route_minutes(place) <= 20)
            or (_minimum_geo_distance(place) is not None and _minimum_geo_distance(place) <= 1.5)
        ]
        if safe_attractions:
            attractions = sorted(
                safe_attractions,
                key=lambda place: (
                    _verified_route_minutes(place) or 10**6,
                    _minimum_geo_distance(place) or 10**6,
                    -(place.amap_rating or 0.0),
                ),
            )[:1]
    return [*attractions, *foods, *kept_non_target]


def _filter_low_transfer_candidates(places: list[Place], user_request: str) -> list[Place]:
    """Remove remote portfolio candidates when the visitor explicitly needs low transfer."""
    low_transfer = any(term in user_request for term in ("老人", "少折腾", "少走路", "少步行"))
    long_stay = any(term in user_request for term in ("住一周", "住十天", "长住", "洗衣"))
    subway_hotel = any(term in user_request for term in ("靠地铁", "地铁方便", "交通方便的酒店"))
    if not low_transfer and not long_stay and not subway_hotel:
        return places
    core_by_city = {
        "北京": {"东城区", "西城区", "朝阳区", "海淀区", "丰台区"},
        "上海": {"黄浦区", "静安区", "徐汇区", "虹口区", "浦东新区", "长宁区", "普陀区"},
        "杭州": {"上城区", "西湖区", "拱墅区", "滨江区"},
    }
    city = next((name for name in core_by_city if name in user_request), "")
    if not city:
        observed_cities = [place.city for place in places if place.city in core_by_city]
        city = max(set(observed_cities), key=observed_cities.count) if observed_cities else ""
    core = core_by_city.get(city, set())
    if not core:
        return places
    portfolio_categories = {
        place.category for place in places
        if place.category in {PlaceCategory.ATTRACTION, PlaceCategory.FOOD, PlaceCategory.HOTEL}
    }
    if low_transfer and len(portfolio_categories) >= 3:
        remote_area_terms = {
            "北京": ("古北水镇", "古北口", "密云", "延庆", "怀柔"),
            "上海": ("周浦", "川沙", "南汇", "临港", "奉贤", "青浦"),
            "杭州": ("九堡", "下沙", "萧山", "机场"),
        }[city]
        non_remote = [
            place for place in places
            if not any(term in f"{place.name} {place.address or ''}" for term in remote_area_terms)
        ]
        compact = _compact_three_category_portfolio(non_remote)
        if compact:
            places = compact
    categories = {place.category for place in places}
    filtered: list[Place] = []
    for category in categories:
        group = [place for place in places if place.category == category]
        central = [place for place in group if place.district in core]
        required_coverage = min(3, len(group))
        filtered.extend(central if len(central) >= required_coverage else group)
    if long_stay:
        hotels = [place for place in filtered if place.category == PlaceCategory.HOTEL]
        apartment = [
            place for place in hotels
            if any(term in f"{place.name} {' '.join(place.tags or [])}" for term in ("公寓", "套房", "长租"))
        ]
        if len(apartment) >= 2:
            filtered = [place for place in filtered if place.category != PlaceCategory.HOTEL] + apartment
    return filtered


def _compact_three_category_portfolio(places: list[Place]) -> list[Place]:
    """Choose a small, coordinate-compact 景餐住 portfolio for low-transfer trips."""
    categories = (PlaceCategory.ATTRACTION, PlaceCategory.FOOD, PlaceCategory.HOTEL)
    by_category = {category: [p for p in places if p.category == category] for category in categories}
    if any(len(group) < 2 for group in by_category.values()):
        return []
    core_result = _low_transfer_core(places)
    if core_result is None:
        return []
    core, _ = core_result

    # Preserve one alternative per category, but optimise the promised primary
    # combination first.  The earlier centre-radius heuristic could choose six
    # individually close cards while leaving the best 景餐住 triplet farther
    # apart than another triplet already present in the same provider pool.
    chosen = list(core)
    for primary in core:
        alternatives = [
            place for place in by_category[primary.category]
            if place.place_id != primary.place_id
        ]

        def alternative_score(place: Place) -> tuple[float, float, float, str]:
            distances = [coordinate_distance_meters(place, member) for member in core]
            if any(distance is None for distance in distances):
                return (10**12, 10**12, 0.0, place.place_id)
            values = [float(distance) for distance in distances if distance is not None]
            return (max(values), sum(values), -(place.amap_rating or 0.0), place.place_id)

        chosen.append(min(alternatives, key=alternative_score))

    anchor = core[0]
    evidenced: list[Place] = []
    for place in chosen:
        distance = coordinate_distance_meters(anchor, place)
        if distance is None:
            return []
        distance_km = round(distance / 1000, 3)
        compactness = GeoEvidence(
            slot_id="portfolio-low-transfer",
            anchor_place=anchor.name,
            constraint_kind="portfolio_compactness",
            status=EvidenceStatus.VERIFIED,
            satisfies_constraint=None,
            straight_line_distance_km=distance_km,
            transport_mode="driving",
            source="amap_coordinates_portfolio",
            observed_at=place.retrieval_observed_at,
        )
        route_check = GeoEvidence(
            slot_id="portfolio-low-transfer",
            anchor_place=anchor.name,
            constraint_kind="portfolio_route",
            status=EvidenceStatus.UNKNOWN,
            satisfies_constraint=None,
            straight_line_distance_km=distance_km,
            transport_mode="driving",
            source="route_time_not_queried",
            observed_at=place.retrieval_observed_at,
            failure_reason="route_time_not_queried",
        )
        existing = [
            item for item in place.geo_evidence
            if item.constraint_kind not in {"portfolio_compactness", "portfolio_route"}
        ]
        evidenced.append(finalize_place_evidence(place.model_copy(
            update={"geo_evidence": [*existing, compactness, route_check]},
        )))
    return evidenced


def _dedupe_spatially_redundant_attractions(
    places: list[Place],
    user_request: str,
) -> list[Place]:
    """Avoid spending a first-visit multi-day portfolio on the same precinct twice."""
    first_visit = any(term in user_request for term in ("第一次", "首次"))
    multi_day = any(term in user_request for term in ("两天", "三天", "四天", "多日"))
    categories = {
        place.category for place in places
        if place.category in {PlaceCategory.ATTRACTION, PlaceCategory.FOOD, PlaceCategory.HOTEL}
    }
    if not (first_visit and multi_day and len(categories) >= 3):
        return places

    kept_attractions: list[Place] = []
    result: list[Place] = []
    for place in places:
        if place.category != PlaceCategory.ATTRACTION:
            result.append(place)
            continue
        redundant = any(
            distance is not None and distance <= 750
            for kept in kept_attractions
            for distance in (coordinate_distance_meters(place, kept),)
        )
        if redundant:
            continue
        kept_attractions.append(place)
        result.append(place)
    return result


def _low_transfer_core(places: list[Place]) -> tuple[list[Place], float] | None:
    """Select one 景餐住 triplet by its worst pairwise straight-line edge."""
    groups = [
        [place for place in places if place.category == category]
        for category in (PlaceCategory.ATTRACTION, PlaceCategory.FOOD, PlaceCategory.HOTEL)
    ]
    if any(not group for group in groups):
        return None
    best: tuple[tuple[float, float, float], list[Place]] | None = None
    for triplet in product(*groups):
        distances = [
            coordinate_distance_meters(left, right)
            for left, right in combinations(triplet, 2)
        ]
        if any(distance is None for distance in distances):
            continue
        values = [float(distance) for distance in distances if distance is not None]
        rating = sum(place.amap_rating or 0.0 for place in triplet)
        score = (max(values), sum(values), -rating)
        if best is None or score < best[0]:
            best = (score, list(triplet))
    return (best[1], round(best[0][0] / 1000, 3)) if best else None


def _attach_low_transfer_core_evidence(places: list[Place], user_request: str) -> list[Place]:
    if not any(term in user_request for term in ("老人", "少走路", "少折腾", "低强度")):
        return places
    cleaned: dict[str, Place] = {}
    for place in places:
        geo = [
            item for item in place.geo_evidence
            if not item.constraint_kind.startswith("low_transfer_core_")
        ]
        cleaned[place.place_id] = finalize_place_evidence(place.model_copy(
            update={"geo_evidence": geo},
        ))
    core_result = _low_transfer_core(list(cleaned.values()))
    if core_result is None:
        return [cleaned[place.place_id] for place in places]
    core, _ = core_result
    for left, right in combinations(core, 2):
        distance = coordinate_distance_meters(left, right)
        if distance is None:
            continue
        current = cleaned[right.place_id]
        distance_km = round(distance / 1000, 3)
        proximity = GeoEvidence(
            slot_id="low-transfer-core",
            anchor_place=left.name,
            constraint_kind="low_transfer_core_proximity",
            status=EvidenceStatus.VERIFIED,
            satisfies_constraint=None,
            straight_line_distance_km=distance_km,
            transport_mode="driving",
            source="amap_delivered_poi_coordinates",
            observed_at=current.retrieval_observed_at,
        )
        route = GeoEvidence(
            slot_id="low-transfer-core",
            anchor_place=left.name,
            constraint_kind="low_transfer_core_route",
            status=EvidenceStatus.UNKNOWN,
            satisfies_constraint=None,
            straight_line_distance_km=distance_km,
            transport_mode="driving",
            source="route_time_not_queried",
            observed_at=current.retrieval_observed_at,
            failure_reason="route_time_not_queried",
        )
        cleaned[right.place_id] = finalize_place_evidence(current.model_copy(
            update={"geo_evidence": [*current.geo_evidence, proximity, route]},
        ))
    return [cleaned[place.place_id] for place in places]


def _filter_named_area_candidates(places: list[Place], user_request: str) -> list[Place]:
    """Keep named three-city areas bounded when the frozen pool has enough direct evidence."""
    rules = (
        (
            "什刹海",
            PlaceCategory.FOOD,
            ("什刹海", "后海", "地安门", "鼓楼", "烟袋斜街"),
        ),
        (
            "运河",
            PlaceCategory.ATTRACTION,
            ("运河", "大兜路", "拱宸桥", "桥西", "小河直街", "香积寺"),
        ),
    )
    for trigger, category, terms in rules:
        if trigger not in user_request:
            continue
        matched = [
            place for place in places
            if place.category == category
            and any(term in f"{place.name} {place.address or ''}" for term in terms)
        ]
        if len(matched) >= 2:
            if trigger == "什刹海":
                anchor = {"coords": {"lng": 116.385121, "lat": 39.941893}}
                evidenced: list[Place] = []
                for place in matched:
                    distance = coordinate_distance_meters(anchor, place)
                    if distance is None:
                        evidenced.append(place)
                        continue
                    area_evidence = GeoEvidence(
                        slot_id="named-area-shichahai",
                        anchor_place="什刹海",
                        constraint_kind="named_area_proximity",
                        status=EvidenceStatus.VERIFIED,
                        satisfies_constraint=distance <= 3000,
                        straight_line_distance_km=round(distance / 1000, 3),
                        transport_mode="walking",
                        source="rc1_three_city_anchor_coordinates",
                        observed_at=place.retrieval_observed_at,
                    )
                    evidenced.append(finalize_place_evidence(place.model_copy(
                        update={"geo_evidence": [*place.geo_evidence, area_evidence]},
                    )))
                matched = evidenced
            places = [place for place in places if place.category != category] + matched
    return places


def _attach_shared_anchor_evidence(places: list[Place], user_request: str) -> list[Place]:
    """Bind nearby meal cards to a delivered landmark when the plan omitted that edge."""
    if not any(term in user_request for term in (
        "就近", "附近", "看完", "再吃", "顺便吃", "吃饭", "吃顿", "吃居民", "小馆", "少换乘",
    )):
        return places
    attractions = [place for place in places if place.category == PlaceCategory.ATTRACTION]
    foods = []
    for place in places:
        if place.category != PlaceCategory.FOOD:
            continue
        # Snapshot refreshes may legitimately reuse provider candidates that
        # already passed through an older selection pipeline.  Shared-anchor
        # facts are selection-relative, so always discard and recompute them
        # against the final delivered attraction set.  finalize_place_evidence
        # also removes confirmation actions produced only by the stale edge.
        current_geo = [
            item for item in place.geo_evidence
            if not item.constraint_kind.startswith("shared_anchor")
        ]
        foods.append(finalize_place_evidence(place.model_copy(
            update={"geo_evidence": current_geo},
        )))
    if not attractions or not foods:
        return places
    ordered_groups = extract_landmark_groups(user_request)
    last_ordered_aliases = ordered_groups[-1][1] if len(ordered_groups) >= 2 else ()
    anchor = next(
        (
            place for place in attractions
            if any(alias in place.name or place.name in alias for alias in last_ordered_aliases)
        ),
        None,
    )
    if anchor is None:
        anchor = next(
            (
                place for place in attractions
                if any(term in user_request and term in place.name for term in ("西湖", "外滩", "故宫", "科技馆", "动物园"))
            ),
            None,
        )
    if anchor is None:
        # For an open "visit, then eat" request, select the delivered
        # attraction with the closest delivered meal candidate as the decision
        # anchor.  This does not claim route feasibility; it only makes the
        # coordinate relationship explicit and auditable.
        scored_anchors = []
        for attraction in attractions:
            distances = [
                distance for food in foods
                if (distance := coordinate_distance_meters(attraction, food)) is not None
            ]
            if distances:
                scored_anchors.append((min(distances), attraction.name, attraction))
        anchor = min(scored_anchors, default=(None, None, None))[2]
    if anchor is None:
        return places
    evidenced_foods: list[Place] = []
    for place in foods:
        if any(item.anchor_place == anchor.name for item in place.geo_evidence):
            evidenced_foods.append(place)
            continue
        distance = coordinate_distance_meters(anchor, place)
        if distance is None:
            evidenced_foods.append(place)
            continue
        proximity = GeoEvidence(
            slot_id="shared-delivered-anchor",
            anchor_place=anchor.name,
            constraint_kind="shared_anchor_proximity",
            status=EvidenceStatus.VERIFIED,
            # A coordinate distance is verified, but the open-language request
            # does not define a universal pass radius.  Keep the fact separate
            # from the user's final route decision instead of dropping a card.
            satisfies_constraint=None,
            straight_line_distance_km=round(distance / 1000, 3),
            transport_mode="walking",
            source="amap_delivered_poi_coordinates",
            observed_at=place.retrieval_observed_at,
        )
        route = GeoEvidence(
            slot_id="shared-delivered-anchor",
            anchor_place=anchor.name,
            constraint_kind="shared_anchor_route",
            status=EvidenceStatus.UNKNOWN,
            satisfies_constraint=None,
            straight_line_distance_km=round(distance / 1000, 3),
            transport_mode="walking",
            source="route_time_not_queried",
            observed_at=place.retrieval_observed_at,
            failure_reason="route_time_not_queried",
        )
        evidenced_foods.append(finalize_place_evidence(place.model_copy(
            update={"geo_evidence": [*place.geo_evidence, proximity, route]},
        )))
    evidenced_foods.sort(key=lambda place: _minimum_geo_distance(place) or 10**6)
    return [place for place in places if place.category != PlaceCategory.FOOD] + evidenced_foods


def _explicit_multi_attraction_request(user_request: str) -> bool:
    quantity = re.search(r"(?:两个|两处|2个|2处|两三个|两三)(?:[^，。]{0,8})(?:场馆|景点|地方|去处)", user_request)
    return bool(quantity or len(extract_landmark_groups(user_request)) >= 2)


def _attach_delivered_attraction_evidence(places: list[Place], user_request: str) -> list[Place]:
    """Expose coordinate facts between every explicitly required delivered attraction.

    A two-place request is not a primary/backup recommendation.  The straight-line
    edge is a verified POI-coordinate fact; actual route time remains UNKNOWN and
    therefore creates a confirmation action instead of a route claim.
    """
    attractions: list[Place] = []
    for place in places:
        if place.category != PlaceCategory.ATTRACTION:
            continue
        current_geo = [
            item for item in place.geo_evidence
            if not item.constraint_kind.startswith("delivered_attraction_")
        ]
        attractions.append(finalize_place_evidence(place.model_copy(
            update={"geo_evidence": current_geo},
        )))
    if len(attractions) < 2 or not _explicit_multi_attraction_request(user_request):
        return [
            next((item for item in attractions if item.place_id == place.place_id), place)
            if place.category == PlaceCategory.ATTRACTION else place
            for place in places
        ]

    ordered_groups = extract_landmark_groups(user_request)
    ordered: list[Place] = []
    if len(ordered_groups) >= 2:
        for _, aliases in ordered_groups:
            matched = next((
                place for place in attractions
                if place not in ordered
                and any(alias in place.name or place.name in alias for alias in aliases)
            ), None)
            if matched is not None:
                ordered.append(matched)
    if len(ordered) < 2:
        ordered = attractions[:2]

    evidenced_by_id = {place.place_id: place for place in attractions}
    previous = ordered[0]
    for index, current in enumerate(ordered[1:], start=2):
        distance = coordinate_distance_meters(previous, current)
        if distance is None:
            previous = current
            continue
        distance_km = round(distance / 1000, 3)
        proximity = GeoEvidence(
            slot_id=f"delivered-attraction-{index:02d}",
            anchor_place=previous.name,
            constraint_kind="delivered_attraction_proximity",
            status=EvidenceStatus.VERIFIED,
            satisfies_constraint=None,
            straight_line_distance_km=distance_km,
            transport_mode="walking",
            source="amap_delivered_poi_coordinates",
            observed_at=current.retrieval_observed_at,
        )
        route = GeoEvidence(
            slot_id=f"delivered-attraction-{index:02d}",
            anchor_place=previous.name,
            constraint_kind="delivered_attraction_route",
            status=EvidenceStatus.UNKNOWN,
            satisfies_constraint=None,
            straight_line_distance_km=distance_km,
            transport_mode="walking",
            source="route_time_not_queried",
            observed_at=current.retrieval_observed_at,
            failure_reason="route_time_not_queried",
        )
        evidenced_by_id[current.place_id] = finalize_place_evidence(current.model_copy(
            update={"geo_evidence": [*current.geo_evidence, proximity, route]},
        ))
        previous = current
    return [evidenced_by_id.get(place.place_id, place) for place in places]


def _drop_obviously_remote_meals(places: list[Place], user_request: str) -> list[Place]:
    """Fail closed when every meal is plainly remote from a requested nearby anchor."""
    nearby_requested = any(term in user_request for term in ("附近", "就近")) or bool(
        re.search(r"看完[^，。]{0,12}(?:想吃|吃点|夜宵)", user_request)
    )
    if not nearby_requested:
        return places
    foods = [place for place in places if place.category == PlaceCategory.FOOD]
    if not foods:
        return places
    distances = {
        place.place_id: min(
            (
                item.straight_line_distance_km
                for item in place.geo_evidence
                if item.constraint_kind == "shared_anchor_proximity"
                and item.straight_line_distance_km is not None
            ),
            default=None,
        )
        for place in foods
    }
    measured = [distance for distance in distances.values() if distance is not None]
    if measured and min(measured) > 4.0:
        return [place for place in places if place.category != PlaceCategory.FOOD]
    return places


def _filter_meal_identity_candidates(places: list[Place], user_request: str) -> list[Place]:
    """Do not use drink-only or tea-space POIs to fill an explicit meal slot."""
    if not any(term in user_request for term in ("早餐", "午饭", "午餐", "晚饭", "晚餐", "正餐", "客户")):
        return places
    foods = [place for place in places if place.category == PlaceCategory.FOOD]
    meal_terms = (
        "餐厅", "饭店", "菜馆", "火锅", "烧肉", "烤肉", "面", "粉", "粥", "饭",
        "小吃", "生煎", "小笼", "馄饨", "饺", "汉堡", "披萨", "串串", "料理",
    )
    drink_only_terms = ("茶空间", "书茶院", "茶座", "奶茶", "茶饮", "咖啡馆", "咖啡店")
    suitable = []
    for place in foods:
        identity = f"{place.name} {' '.join(place.tags or [])}"
        if any(term in identity for term in drink_only_terms) and not any(term in identity for term in meal_terms):
            continue
        suitable.append(place)
    if len(suitable) >= 2:
        return [place for place in places if place.category != PlaceCategory.FOOD] + suitable
    return places


def _filter_local_snack_candidates(places: list[Place], user_request: str) -> list[Place]:
    if "小吃" not in user_request:
        return places
    terms_by_city = {
        "北京": ("小吃", "豆汁", "炒肝", "卤煮", "炸酱面", "门钉", "爆肚", "糖火烧", "豌豆黄"),
        "上海": ("小吃", "馄饨", "生煎", "小笼", "葱油", "锅贴", "面馆", "豆腐花", "上海菜"),
        "杭州": ("小吃", "片儿川", "杭州面", "葱包", "定胜糕", "小笼", "面馆"),
    }
    city = next((name for name in terms_by_city if name in user_request), "")
    terms = terms_by_city.get(city, ("小吃",))
    foods = [place for place in places if place.category == PlaceCategory.FOOD]
    other_region_terms = {
        "北京": ("重庆", "上海", "杭州", "广州", "东北"),
        "上海": ("无锡", "重庆", "北京", "杭州", "贵州", "东北"),
        "杭州": ("重庆", "北京", "上海", "贵州", "东北"),
    }.get(city, ())
    matched = []
    for place in foods:
        identity = f"{place.name} {' '.join(place.tags or [])}"
        if any(term in place.name for term in other_region_terms):
            continue
        if not any(term in identity for term in terms):
            continue
        if any(term in place.name for term in ("火锅", "涮肉", "烤肉", "海鲜", "酒楼")) and not any(
            term in place.name for term in terms
        ):
            continue
        matched.append(place)
    if len(matched) >= 2:
        return [place for place in places if place.category != PlaceCategory.FOOD] + matched
    return places


def _filter_city_local_food_candidates(places: list[Place], user_request: str) -> list[Place]:
    """Prefer local food identities for explicit local-cuisine or city-night-food requests."""
    city = next((name for name in ("北京", "上海", "杭州") if name in user_request), "")
    triggers = ("本地菜", f"{city}小吃", f"{city}夜宵", f"{city}菜")
    if not city or not any(trigger in user_request for trigger in triggers if trigger != city):
        return places
    terms_by_city = {
        "北京": ("老北京", "北京菜", "京味", "炸酱面", "卤煮", "豆汁", "炒肝", "爆肚", "烤鸭", "护国寺"),
        "上海": ("老上海", "上海菜", "上海小吃", "本帮", "生煎", "小笼", "馄饨", "葱油", "黄鱼面", "粥面"),
        "杭州": ("杭州菜", "杭州小吃", "杭帮", "片儿川", "葱包", "定胜糕", "杭州面", "江南"),
    }
    other_region_terms = {
        "北京": ("重庆", "上海", "杭州", "广州", "贵州"),
        "上海": ("重庆", "无锡", "北京", "杭州", "贵州"),
        "杭州": ("重庆", "北京", "上海", "贵州", "东北"),
    }[city]
    foods = [place for place in places if place.category == PlaceCategory.FOOD]
    matched = [
        place for place in foods
        if any(term in f"{place.name} {' '.join(place.tags or [])}" for term in terms_by_city[city])
        and not any(term in place.name for term in other_region_terms)
    ]
    neutral = [
        place for place in foods
        if place not in matched and not any(term in place.name for term in other_region_terms)
    ]
    target = 3 if len(foods) >= 3 else len(foods)
    selected = [*matched, *neutral[:max(0, target - len(matched))]]
    if len(selected) >= 2:
        return [place for place in places if place.category != PlaceCategory.FOOD] + selected
    return places


def _filter_by_requested_open_time(places: list[Place], user_request: str) -> list[Place]:
    """Drop POIs whose provider hours explicitly contradict a requested time."""
    target: int | None = None
    target_categories: set[PlaceCategory] = {PlaceCategory.FOOD}
    if "六点" in user_request and "早餐" in user_request:
        target = 6 * 60
    elif "七点" in user_request and "早餐" in user_request:
        target = 7 * 60
    elif "早餐" in user_request:
        target = 9 * 60
    elif any(term in user_request for term in ("午餐", "午饭", "中午吃", "客户午餐")):
        target = 12 * 60 + 30
    elif "十点半" in user_request or "22:30" in user_request:
        target = 22 * 60 + 30
    elif "日出" in user_request or "晨光" in user_request:
        target = 6 * 60
        target_categories = {PlaceCategory.ATTRACTION}
    elif any(term in user_request for term in ("夜景散步", "晚上散步", "夜间散步", "夜景")):
        target = 20 * 60
        target_categories = {PlaceCategory.ATTRACTION}
    if target is None:
        return places

    matching: list[Place] = []
    unknown: list[Place] = []
    for place in places:
        if place.category not in target_categories:
            matching.append(place)
            continue
        verdict = _hours_include(place.opening_hours or "", target)
        if verdict is True:
            matching.append(place)
        elif verdict is None:
            unknown.append(place)
    known_target = sum(place.category in target_categories for place in matching)
    if known_target == 0:
        # With no positive provider-hours evidence, preserve the full unknown
        # pool for later identity and evidence filters instead of making input
        # order decide which candidates survive.
        return matching + unknown
    return matching + unknown[:max(0, 2 - known_target)]


def _hours_include(raw: str, target: int) -> bool | None:
    if "24小时" in raw:
        return True
    windows = re.findall(r"(\d{1,2}):(\d{2})\s*[-—至]\s*(\d{1,2}):(\d{2})", raw)
    if not windows:
        return None
    for start_h, start_m, end_h, end_m in windows:
        start = int(start_h) * 60 + int(start_m)
        end = int(end_h) * 60 + int(end_m)
        if end <= start:
            end += 24 * 60
        point = target + (24 * 60 if target < start and end > 24 * 60 else 0)
        if start <= point <= end:
            return True
    return False


def _filter_exact_entity_slots(places: list[Place], recommendation_plan: Any) -> list[Place]:
    """Reject broad Amap siblings from a slot compiled for an exact entity."""
    if not recommendation_plan:
        return places
    slots = (
        recommendation_plan.slots
        if hasattr(recommendation_plan, "slots")
        else recommendation_plan.get("slots", [])
    )
    by_id = {
        (slot.slot_id if hasattr(slot, "slot_id") else slot.get("slot_id")): slot
        for slot in slots
    }
    kept: list[Place] = []
    for place in places:
        attached = [by_id.get(slot_id) for slot_id in (place.recommendation_slot_ids or [])]
        attached = [slot for slot in attached if slot is not None]
        bounded = [
            slot for slot in attached
            if (
                slot.provider_match_aliases
                if hasattr(slot, "provider_match_aliases")
                else slot.get("provider_match_aliases", [])
            )
        ]
        if not bounded:
            kept.append(place)
            continue
        compact_values = [
            re.sub(r"\s+", "", value).lower()
            for value in [place.name or "", *(place.canonical_entity_names or [])]
            if value
        ]
        if any(
            any(
                re.sub(r"\s+", "", str(alias)).lower() in compact
                or compact in re.sub(r"\s+", "", str(alias)).lower()
                for compact in compact_values
                for alias in (
                    slot.provider_match_aliases if hasattr(slot, "provider_match_aliases")
                    else slot.get("provider_match_aliases", [])
                )
            )
            for slot in bounded
        ):
            kept.append(place)
    return kept
