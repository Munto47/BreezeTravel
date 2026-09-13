"""An alternative is a separate label, never an omitted suffix of its sibling."""
import pytest

from app.trip_understanding.experience_inference import SemanticDraft, SourceAnchorValidationError, proposal_from_draft
from tests.test_semantic_partial_recovery import activity


@pytest.mark.parametrize("first,second", [
    ("中国科学技术馆", "北京自然博物馆"),
    ("浙江省博物馆之江馆区", "孤山馆区"),
    ("上海博物馆东馆", "私人美术馆"),
])
def test_complete_qualified_name_before_or_keeps_both_model_options(first, second):
    source = f"Day1：参观{first}或{second}。"
    draft = SemanticDraft.model_validate({"activities": [activity(first, role="OPTIONAL"), activity(second, role="OPTIONAL")]})
    result = proposal_from_draft(source, draft)
    assert [(item.atomic_place_name, item.role.value) for item in result.mentions] == [(first, "OPTIONAL"), (second, "OPTIONAL")]
    assert result.diagnostics == []


def test_the_actual_attached_branch_is_still_required_before_an_alternative():
    source = "Day1：参观上海博物馆东馆或私人美术馆。"
    draft = SemanticDraft.model_validate({"activities": [activity("上海博物馆", role="OPTIONAL"), activity("私人美术馆", role="OPTIONAL")]})
    with pytest.raises(SourceAnchorValidationError):
        proposal_from_draft(source, draft)
