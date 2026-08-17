"""Declarative city/entity provider-query registry; contains data, not flow logic."""

from __future__ import annotations


_ENTITY_PROVIDER_QUERIES: dict[tuple[str, str], str] = {
    ("上海", "上海中心大厦"): "上海中心大厦观光厅",
}

# Human-facing area names are not always POI names.  Keep this mapping closed
# and city-scoped so an around-search anchor is resolved through a stable POI
# query instead of feeding arbitrary prose to the provider.
_GEO_ANCHOR_PROVIDER_QUERIES: dict[tuple[str, str], str] = {
    ("北京", "二环内"): "天安门广场",
    ("上海", "法租界"): "武康路历史文化名街",
    ("杭州", "湖滨"): "杭州湖滨步行街",
}

# Bounded visitor destinations for a short airport layover. These are only
# provider queries, never static recommendations: Amap must resolve each POI
# live and the route evidence layer must still prove the travel-time contract.
_AIRPORT_LAYOVER_DESTINATIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("北京", "首都机场"): ("民航博物馆", "罗红摄影艺术馆"),
    ("杭州", "萧山机场"): ("萧山博物馆", "浙东运河萧山展示馆"),
}


def provider_query_for_entity(city: str, canonical_entity: str) -> str:
    normalized_city = (city or "").removesuffix("市")
    return _ENTITY_PROVIDER_QUERIES.get(
        (normalized_city, canonical_entity),
        canonical_entity,
    )


def provider_query_for_geo_anchor(city: str, anchor: str) -> str:
    normalized_city = (city or "").removesuffix("市")
    return _GEO_ANCHOR_PROVIDER_QUERIES.get(
        (normalized_city, anchor),
        anchor,
    )


def airport_layover_destinations(city: str, anchor: str) -> list[str]:
    normalized_city = (city or "").removesuffix("市")
    return list(_AIRPORT_LAYOVER_DESTINATIONS.get((normalized_city, anchor), ()))
