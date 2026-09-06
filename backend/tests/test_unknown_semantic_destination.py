"""Missing model destination remains unknown without losing source activities."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.models import DestinationBasis
from app.trip_understanding.pipeline import _model_activity_cities


SOURCE = "Day1：M28艺境；云岚47书院。\nDay2：星湾2030。\nDay3：青岚展厅。"
PLACES = [("M28艺境", 1), ("云岚47书院", 1), ("星湾2030", 2), ("青岚展厅", 3)]


def activities(places=PLACES):
    return [{"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": day}
            for name, day in places]


@pytest.mark.parametrize("destination_fields", [
    {}, {"destination": None}, {"destination": ""}, {"destination": "   "},
    {"destination": "\t\r\n"}, {"destination": "\u3000"},
])
def test_missing_null_or_blank_destination_preserves_unknown_places_order_and_days(destination_fields):
    draft = SemanticDraft.model_validate({"activities": activities(), **destination_fields})
    assert draft.destination == "目的地待确认"
    proposal = proposal_from_draft(SOURCE, draft)
    assert proposal.destination_name == "目的地待确认"
    assert proposal.destination_basis == DestinationBasis.SOFT_ASSUMPTION
    assert proposal.day_count == 3
    assert len(proposal.mentions) == len(PLACES)
    assert [(item.atomic_place_name, item.day_index) for item in proposal.mentions] == PLACES
    assert [item.sequence_index for item in proposal.mentions] == [0, 1, 0, 0]
    assert all(item.role.value == "PLANNED" for item in proposal.mentions)
    for item in proposal.mentions:
        assert SOURCE[item.span_start:item.span_end] == item.raw_text == item.atomic_place_name
        assert item.city_hint is None
        assert item.city_evidence is None
        assert _model_activity_cities(SOURCE, proposal, item) == ("目的地待确认",)


def test_city_words_embedded_in_unknown_poi_names_cannot_supply_the_missing_destination():
    places = [("北京云岭书院", 1), ("上海星河工坊", 2)]
    source = "Day1：北京云岭书院。\nDay2：上海星河工坊。"
    draft = SemanticDraft.model_validate({"destination": None, "activities": activities(places)})
    proposal = proposal_from_draft(source, draft)
    assert proposal.destination_name == "目的地待确认"
    assert proposal.destination_basis == DestinationBasis.SOFT_ASSUMPTION
    assert proposal.day_count == 2
    assert [(item.atomic_place_name, item.day_index) for item in proposal.mentions] == places
    assert all(item.city_hint is None for item in proposal.mentions)
    assert all(_model_activity_cities(source, proposal, item) == ("目的地待确认",) for item in proposal.mentions)


@pytest.mark.parametrize("invalid", [{"city": "北京"}, ["北京"], 1, 3.5, True])
def test_non_string_destination_is_rejected_instead_of_becoming_a_city_or_unknown(invalid):
    with pytest.raises(ValidationError) as error:
        SemanticDraft.model_validate({"destination": invalid, "activities": activities()})
    assert any(issue["loc"] == ("destination",) for issue in error.value.errors())


def test_an_explicit_literal_destination_is_preserved():
    source = "上海三日游\n" + SOURCE
    draft = SemanticDraft.model_validate({"destination": "上海", "activities": activities()})
    proposal = proposal_from_draft(source, draft)
    assert proposal.destination_name == "上海"
    assert proposal.destination_basis == DestinationBasis.EXPLICIT
    assert proposal.day_count == 3
    assert [(item.atomic_place_name, item.day_index) for item in proposal.mentions] == PLACES
