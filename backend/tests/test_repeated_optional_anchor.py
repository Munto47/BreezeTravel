import pytest

from app.trip_understanding.experience_inference import SemanticDraft, SourceAnchorValidationError, proposal_from_draft


def repeated_draft(**option_update):
    return SemanticDraft.model_validate({"destination": "上海", "activities": [
        {"source_quote": "青溪公园", "place_name": "青溪公园", "role": "PLANNED", "day_index": 1},
        {"source_quote": "星河路", "place_name": "星河路", "role": "PLANNED", "day_index": 2},
        {"source_quote": "云岚路", "place_name": "云岚路", "role": "OPTIONAL", "day_index": 2},
        {"source_quote": "星河路", "place_name": "星河路", "role": "OPTIONAL", "day_index": 2,
         "occurrence": 1, **option_update},
    ]})


SOURCE = "Day1：青溪公园。\nDay2：街道散步\n- 上午：星河路。\n- 中午：云岚路 / 星河路吃面。"


@pytest.mark.parametrize("source", [SOURCE, SOURCE.replace("\n", "\r\n  ")])
def test_optional_same_name_is_anchored_to_its_unique_later_occurrence(source):
    proposal = proposal_from_draft(source, repeated_draft())
    assert [(m.atomic_place_name, m.role.value, m.day_index) for m in proposal.mentions] == [
        ("青溪公园", "PLANNED", 1), ("星河路", "PLANNED", 2),
        ("云岚路", "OPTIONAL", 2), ("星河路", "OPTIONAL", 2),
    ]
    assert proposal.mentions[1].span_start == source.index("星河路")
    assert proposal.mentions[3].span_start == source.rindex("星河路")


@pytest.mark.parametrize("change", [
    {"visit_duration_minutes": 60, "time_evidence": "上午"},
    {"fixed_commitment": True, "time_evidence": "上午"},
    {"day_index": 1},
])
def test_reanchoring_does_not_move_timing_commitments_or_days(change):
    with pytest.raises(SourceAnchorValidationError):
        proposal_from_draft(SOURCE, repeated_draft(**change))


def test_multiple_unoccupied_same_name_options_are_not_guessed():
    with pytest.raises(SourceAnchorValidationError):
        proposal_from_draft(SOURCE + "\n- 晚餐：月溪路 / 星河路吃面。", repeated_draft())
