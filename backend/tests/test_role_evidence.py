"""Source scope is verifiable; the model retains ownership of role meaning."""
import pytest

from app.trip_understanding.experience_inference import SemanticDraft, SourceAnchorValidationError, proposal_from_draft
from app.trip_understanding.pipeline import EvidenceCompiler


def activity(name, **fields):
    return {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1, **fields}


def test_action_evidence_cannot_borrow_another_days_same_named_visit():
    source = "Day1：前往星河公园。Day2：若有余力可去星河公园。"
    draft = SemanticDraft.model_validate({"activities": [activity("星河公园", role_evidence="若有余力可去星河公园")]})
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(source, draft)
    assert any(issue["category"] == "ROLE_EVIDENCE_SCOPE_MISMATCH" for issue in error.value.issues)


def test_invalid_role_evidence_keeps_other_valid_places_in_partial_result():
    source = "Day1：前往星河公园。然后参观月光桥。"
    draft = SemanticDraft.model_validate({"activities": [activity("星河公园", role_evidence="前往星河公园"),
        activity("月光桥", role_evidence="如果预约成功去月光桥")]})
    result = proposal_from_draft(source, draft, allow_partial=True)
    assert [item.atomic_place_name for item in result.mentions] == ["星河公园"]
    assert result.unprocessed_count == 1


def test_explicit_actual_child_visit_preserves_role_and_parent_evidence_privately():
    source = "Day1：游览星河公园，园内路线：参观月光阁。"
    draft = SemanticDraft.model_validate({"activities": [activity("星河公园", role_evidence="游览星河公园"),
        activity("月光阁", role_evidence="园内路线：参观月光阁", parent_source_quote="星河公园")]})
    result = proposal_from_draft(source, draft)
    assert [item.role.value for item in result.mentions] == ["PLANNED", "PLANNED"]
    assert result.mentions[1].parent_mention_id == result.mentions[0].mention_id
    _activities, claims, _receipt = EvidenceCompiler().compile(source, result)
    assert [claim.quote for claim in claims if claim.claim_type == "ROLE"] == ["游览星河公园", "园内路线：参观月光阁"]


def test_parent_relationship_cannot_point_to_an_unmentioned_parent():
    source = "Day1：游览星河公园，参观月光阁。"
    draft = SemanticDraft.model_validate({"activities": [activity("月光阁", role_evidence="参观月光阁", parent_source_quote="星河公园")]})
    result = proposal_from_draft(source, draft)
    assert result.mentions[0].parent_mention_id is None
    assert any(issue.category == "PARENT_RELATION_UNRESOLVED" for issue in result.diagnostics)
