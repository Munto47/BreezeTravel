"""An address clause cannot create another independent stop."""
import pytest

from app.trip_understanding.semantic_recovery import explicit_reference_context


@pytest.mark.parametrize('text,expected', [
    ('先参观历史馆，选福田区市民中心里的馆。', True),
    ('先参观历史馆，明确是位于福田市民中心的这一馆。', True),
    ('先参观历史馆，选的是市民中心的这一个馆。', True),
    ('先参观历史馆，选的是市民中心这个馆。', True),
    ('先参观历史馆，选的是市民中心这一馆。', True),
    ('先参观历史馆，然后去市民中心参观。', False),
    ('先参观历史馆，再到市民中心，最后去这一个馆。', False),
])
def test_venue_location_grammar_preserves_separate_arrivals(text, expected):
    start = text.index('市民中心')
    assert (explicit_reference_context(text, start, start + 4) == 'VENUE_ADDRESS_REFERENCE') is expected


@pytest.mark.parametrize('quote', ['市民中心', '市民中心里的馆'])
def test_address_proposed_as_internal_visit_creates_neither_child_nor_false_pending(quote):
    from tests.test_source_visit_supplement import apply, plan, row

    source = '北京。\nDay1参观历史馆，选市民中心里的馆。'
    before = plan(source, [('历史馆', 1, 1)])
    after = apply(source, before, [row(0, 'VISIT', quote, source)])
    assert after.mentions == before.mentions
    assert after.unprocessed_count == before.unprocessed_count
    assert after.diagnostics == before.diagnostics


@pytest.mark.parametrize('whole_source', [False, True])
def test_address_on_another_visit_cannot_explain_a_missing_detail(whole_source):
    from tests.test_source_visit_supplement import apply, plan, row

    source = '北京。\nDay1参观历史馆。\nDay2参观民俗馆，选市民中心里的馆。'
    before = plan(source, [('历史馆', 1, 1), ('民俗馆', 2, 1)])
    evidence = source if whole_source else 'Day2参观民俗馆，选市民中心里的馆'
    after = apply(source, before, [row(0, 'VISIT', '市民中心', evidence)])
    assert after.mentions == before.mentions
    assert after.unprocessed_count > before.unprocessed_count
