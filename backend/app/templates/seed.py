"""Explicitly-labelled model-generated route-skeleton drafts.

They make the three-city UI testable, but must not be presented as the human
reviewed template corpus required by the product evidence gate.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.schemas.place import Coordinates, Place, PlaceCategory, PlaceSource
from app.templates.models import (
    AlternativeGroup,
    AnchorSlot,
    CityRouteTemplate,
    RouteZone,
    SourceReference,
    TemplateProvenance,
    TemplateStatus,
)


_CITY_ZONES = {
    "北京": ((116.397, 39.908), (116.403, 39.924)),
    "上海": ((121.490, 31.233), (121.474, 31.230)),
    "杭州": ((120.155, 30.274), (120.145, 30.230)),
}
_KINDS = (
    ("classic", "首次到访经典路线", "medium"),
    ("history", "历史文化路线", "medium"),
    ("family", "亲子与室内路线", "low"),
    ("walk", "城市漫步路线", "medium"),
    ("low-energy", "低体力或雨天替代路线", "low"),
)


def model_generated_template_drafts() -> list[CityRouteTemplate]:
    """Return 15 deterministic drafts (3 cities × 5), with honest provenance."""
    generated_at = datetime(2026, 8, 20, tzinfo=timezone.utc)
    result: list[CityRouteTemplate] = []
    for city, points in _CITY_ZONES.items():
        for slug, label, intensity in _KINDS:
            zones = [
                RouteZone(zone_id=f"{city}-{slug}-zone-a", city=city, district="核心片区", center=Coordinates(lng=points[0][0], lat=points[0][1])),
                RouteZone(zone_id=f"{city}-{slug}-zone-b", city=city, district="相邻片区", center=Coordinates(lng=points[1][0], lat=points[1][1])),
            ]
            group = AlternativeGroup(
                group_id=f"{city}-{slug}-food", label="运行时餐饮替代组", category="food", zone_id=zones[0].zone_id,
            )
            # These are intentionally *not* real POIs.  They only provide a
            # deterministic geographic projection for the local DRAFT flow,
            # and their identifiers/names make that boundary visible even if a
            # caller reads a serialized template outside the UI.
            anchor_places = [
                Place(
                    place_id=f"model-draft:{city}:{slug}:d0-a",
                    name=f"{city}{label}合成锚点 A",
                    category=PlaceCategory.ATTRACTION,
                    address="模型生成草稿坐标，仅用于本地路线投影",
                    coords=zones[0].center,
                    city=city,
                    source=PlaceSource.SYNTHESIZED,
                    description="GPT-5.6-sol 合成模板锚点，不是已核验 POI。",
                    tags=["MODEL_GENERATED_DRAFT", "SYNTHETIC_ROUTE_ANCHOR"],
                ),
                Place(
                    place_id=f"model-draft:{city}:{slug}:d0-b",
                    name=f"{city}{label}合成锚点 B",
                    category=PlaceCategory.ATTRACTION,
                    address="模型生成草稿坐标，仅用于本地路线投影",
                    coords=zones[1].center,
                    city=city,
                    source=PlaceSource.SYNTHESIZED,
                    description="GPT-5.6-sol 合成模板锚点，不是已核验 POI。",
                    tags=["MODEL_GENERATED_DRAFT", "SYNTHETIC_ROUTE_ANCHOR"],
                ),
                Place(
                    place_id=f"model-draft:{city}:{slug}:d1-a",
                    name=f"{city}{label}合成锚点 C",
                    category=PlaceCategory.ATTRACTION,
                    address="模型生成草稿坐标，仅用于本地路线投影",
                    coords=zones[1].center,
                    city=city,
                    source=PlaceSource.SYNTHESIZED,
                    description="GPT-5.6-sol 合成模板锚点，不是已核验 POI。",
                    tags=["MODEL_GENERATED_DRAFT", "SYNTHETIC_ROUTE_ANCHOR"],
                ),
                Place(
                    place_id=f"model-draft:{city}:{slug}:d1-b",
                    name=f"{city}{label}合成锚点 D",
                    category=PlaceCategory.ATTRACTION,
                    address="模型生成草稿坐标，仅用于本地路线投影",
                    coords=zones[0].center,
                    city=city,
                    source=PlaceSource.SYNTHESIZED,
                    description="GPT-5.6-sol 合成模板锚点，不是已核验 POI。",
                    tags=["MODEL_GENERATED_DRAFT", "SYNTHETIC_ROUTE_ANCHOR"],
                ),
            ]
            result.append(CityRouteTemplate(
                template_id=f"seed-{city}-{slug}-v1",
                city=city,
                name=f"{city}{label}",
                template_version=1,
                suitable_days=[2, 3, 4, 5],
                suitable_groups=["friends", "couple", "family"],
                intensity=intensity,
                route_zones=zones,
                anchor_slots=[
                    AnchorSlot(slot_id=f"{city}-{slug}-d0-am", day_offset=0, time_window="09:00-11:00", zone_id=zones[0].zone_id, slot_type="ATTRACTION", category_constraints=["attraction"], anchor_place_ids=[anchor_places[0].place_id], dwell_minutes=120),
                    AnchorSlot(slot_id=f"{city}-{slug}-d0-pm", day_offset=0, time_window="14:00-16:00", zone_id=zones[1].zone_id, slot_type="ATTRACTION", category_constraints=["attraction"], anchor_place_ids=[anchor_places[1].place_id], dwell_minutes=120),
                    AnchorSlot(slot_id=f"{city}-{slug}-d0-food", day_offset=0, time_window="12:00-13:00", zone_id=zones[0].zone_id, slot_type="FOOD", category_constraints=["food"], alternative_group_id=group.group_id, optional=True, dwell_minutes=60),
                    AnchorSlot(slot_id=f"{city}-{slug}-d1-am", day_offset=1, time_window="09:00-11:00", zone_id=zones[1].zone_id, slot_type="ATTRACTION", category_constraints=["attraction"], anchor_place_ids=[anchor_places[2].place_id], dwell_minutes=120),
                    AnchorSlot(slot_id=f"{city}-{slug}-d1-pm", day_offset=1, time_window="14:00-16:00", zone_id=zones[0].zone_id, slot_type="ATTRACTION", category_constraints=["attraction"], anchor_place_ids=[anchor_places[3].place_id], dwell_minutes=120),
                ],
                anchor_places=anchor_places,
                alternative_groups=[group],
                hotel_area_rules=[{"zone_id": zones[0].zone_id, "rule": "score_against_all_day_boundaries"}],
                source_refs=[SourceReference(
                    label="GPT-5.6-sol model-generated route skeleton draft",
                    observed_at=generated_at,
                    provenance=TemplateProvenance.MODEL_GENERATED,
                    note="Synthetic planning seed; not a human fact review or public-source verification.",
                )],
                status=TemplateStatus.DRAFT,
                provenance=TemplateProvenance.MODEL_GENERATED,
            ))
    return result
