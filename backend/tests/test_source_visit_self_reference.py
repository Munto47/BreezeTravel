"""A repeated, already retained parent is not a missing internal project."""
import pytest

from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
from app.trip_understanding.source_visit_supplement import apply_source_visit_supplement


@pytest.mark.parametrize('parent_role,optional,occurrence,unfinished', [
    ('PLANNED', False, 1, False),
    ('PLANNED', True, 1, True),
    ('OPTIONAL', False, 1, False),
    ('OPTIONAL', True, 1, False),
    ('OPTIONAL', True, 2, True),
])
def test_self_reference_keeps_role_but_never_borrows_another_occurrence(parent_role, optional, occurrence, unfinished):
    source = '北京。\nDay1：青溪公园。\nDay2：再次去青溪公园。'
    draft = SemanticDraft.model_validate(dict(destination='北京', activities=[
        dict(source_quote='青溪公园', place_name='青溪公园', role=parent_role, day_index=1)]))
    before = _proposal_from_live_draft(source, draft)
    evidence = 'Day1：青溪公园。' if occurrence == 1 else 'Day2：再次去青溪公园。'
    after = apply_source_visit_supplement(source, before, [dict(parent_index=0, kind='VISIT',
        source_quote='青溪公园', evidence=evidence, optional=optional)], parent_ids=[before.mentions[0].mention_id])
    assert after.mentions == before.mentions
    assert bool(after.unprocessed_count) == unfinished
