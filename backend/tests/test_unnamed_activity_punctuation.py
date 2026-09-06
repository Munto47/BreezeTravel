"""Original synthetic inputs for unnamed descriptions and genuine place lists."""

from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)
from app.trip_understanding.models import ActivityRole
from app.trip_understanding.pipeline import TripUnderstandingPipeline


def _draft(quote, *, name=None, role="PLANNED", category="地点"):
    return SemanticDraft.model_validate({
        "destination": "北京",
        "activities": [{"source_quote": quote, "place_name": name,
                        "role": role, "day_index": 1, "category": category}],
    })


@pytest.mark.parametrize("role", ["PLANNED", "OPTIONAL"])
@pytest.mark.parametrize("quote", [
    "上午集合，沿山路游玩 2 小时",
    "上午休息，下午自由活动",
    "提前预约，之后返回住处",
])
def test_unnamed_description_punctuation_preserves_source_day_and_role(quote, role):
    proposal = proposal_from_draft(quote, _draft(quote, role=role))
    assert len(proposal.mentions) == 1
    mention = proposal.mentions[0]
    assert mention.atomic_place_name is None
    assert mention.role == ActivityRole(role) and mention.day_index == 1
    assert mention.raw_text == quote
    assert quote[mention.span_start:mention.span_end] == quote


@pytest.mark.parametrize("category", ["景点", "地点", "交通节点", "住宿"])
def test_unnamed_description_is_not_a_place_list_in_any_location_category(category):
    quote = "上午休息，下午自由活动"
    proposal = proposal_from_draft(quote, _draft(quote, category=category))
    assert len(proposal.mentions) == 1
    assert proposal.mentions[0].atomic_place_name is None


@pytest.mark.parametrize("role", ["PLANNED", "OPTIONAL"])
@pytest.mark.parametrize("quote", ["星河公园、青溪博物馆", "星河、青溪"])
def test_unnamed_pure_place_list_still_requires_separate_names(quote, role):
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(quote, _draft(quote, role=role))
    assert "NON_ATOMIC_PLACE_LIST" in {issue["category"] for issue in error.value.issues}


@pytest.mark.parametrize("quote", [
    "下午：**星河公园、青溪博物馆**，游玩 2 小时。",
    "下午：星河公园、青溪博物馆，游玩 2 小时。",
])
def test_prose_with_explicit_named_places_cannot_bypass_completeness(quote):
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(quote, _draft(quote))
    assert "MISSING_EXPLICIT_PARALLEL_PLACE" in {
        issue["category"] for issue in error.value.issues
    }


def test_a_named_place_list_still_becomes_two_individual_places():
    quote = "星河公园、青溪博物馆"
    proposal = proposal_from_draft(quote, _draft(quote, name=quote))
    assert [mention.atomic_place_name for mention in proposal.mentions] == [
        "星河公园", "青溪博物馆",
    ]


@pytest.mark.parametrize("quote", ["星河公园、下午休息", "星河公园、https://example.com"])
def test_an_unsplittable_named_list_still_fails_atomic_validation(quote):
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(quote, _draft(quote, name=quote))
    assert "NON_ATOMIC_PLACE_LIST" in {issue["category"] for issue in error.value.issues}


@pytest.mark.asyncio
async def test_unnamed_description_does_not_search_or_confirm_a_place():
    quote = "上午集合，沿山路游玩 2 小时"

    class DraftProvider:
        async def propose(self, source):
            return proposal_from_draft(source, _draft(quote))

    class NoSearch:
        async def resolve(self, **values):
            raise AssertionError("An unnamed activity cannot trigger a place query")

    result = await TripUnderstandingPipeline(DraftProvider(), NoSearch()).run(quote)
    assert len(result.activities) == 1
    assert not result.activities[0].compiled.eligible_for_place_search
    assert result.proposal.mentions[0].atomic_place_name is None
