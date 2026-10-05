from types import SimpleNamespace

import pytest

from app.trip_understanding.city_knowledge import source_place_hints
from app.trip_understanding.departure_reference import is_departure_reference
from app.trip_understanding.experience_inference import _with_coverage_diagnostics
from tests.test_source_visit_supplement import plan


@pytest.mark.parametrize('tail,expected', [
    ('离开故宫后去景山公园。', True),
    ('走出故宫后再去景山公园。', True),
    ('不离开故宫后去景山公园。', False),
    ('再访故宫后去景山公园。', False),
    ('离开故宫前再参观一次。', False),
    ('Day2：离开故宫后去景山公园。', False),
])
def test_departure_requires_retained_previous_visit_and_narrow_context(tail, expected):
    source = '北京。Day1：故宫博物院。' + tail
    proposal = plan(source, [('故宫博物院', 1, 1)])
    start = source.rindex('故宫')
    hints = source_place_hints(source)
    assert is_departure_reference(source, proposal, start, start + 2, hints) is expected


def test_departure_clears_only_reference_warning_and_retains_missing_next_visit():
    source = '北京。Day1：故宫博物院。离开故宫后去景山公园。'
    proposal = plan(source, [('故宫博物院', 1, 1)])
    start = source.index('景山公园')
    hints = [*source_place_hints(source), {'span_start': start, 'span_end': start + 4, 'entity_id': 'other-place'}]
    result = _with_coverage_diagnostics(source, SimpleNamespace(unprocessed_quotes=[]), proposal, hints)
    assert [source[d.span_start:d.span_end] for d in result.diagnostics] == ['景山公园']


def test_departure_does_not_hide_an_unrepresented_revisit_or_different_parent():
    source = '北京。Day1：故宫博物院，景山公园。离开故宫后去北海公园。'
    proposal = plan(source, [('故宫博物院', 1, 1), ('景山公园', 1, 1)])
    start = source.rindex('故宫')
    assert not is_departure_reference(source, proposal, start, start + 2, source_place_hints(source))
