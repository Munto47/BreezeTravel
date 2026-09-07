import json

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft, SourceAnchorValidationError, _repair_prompt, _validation_issues, proposal_from_draft,
)


@pytest.mark.parametrize("decoration", ["", "**"])
@pytest.mark.parametrize("occurrence,qualifier", [(1, "西湖馆区"), (2, "江南分馆")])
def test_missing_campus_gets_the_exact_occurrence_in_private_repair_data_only(decoration, occurrence, qualifier):
    source = f"Day1：{decoration}青岚博物馆（西湖馆区，免费）{decoration}。随后青岚博物馆（江南分馆，免费）。"
    draft = SemanticDraft.model_validate({"destination": "杭州", "activities": [
        {"source_quote": "青岚博物馆", "place_name": "青岚博物馆", "occurrence": occurrence,
         "day_index": 1, "role": "PLANNED"},
    ]})
    with pytest.raises(SourceAnchorValidationError) as caught:
        proposal_from_draft(source, draft)
    error = caught.value
    hint = json.loads(error.repair_hints[0])
    assert hint == {"field": "activities[0].place_name", "source_quote": f"青岚博物馆（{qualifier}，免费）",
                    "place_name": f"青岚博物馆（{qualifier}）", "occurrence": 1}
    assert hint["source_quote"] in _repair_prompt(source, draft.model_dump_json(), error)
    assert "青岚" not in str(error) + json.dumps(_validation_issues(error), ensure_ascii=False)


def test_a_conflicting_or_unrecognized_annotation_cannot_become_a_clean_identity_hint():
    source = "Day1：青岚博物馆（西馆，改到北馆）。"
    draft = SemanticDraft.model_validate({"activities": [
        {"source_quote": "青岚博物馆（西馆，改到北馆）", "place_name": "青岚博物馆（北馆）", "role": "PLANNED", "day_index": 1},
    ]})
    with pytest.raises(SourceAnchorValidationError) as caught:
        proposal_from_draft(source, draft)
    assert not caught.value.repair_hints
