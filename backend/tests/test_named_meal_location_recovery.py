"""A source-named lunch street cannot silently become an unnamed meal."""
import pytest

from app.trip_understanding.experience_inference import SemanticDraft, SourceAnchorValidationError, proposal_from_draft
from app.trip_understanding.guide_choices import explicit_visit_labels


def propose(source, quote, name=None, *, role="PLANNED", day=1, more=()):
    return proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [
        {"source_quote": quote, "place_name": name, "role": role, "day_index": day, "category": "餐饮"}, *more,
    ]}))


@pytest.mark.parametrize("quote", ["青溪路", "青溪路吃午饭", "中午：青溪路吃午饭"])
def test_existing_meal_quote_keeps_the_explicit_street_name(quote):
    source = "Day1\n中午：青溪路吃午饭，吃点面。"
    proposal = propose(source, quote)
    assert len(proposal.mentions) == 1
    mention = proposal.mentions[0]
    assert mention.atomic_place_name == mention.raw_text == "青溪路"
    assert mention.category_hint == "地点"
    assert mention.day_index == 1 and mention.role.value == "PLANNED"
    assert source[mention.span_start:mention.span_end] == "青溪路"


def test_omitting_the_named_lunch_street_is_an_error_even_if_a_generic_meal_exists():
    with pytest.raises(SourceAnchorValidationError, match="MISSING_EXPLICIT_VISIT_PLACE"):
        propose("Day1\n中午：青溪路吃午饭。", "午饭")


def test_an_existing_named_card_is_not_duplicated_by_an_unnamed_meal():
    source = "Day1\n中午：青溪路吃午饭。"
    named = {"source_quote": "青溪路", "place_name": "青溪路", "role": "PLANNED", "day_index": 1}
    proposal = propose(source, "青溪路吃午饭", more=[named])
    assert [m.atomic_place_name for m in proposal.mentions] == [None, "青溪路"]


@pytest.mark.parametrize("clause", [
    "如果下雨，中午：青溪路吃午饭。", "> 中午：青溪路吃午饭。",
    "中午：青溪路吃午饭，后来取消了。", "中午：青溪路周边吃饭。",
    "中午：青溪路 / 星河街吃午饭。", "中午：青溪餐厅吃午饭。",
    "中午：附近街吃午饭。", "中午：前往青溪路吃午饭。",
])
def test_conditions_references_areas_choices_restaurants_and_prose_are_not_forced_street_visits(clause):
    assert explicit_visit_labels("Day1\n" + clause) == []


def test_unselected_day_branch_keeps_the_recovered_street_optional():
    source = "Day1：二选一\n### 方案 A：城中\n中午：青溪路吃午饭。\n### 方案 B：郊外\n星河公园。"
    proposal = propose(source, "青溪路吃午饭", role="OPTIONAL")
    assert proposal.mentions[0].atomic_place_name == "青溪路"
    assert proposal.mentions[0].role.value == "OPTIONAL"
