"""Context classification cannot substitute for reviewing actual visits."""
from copy import deepcopy

from app.trip_understanding.source_inventory import inventory_covers, source_segments
from tests.test_source_visit_supplement import plan


SOURCE = '这份攻略包含园内安排和备选。\nDay1：青溪公园。'


def inventory():
    segments = source_segments(SOURCE)
    return dict(segments=[dict(segment_index=s['index'],classification='ARRANGEMENTS' if '青溪公园' in s['text'] else 'CONTEXT',
        unresolved=False,items=[dict(quote='青溪公园',occurrence=1,day_index=1,role='PLANNED',kind='VISIT')]
            if '青溪公园' in s['text'] else []) for s in segments])


def test_explicit_context_review_does_not_create_pending_visits_for_introduction():
    proposal = plan(SOURCE,[('青溪公园',1,1)])
    assert inventory_covers(SOURCE,proposal,inventory())
    legacy = deepcopy(inventory())
    for segment in legacy['segments']:
        segment.pop('classification')
    assert not inventory_covers(SOURCE,proposal,legacy)


def test_context_review_still_requires_every_retained_visit_and_source_segment():
    proposal = plan(SOURCE,[('青溪公园',1,1)])
    missing_visit = inventory()
    missing_visit['segments'][-1].update(classification='CONTEXT',items=[])
    assert not inventory_covers(SOURCE,proposal,missing_visit)
    missing_segment = inventory()
    missing_segment['segments'].pop(0)
    assert not inventory_covers(SOURCE,proposal,missing_segment)
    empty_arrangement = inventory()
    empty_arrangement['segments'][-1]['items'] = []
    assert not inventory_covers(SOURCE,proposal,empty_arrangement)


def test_unique_segment_quote_has_no_global_vs_local_ordinal_ambiguity():
    proposal = plan(SOURCE,[('青溪公园',1,1)])
    reviewed = inventory()
    reviewed['segments'][-1]['items'][0]['occurrence'] = 2
    assert inventory_covers(SOURCE, proposal, reviewed)


def test_bad_ordinal_still_fails_when_segment_contains_two_actual_visits():
    source = 'Day1：青溪公园，再访青溪公园。'
    proposal = plan(source, [('青溪公园',1,1), ('青溪公园',1,2)])
    reviewed = dict(segments=[dict(segment_index=0,classification='ARRANGEMENTS', items=[
        dict(quote='青溪公园',occurrence=n,day_index=1,role='PLANNED',kind='VISIT') for n in [1,3]])])
    assert not inventory_covers(source, proposal, reviewed)
