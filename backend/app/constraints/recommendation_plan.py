"""General slot planning and coverage verification for recommendation requests."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

from app.constraints.location import extract_district_constraint, extract_explicit_district_constraint
from app.constraints.amap_types import typecodes_for_category
from app.constraints.city_knowledge import provider_query_for_entity
from app.constraints.recommendation_intent import (
    build_category_search_plan,
    build_generic_category_query,
    build_place_search_queries,
    extract_landmark_groups,
    infer_requested_categories,
    is_closed_landmark_request,
    _representative_complements,
)
from app.schemas.place import Coordinates, PlaceCategory
from app.schemas.recommendation_plan import GeoConstraint, RecommendationPlan, RecommendationSlot


_CATEGORY_ORDER = (
    PlaceCategory.ATTRACTION,
    PlaceCategory.FOOD,
    PlaceCategory.HOTEL,
    PlaceCategory.TRANSPORT,
)
_CATEGORY_LABEL = {
    PlaceCategory.ATTRACTION: "景点",
    PlaceCategory.FOOD: "餐厅",
    PlaceCategory.HOTEL: "酒店",
    PlaceCategory.TRANSPORT: "交通枢纽",
}
_COUNT_PATTERNS = (
    (re.compile(r"(?:两个|2个|两处|2处|两家|2家)"), 2),
    (re.compile(r"(?:三个|3个|三处|3处|三家|3家|两三个|2-3个)"), 3),
)
_TRUSTED_GEO_ANCHORS = (
    # Transport hubs are location anchors, not result cards.  Keep this list
    # closed: arbitrary text before "附近/吃饭/住" is not an entity parser.
    "北京南站", "北京西站", "北京站", "首都机场", "大兴机场",
    "虹桥机场", "虹桥站", "浦东机场", "上海站",
    "萧山机场", "杭州东站", "杭州站",
    # Named commercial/visitor areas that Amap can resolve deterministically.
    "王府井", "国贸", "三里屯", "前门", "牛街", "陆家嘴", "人民广场",
    "南京西路", "南京东路", "徐家汇", "静安寺", "城隍庙", "七宝",
    "湖滨", "武林广场", "黄龙", "河坊街", "钱江新城", "龙井村",
    "二环内", "法租界",
)
_ANCHOR_DISTRICTS = {
    "北京南站": "丰台区", "北京西站": "丰台区", "北京站": "东城区",
    "首都机场": "顺义区", "大兴机场": "大兴区",
    "虹桥机场": "长宁区", "虹桥站": "闵行区", "浦东机场": "浦东新区",
    "上海站": "静安区", "萧山机场": "萧山区", "杭州东站": "上城区",
    "杭州站": "上城区",
}


def _minimum_for(text: str, category: PlaceCategory, compound: bool) -> int:
    category_terms = {
        PlaceCategory.ATTRACTION: ("景点", "地方", "场馆", "展"),
        PlaceCategory.FOOD: ("餐厅", "饭", "店", "正餐", "吃"),
        PlaceCategory.HOTEL: ("酒店", "住宿", "住"),
        PlaceCategory.TRANSPORT: ("交通", "枢纽", "车站"),
    }[category]
    for pattern, count in _COUNT_PATTERNS:
        match = pattern.search(text)
        if match and any(term in text[match.start():match.end() + 12] for term in category_terms):
            return count
    if category == PlaceCategory.ATTRACTION and compound and any(term in text for term in ("两三个", "几个", "一天")):
        return 2
    return 1


def _explicit_count_for(text: str, category: PlaceCategory) -> int | None:
    """Return a user-stated category count without inventing one for '几个'."""
    category_terms = {
        PlaceCategory.ATTRACTION: ("景点", "地方", "场馆", "展"),
        PlaceCategory.FOOD: ("餐厅", "饭", "店", "正餐", "吃"),
        PlaceCategory.HOTEL: ("酒店", "住宿", "住"),
        PlaceCategory.TRANSPORT: ("交通", "枢纽", "车站"),
    }[category]
    for pattern, count in _COUNT_PATTERNS:
        match = pattern.search(text)
        if match and any(term in text[match.start():match.end() + 12] for term in category_terms):
            return count
    return None


def _transport_contract(text: str) -> tuple[str, int | None]:
    if "转机" in text and "机场" in text:
        # A layover recommendation is evaluated against the airport by road,
        # not as an impossible multi-kilometre walking trip.
        return "driving", None
    if any(term in text for term in ("少换乘", "地铁", "公交", "不自驾", "公共交通")):
        return "transit", 1 if "少换乘" in text else None
    if any(term in text for term in ("打车", "驾车", "自驾", "开车")):
        return "driving", None
    return "walking", None


def _max_travel_minutes(text: str, anchor: str | None) -> int | None:
    if not anchor:
        return None
    transport_hubs = ("站", "机场")
    if "转机" in text and "机场" in anchor:
        return 30
    two_hours = any(term in text for term in ("两小时", "2小时", "两个小时"))
    if two_hours and any(term in anchor for term in transport_hubs):
        return 30
    if any(term in anchor for term in transport_hubs):
        return 20
    if "先" in text and any(term in text for term in ("再", "之后", "然后")):
        return 20
    if any(term in text for term in ("少换乘", "别跑远", "不跑远", "少折腾", "别跨城")):
        return 20
    # Generic landmark proximity is already a bounded coordinate-radius
    # contract. Requiring a second live route for every "附近" request
    # turns a verified short-distance match into UNKNOWN when route enrichment
    # is absent, although the user never requested a travel-time ceiling.
    return None


def _anchor_for(text: str, category: PlaceCategory, landmark_names: list[str]) -> str | None:
    spatial_cues = ("附近", "周边", "旁边", "一带", "就近")
    # Explicit transport/commercial entities may anchor either a nearby
    # attraction or a food/hotel slot. Administrative districts intentionally
    # stay out of this list: a district-only request uses district/city search
    # instead of inventing a point coordinate.
    hits = [
        (text.rfind(anchor), anchor)
        for anchor in _TRUSTED_GEO_ANCHORS
        if text.rfind(anchor) >= 0
    ]
    if hits:
        _, anchor = max(hits)
        suffix = text[text.rfind(anchor) + len(anchor):]
        category_actions = {
            PlaceCategory.ATTRACTION: ("看", "逛", "玩", "景点", "东西"),
            PlaceCategory.FOOD: ("吃", "用餐", "餐厅", "饭", "咖啡", "午餐", "夜宵", "客户"),
            PlaceCategory.HOTEL: ("住", "住宿", "酒店", "客房"),
            PlaceCategory.TRANSPORT: ("交通", "换乘", "车站", "机场"),
        }[category]
        semantic_area = anchor in {"二环内", "法租界"}
        business_commute = (
            category == PlaceCategory.HOTEL
            and "出差" in text
            and any(term in text for term in ("走路", "步行", "一站地铁", "通勤", "公司"))
        )
        if semantic_area or business_commute or any(term in suffix for term in (*spatial_cues, *category_actions)):
            return anchor
    # The landmark registry is an already-resolved, high-confidence entity
    # source.  A generic "附近" after a landmark refers to the last landmark,
    # even when connective prose sits between them ("798看展，想顺便找附近...").
    if landmark_names and (
        any(term in text for term in spatial_cues)
        or (
            category == PlaceCategory.ATTRACTION
            and any(term in text for term in ("老人", "七十", "七十五", "走不了", "少步行", "少爬坡"))
        )
    ):
        return landmark_names[-1]
    return None


def _radius_for(text: str, anchor: str | None) -> float | None:
    if not anchor:
        return None
    if anchor == "二环内":
        return 6.0
    if anchor == "法租界":
        return 4.0
    if "出差" in text and any(term in text for term in ("走路", "步行", "一站地铁", "通勤")):
        return 3.0
    return 3.0


def build_recommendation_plan(text: str, city: str, district: str = "") -> RecommendationPlan:
    categories = infer_requested_categories(text)
    landmarks = extract_landmark_groups(text)
    landmarks_are_destinations = (
        bool(landmarks) and (
            PlaceCategory.ATTRACTION in categories
            or (len(landmarks) >= 2 and is_closed_landmark_request(text, landmarks))
        )
    )
    if landmarks_are_destinations:
        categories.add(PlaceCategory.ATTRACTION)
    category_queries = build_category_search_plan(text, city, categories)
    district = district or extract_district_constraint(text) or ""
    explicit_district = extract_explicit_district_constraint(text) or ""
    slots: list[RecommendationSlot] = []
    order = 0

    # Recurrent open discovery intents need independent provider queries.  A
    # single keyword like "景山公园 鼓楼 观景" typically collapses to one POI,
    # while three bounded searches preserve diversity and audit each result.
    if categories == {PlaceCategory.ATTRACTION} and not landmarks:
        discovery_queries = build_place_search_queries(text, city)
        if len(discovery_queries) > 1:
            explicit_count = _explicit_count_for(text, PlaceCategory.ATTRACTION)
            if explicit_count is not None:
                discovery_queries = discovery_queries[:explicit_count]
            landmark_names: list[str] = []
            anchor = _anchor_for(text, PlaceCategory.ATTRACTION, landmark_names)
            transport_mode, max_transfers = _transport_contract(text)
            layover_radius_km = 25.0 if anchor and "机场" in anchor and "转机" in text else 3.0
            for query in discovery_queries:
                order += 1
                slots.append(RecommendationSlot(
                    slot_id=f"slot-{order:02d}",
                    category=PlaceCategory.ATTRACTION,
                    order=order,
                    min_results=1,
                    entity_name=query,
                    entity_aliases=[query],
                    query=query,
                    # These queries come from the bounded city destination
                    # registry. Keep exact-name relevance; category validation
                    # still rejects parking, retail and other same-name POIs.
                    provider_typecodes=[],
                    geo=GeoConstraint(
                        administrative_district=explicit_district or None,
                        anchor_place=anchor,
                        max_radius_km=layover_radius_km if anchor else None,
                        max_travel_minutes=_max_travel_minutes(text, anchor),
                        max_transfers=max_transfers if anchor else None,
                        transport_mode=transport_mode,
                    ),
                ))
            return RecommendationPlan(user_request=text, city=city, slots=slots)

    # Explicit destinations are independent slots, preserving user order.
    for canonical, aliases in landmarks if landmarks_are_destinations else []:
        order += 1
        slots.append(RecommendationSlot(
            slot_id=f"slot-{order:02d}",
            category=PlaceCategory.ATTRACTION,
            order=order,
            min_results=1,
            entity_name=canonical,
            entity_aliases=list(dict.fromkeys((canonical, *aliases))),
            query=provider_query_for_entity(city, canonical),
            # Amap v5 treats a broad type alongside an exact entity keyword as
            # an additional recall signal and may rank generic city highlights
            # ahead of the requested POI. Exact entity slots therefore search
            # by canonical keyword only; category/entity post-filters remain
            # fail-closed downstream.
            provider_typecodes=[],
            geo=GeoConstraint(administrative_district=explicit_district or None),
        ))

    # Open attraction discovery can also be one half of a compound request.
    # Preserve each bounded attraction query as its own slot, then let the
    # category loop add food/hotel slots below.
    open_attraction_queries = (
        category_queries.get(PlaceCategory.ATTRACTION, [])
        if PlaceCategory.ATTRACTION in categories and not landmarks_are_destinations
        else []
    )
    if len(open_attraction_queries) > 1:
        explicit_count = _explicit_count_for(text, PlaceCategory.ATTRACTION)
        bounded_attraction_queries = (
            open_attraction_queries[:explicit_count]
            if explicit_count is not None else open_attraction_queries
        )
        for query in bounded_attraction_queries:
            order += 1
            slots.append(RecommendationSlot(
                slot_id=f"slot-{order:02d}",
                category=PlaceCategory.ATTRACTION,
                order=order,
                min_results=1,
                provider_match_aliases=[query],
                query=query,
                provider_typecodes=[],
                geo=GeoConstraint(administrative_district=explicit_district or None),
            ))

    open_food_queries = category_queries.get(PlaceCategory.FOOD, [])
    if len(open_food_queries) > 1:
        food_transport_mode, food_max_transfers = _transport_contract(text)
        for query in open_food_queries:
            order += 1
            anchor = _anchor_for(text, PlaceCategory.FOOD, [canonical for canonical, _ in landmarks])
            if not anchor and open_attraction_queries and any(term in text for term in ("附近", "就近", "少换乘", "不跑远")):
                anchor = open_attraction_queries[0]
            slots.append(RecommendationSlot(
                slot_id=f"slot-{order:02d}",
                category=PlaceCategory.FOOD,
                order=order,
                min_results=1,
                query=f"{anchor}附近 {query}" if anchor else query,
                provider_typecodes=typecodes_for_category(PlaceCategory.FOOD),
                geo=GeoConstraint(
                    administrative_district=(
                        explicit_district or _ANCHOR_DISTRICTS.get(anchor or "") or district or None
                    ),
                    anchor_place=anchor,
                    max_radius_km=_radius_for(text, anchor),
                    max_travel_minutes=_max_travel_minutes(text, anchor),
                    max_transfers=food_max_transfers if anchor else None,
                    transport_mode=food_transport_mode,
                ),
            ))

    landmark_names = [canonical for canonical, _ in landmarks]
    complement_queries = (
        _representative_complements(f"{city} {text}", landmark_names)
        if landmarks_are_destinations and PlaceCategory.ATTRACTION in categories
        else []
    )
    for query in complement_queries:
        order += 1
        slots.append(RecommendationSlot(
            slot_id=f"slot-{order:02d}",
            category=PlaceCategory.ATTRACTION,
            order=order,
            min_results=1,
            entity_name=query,
            entity_aliases=[query],
            query=provider_query_for_entity(city, query),
            provider_typecodes=[],
            geo=GeoConstraint(administrative_district=explicit_district or None),
        ))
    transport_mode, max_transfers = _transport_contract(text)
    for category in _CATEGORY_ORDER:
        if category not in categories:
            continue
        if category == PlaceCategory.ATTRACTION and len(open_attraction_queries) > 1:
            continue
        if category == PlaceCategory.FOOD and len(open_food_queries) > 1:
            continue
        if (
            category == PlaceCategory.ATTRACTION
            and landmarks_are_destinations
            and (is_closed_landmark_request(text, landmarks) or complement_queries)
        ):
            # A closed ordered destination list needs no open discovery slot.
            continue
        order += 1
        query = (
            build_generic_category_query(text, city, category)
            if category == PlaceCategory.ATTRACTION and landmarks_are_destinations
            else (category_queries.get(category) or [_CATEGORY_LABEL[category]])[0]
        )
        anchor = _anchor_for(text, category, landmark_names)
        if (
            category == PlaceCategory.FOOD
            and not anchor
            and open_attraction_queries
            and any(term in text for term in ("附近", "就近", "少换乘", "不跑远"))
        ):
            anchor = open_attraction_queries[0]
        if anchor and "附近" not in query:
            # Keep the semantic constraint (for example 北京菜/素食/家庭房)
            # when adding the spatial anchor. Replacing it with the broad
            # category label causes retrieval to succeed while every result
            # is later rejected by the hard constraint filter.
            for _canonical, aliases in landmarks:
                for alias in aliases:
                    query = query.replace(alias, "")
            query = query.replace(anchor, "")
            query = re.sub(r"\s+", " ", query).strip()
            query = f"{anchor}附近 {query or _CATEGORY_LABEL[category]}"
        if anchor and category == PlaceCategory.ATTRACTION:
            # Around-search already carries a closed attraction typecode and
            # the original semantic request remains in post-filtering. Amap v5
            # often returns zero for compound nearby keywords such as
            # "文化景点 博物馆", while the bounded keyword "景点" preserves recall.
            query = "景点"
        slots.append(RecommendationSlot(
            slot_id=f"slot-{order:02d}",
            category=category,
            order=order,
            min_results=max(
                _minimum_for(text, category, len(categories) > 1),
                2 if category == PlaceCategory.ATTRACTION and landmarks_are_destinations else 1,
            ),
            query=query,
            provider_typecodes=typecodes_for_category(category),
            geo=GeoConstraint(
                administrative_district=(
                    explicit_district or _ANCHOR_DISTRICTS.get(anchor or "") or district or None
                ),
                anchor_place=anchor,
                max_radius_km=_radius_for(text, anchor),
                max_travel_minutes=_max_travel_minutes(text, anchor),
                max_transfers=max_transfers if anchor else None,
                transport_mode=transport_mode,
            ),
        ))

    return RecommendationPlan(user_request=text, city=city, slots=slots)


def bind_geo_anchor_evidence(
    plan: RecommendationPlan | dict,
    retrieval_audits: Iterable[dict[str, Any]],
) -> RecommendationPlan:
    """Bind resolved provider anchor coordinates to their plan slots.

    Around-search already used these coordinates. Persisting them in the plan
    keeps the evidence chain available after the anchor POI itself is omitted
    from the recommendation cards and during deterministic snapshot replay.
    """
    parsed = plan if isinstance(plan, RecommendationPlan) else RecommendationPlan.model_validate(plan)
    audits = list(retrieval_audits)
    bound_slots: list[RecommendationSlot] = []
    for slot in parsed.slots:
        if not slot.geo.anchor_place:
            bound_slots.append(slot)
            continue
        matched = next((
            audit for audit in reversed(audits)
            if audit.get("slot_id") == slot.slot_id
            and (audit.get("anchor_location") or audit.get("location"))
        ), None)
        if not matched:
            bound_slots.append(slot)
            continue
        raw_location = str(matched.get("anchor_location") or matched.get("location"))
        try:
            lng, lat = raw_location.split(",", 1)
            coords = Coordinates(lng=float(lng), lat=float(lat))
        except (TypeError, ValueError):
            bound_slots.append(slot)
            continue
        legacy_anchor_audit = next((
            audit for audit in reversed(audits)
            if audit.get("query") == f"anchor:{slot.geo.anchor_place}"
            and audit.get("response_hash")
        ), None)
        geo = slot.geo.__class__.model_validate({
            **slot.geo.model_dump(),
            "anchor_place_id": matched.get("anchor_place_id") or slot.geo.anchor_place_id,
            "anchor_coords": coords,
            "anchor_response_hash": (
                matched.get("anchor_response_hash")
                or (legacy_anchor_audit or {}).get("response_hash")
                or slot.geo.anchor_response_hash
            ),
            "anchor_observed_at": (
                matched.get("anchor_observed_at")
                or (legacy_anchor_audit or {}).get("retrieved_at")
                or matched.get("retrieved_at")
                or slot.geo.anchor_observed_at
            ),
        })
        bound_slots.append(slot.model_copy(update={"geo": geo}))
    return parsed.model_copy(update={"slots": bound_slots})


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def slot_coverage(plan: RecommendationPlan | dict, places: Iterable[Any]) -> dict[str, dict[str, Any]]:
    parsed = plan if isinstance(plan, RecommendationPlan) else RecommendationPlan.model_validate(plan)
    place_items = list(places)
    counts: Counter[str] = Counter()
    for place in place_items:
        for slot_id in _value(place, "recommendation_slot_ids", []) or []:
            counts[str(slot_id)] += 1
    # Backward-compatible inference for persisted/legacy candidates that do
    # not yet carry slot ids. New retrievals always use explicit provenance.
    for slot in parsed.slots:
        if counts[slot.slot_id]:
            continue
        for place in place_items:
            raw_category = _value(place, "category")
            category = getattr(raw_category, "value", raw_category)
            if category != slot.category.value:
                continue
            if slot.entity_name:
                compact_name = "".join(str(_value(place, "name", "")).lower().split())
                aliases = ["".join(alias.lower().split()) for alias in slot.entity_aliases]
                if not any(alias in compact_name for alias in aliases):
                    continue
            counts[slot.slot_id] += 1
    return {
        slot.slot_id: {
            "required": slot.min_results,
            "actual": counts[slot.slot_id],
            "satisfied": counts[slot.slot_id] >= slot.min_results,
        }
        for slot in parsed.slots
    }


def missing_slot_ids(plan: RecommendationPlan | dict, places: Iterable[Any]) -> list[str]:
    return [slot_id for slot_id, status in slot_coverage(plan, places).items() if not status["satisfied"]]


def order_places_by_plan(places: Iterable[Any], plan: RecommendationPlan | dict | None) -> list[Any]:
    items = list(places)
    if not plan:
        return items
    parsed = plan if isinstance(plan, RecommendationPlan) else RecommendationPlan.model_validate(plan)
    order = {slot.slot_id: slot.order for slot in parsed.slots}
    return sorted(items, key=lambda place: min(
        (order.get(slot_id, len(order) + 1) for slot_id in (_value(place, "recommendation_slot_ids", []) or [])),
        default=len(order) + 1,
    ))


def reserve_places_for_plan(places: Iterable[Any], plan: RecommendationPlan | dict | None) -> list[Any]:
    """Reserve every slot's minimum before general ranking/caps consume cards."""
    items = order_places_by_plan(places, plan)
    if not plan:
        return items
    parsed = plan if isinstance(plan, RecommendationPlan) else RecommendationPlan.model_validate(plan)
    selected: list[Any] = []
    selected_ids: set[int] = set()
    for slot in sorted(parsed.slots, key=lambda item: item.order):
        candidates = [
            place for place in items
            if slot.slot_id in (_value(place, "recommendation_slot_ids", []) or [])
            and id(place) not in selected_ids
        ]
        for place in candidates[:slot.min_results]:
            selected.append(place)
            selected_ids.add(id(place))
    selected.extend(place for place in items if id(place) not in selected_ids)
    return selected
