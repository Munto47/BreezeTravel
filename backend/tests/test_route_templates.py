from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.schemas.place import Coordinates, Place, PlaceCategory
from app.templates.models import (
    CandidateGate,
    CandidateTier,
    CityRouteTemplate,
    EvidenceFreshness,
    RouteEstimate,
    TemplateProvenance,
    TemplateStatus,
)
from app.templates.seed import model_generated_template_drafts
from app.templates.service import CandidateSuggestionService, HotelScoringService, StaticRouteEstimator


def _place(place_id: str, *, city: str = "北京") -> Place:
    return Place(
        place_id=place_id, name=place_id, category=PlaceCategory.ATTRACTION,
        address="测试地址", coords=Coordinates(lng=116.4, lat=39.9), city=city,
    )


def _edge(minutes: int | None, freshness: EvidenceFreshness = EvidenceFreshness.FRESH) -> RouteEstimate:
    return RouteEstimate(minutes=minutes, source="controlled_route", freshness=freshness, observed_at=datetime.now(timezone.utc))


def test_model_seed_has_three_city_five_drafts_and_never_claims_human_review():
    templates = model_generated_template_drafts()
    assert len(templates) == 15
    assert {template.city for template in templates} == {"北京", "上海", "杭州"}
    assert all(template.status is TemplateStatus.DRAFT for template in templates)
    assert all(template.provenance is TemplateProvenance.MODEL_GENERATED for template in templates)
    assert all(len(template.anchor_places) == 4 for template in templates)
    assert all(all(place.place_id.startswith("model-draft:") for place in template.anchor_places) for template in templates)
    assert all(all(place.source.value == "synthesized" for place in template.anchor_places) for template in templates)
    assert all(all(slot.anchor_place_ids for slot in template.anchor_slots if slot.slot_type == "ATTRACTION") for template in templates)
    assert all(not any("餐厅" in slot.slot_id for slot in template.anchor_slots) for template in templates)


def test_model_generated_template_cannot_be_laundered_to_reviewed():
    payload = model_generated_template_drafts()[0].model_dump()
    payload["status"] = TemplateStatus.REVIEWED
    with pytest.raises(ValueError, match="MODEL_GENERATED_TEMPLATE_REQUIRES_HUMAN_REVIEW"):
        CityRouteTemplate.model_validate(payload)


def test_reviewed_template_requires_readback_sources_and_verification_time():
    payload = model_generated_template_drafts()[0].model_dump()
    payload["provenance"] = TemplateProvenance.HUMAN_CURATED
    payload["status"] = TemplateStatus.REVIEWED
    with pytest.raises(ValueError, match="REVIEWED_TEMPLATE_REQUIRES_SOURCES_AND_VERIFICATION_TIME"):
        CityRouteTemplate.model_validate(payload)


def test_insertion_cost_and_four_tiers_keep_remote_places_visible():
    before, candidate, after = _place("before"), _place("candidate"), _place("after")
    service = CandidateSuggestionService(StaticRouteEstimator({
        ("before", "candidate"): _edge(20),
        ("candidate", "after"): _edge(25),
        ("before", "after"): _edge(8),
    }))
    result = asyncio.run(service.suggest(candidate=candidate, previous=before, next_stop=after))
    assert result.delta_route_minutes == 37
    assert result.tier is CandidateTier.ANOTHER_DAY
    assert result.hard_gate_passed is True
    assert "SUGGEST_ANOTHER_DAY" in result.explanation_codes


def test_candidate_hard_gate_stops_direct_application_before_ranking():
    result = asyncio.run(CandidateSuggestionService(StaticRouteEstimator({})).suggest(
        candidate=_place("closed"), previous=None, next_stop=None,
        gate=CandidateGate(opening_time_fit=False),
    ))
    assert result.tier is CandidateTier.NOT_FEASIBLE
    assert result.hard_gate_passed is False
    assert "OPENING_TIME_CONFLICT" in result.explanation_codes
    assert result.delta_route_minutes is None


def test_unknown_route_remains_exposed_not_silently_ranked_as_on_the_way():
    result = asyncio.run(CandidateSuggestionService(StaticRouteEstimator({})).suggest(
        candidate=_place("candidate"), previous=_place("previous"), next_stop=_place("next"),
    ))
    assert result.tier is CandidateTier.ACCEPTABLE
    assert result.delta_route_minutes is None
    assert "ROUTE_COST_UNKNOWN" in result.explanation_codes
    assert result.evidence_freshness is EvidenceFreshness.UNAVAILABLE


def test_hotel_area_score_uses_first_and_last_stop_for_every_day():
    hotel = _place("hotel-area")
    day_zero = [_place("d0-first"), _place("d0-last")]
    day_one = [_place("d1-first"), _place("d1-last")]
    service = HotelScoringService(StaticRouteEstimator({
        ("hotel-area", "d0-first"): _edge(10), ("d0-last", "hotel-area"): _edge(12),
        ("hotel-area", "d1-first"): _edge(8), ("d1-last", "hotel-area"): _edge(9),
    }))
    score = asyncio.run(service.score_area(hotel.place_id, [day_zero, day_one]))
    assert score.score_minutes == 39
    assert score.all_days_covered is True
    assert score.explanation_codes == ["HOTEL_ALL_DAY_BOUNDARIES_SCORED"]
