"""Whole action phrases must never become searchable or displayed place names."""

from __future__ import annotations

import httpx
import pytest

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline


async def _run(name: str, *, role: str = "PLANNED"):
    source = f"北京一日游。Day1：{name}。"
    draft = SemanticDraft.model_validate({
        "destination": "北京",
        "activities": [{"source_quote": name, "place_name": name,
                        "role": role, "day_index": 1, "category": "地点"}],
    })
    requests: list[str] = []

    class Inference:
        async def propose(self, text):
            return proposal_from_draft(text, draft)

    async def respond(request):
        requests.append(request.url.params["keywords"])
        return httpx.Response(200, json={"status": "1", "infocode": "10000", "pois": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolver = AmapPlaceResolver(api_key="synthetic-only", client=client)
        try:
            output = await TripUnderstandingPipeline(Inference(), resolver).run(source)
        except SourceAnchorValidationError as error:
            return None, requests, error
    return output, requests, None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["PLANNED", "OPTIONAL"])
@pytest.mark.parametrize("phrase", [
    "早点出发", "早些出发", "提前出发", "步行半天", "徒步半天", "步行一小时",
])
async def test_whole_action_phrase_is_neither_queried_nor_shown_as_a_place(phrase, role):
    output, requests, error = await _run(phrase, role=role)
    assert requests == []
    assert error is None
    assert output is not None
    assert len(output.proposal.mentions) == 1
    assert output.proposal.mentions[0].atomic_place_name is None
    assert not output.activities[0].compiled.eligible_for_place_search
    cards = [card for day in output.public_result.days for card in day.activities]
    choices = [choice for day in output.public_result.days for choice in day.alternatives]
    assert all(card.name == "地点待确认" for card in cards)
    assert choices == []
    assert len(cards) == (1 if role == "PLANNED" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", ["早点出发，步行半天", "提前出发、徒步半天"])
async def test_action_clauses_cannot_expand_into_multiple_place_queries(phrase):
    output, requests, error = await _run(phrase)
    assert requests == []
    if error is not None:
        assert "NON_ATOMIC_PLACE_LIST" in {issue["category"] for issue in error.issues}
    else:
        assert output is not None
        assert all(item.atomic_place_name is None for item in output.proposal.mentions)
        assert all(not item.compiled.eligible_for_place_search for item in output.activities)
        assert all(card.name == "地点待确认" for day in output.public_result.days for card in day.activities)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "早点出发咖啡馆", "提前出发书店", "步行半天咖啡馆", "徒步半天书店",
])
async def test_a_complete_venue_name_is_not_rejected_for_containing_action_words(name):
    output, requests, error = await _run(name)
    assert error is None
    assert output is not None
    assert requests == [name]
    assert output.proposal.mentions[0].atomic_place_name == name
    assert output.activities[0].compiled.eligible_for_place_search
    card = output.public_result.days[0].activities[0]
    assert card.name == name and card.status == "NEEDS_CONFIRMATION"
