"""Ticket facts cannot create visits or hide actual arrangements in a guide."""
import pytest

from app.trip_understanding.semantic_recovery import explicit_reference_context


TABLE = '景点预约信息\n青溪公园：免费，无需预约，全天开放\n文化馆：联票35元，提前七天预约，24:00放票\n星河苑：2元，无需预约，6:30-20:00开放'


@pytest.mark.parametrize('name', ['青溪公园', '文化馆', '星河苑'])
def test_contiguous_ticket_rows_are_only_metadata(name):
    source = '北京。\nDay1：青溪公园，之后去文化馆。\n' + TABLE
    start = source.rfind(name)
    assert explicit_reference_context(source, start, start + len(name)) == 'TICKET_METADATA_REFERENCE'
    if name != '星河苑':
        planned = source.index(name)
        assert explicit_reference_context(source, planned, planned + len(name)) is None


@pytest.mark.parametrize('body', [
    'Day2：文化馆：免费，无需预约',
    '游览路线\n文化馆：免费，无需预约',
    '文化馆：免费，预约后前往参观',
    '如果有时间，文化馆：免费',
    '文化馆：免费，下午仍去',
    '文化馆：下午参观，门票35元',
    '文化馆：已取消',
    '路线调整\n文化馆：免费，全天开放',
])
def test_narrative_or_new_heading_ends_ticket_table_scope(body):
    source = TABLE.split('\n文化馆')[0] + '\n' + body
    start = source.rfind('文化馆')
    assert explicit_reference_context(source, start, start + 3) is None


def test_price_on_a_route_without_ticket_heading_is_not_waived():
    source = '北京。\nDay1：文化馆：免费，无需预约，全天开放'
    start = source.index('文化馆')
    assert explicit_reference_context(source, start, start + 3) is None


def test_metadata_review_does_not_clear_a_missing_later_visit():
    from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft, _with_coverage_diagnostics

    source = '北京。\nDay1：青溪公园。\n' + TABLE + '\nDay2：参观文化馆。'
    draft = SemanticDraft.model_validate(dict(destination='北京', activities=[dict(
        source_quote='青溪公园', occurrence=1, place_name='青溪公园', role='PLANNED', day_index=1)]))
    plan = _proposal_from_live_draft(source, draft)
    spans = [source.index('文化馆'), source.rindex('文化馆')]
    checked = _with_coverage_diagnostics(source, draft, plan,
        [dict(span_start=start, span_end=start + 3) for start in spans])
    missing = [d for d in checked.diagnostics if d.category == 'KNOWN_PLACE_UNCLASSIFIED']
    assert len(missing) == 1 and missing[0].span_start == spans[1]
    assert checked.mentions == plan.mentions
    assert checked.unprocessed_count == plan.unprocessed_count + 1
