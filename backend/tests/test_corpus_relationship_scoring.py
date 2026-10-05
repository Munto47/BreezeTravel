"""Independent relationships and identity errors remain observable to scoring."""
from copy import deepcopy

import pytest

from scripts.platform_corpus_metrics import compare_annotations, public_projection_observations


def choices():
    gold = [dict(id=str(i), name=name, day_index=1, role='OPTIONAL', choice_group_id=group,
                 branch_id=branch, choice_group_selectable=True)
            for i, (name, group, branch) in enumerate([
                ('青溪公园', 'first', 'outdoor'), ('星河街', 'first', 'outdoor'),
                ('文化馆', 'first', 'indoor'), ('美术馆', 'second', 'indoor')])]
    actual = [dict(row, mention_id=row['id'], choice_group_id='generated-' + row['choice_group_id'],
                   branch_id='generated-' + row['branch_id']) for row in gold]
    return dict(annotation_status='reviewed', annotator_type='implementation_agent', activities=gold), actual


@pytest.mark.parametrize('damage', [None, 'split_group', 'merge_group', 'split_branch', 'merge_branch', 'missing_group', 'not_selectable'])
def test_choice_membership_is_compared_without_requiring_identical_generated_ids(damage):
    label, actual = choices()
    if damage == 'split_group':
        actual[1]['choice_group_id'] = 'different'
    elif damage == 'merge_group':
        actual[3]['choice_group_id'] = actual[2]['choice_group_id']
    elif damage == 'split_branch':
        actual[1]['branch_id'] = 'different'
    elif damage == 'merge_branch':
        actual[2]['branch_id'] = actual[1]['branch_id']
    elif damage == 'missing_group':
        actual[1]['choice_group_id'] = None
    elif damage == 'not_selectable':
        actual[1]['choice_group_selectable'] = False
    result = compare_annotations(label, actual)
    assert result['semantic_exact'] is (damage is None)
    if damage:
        assert result['matched_places'] < result['expected_places']


@pytest.mark.parametrize('wrong_field', ['role', 'day_index', 'parent_mention_id'])
def test_wrong_identity_is_counted_even_when_the_visit_relation_is_also_wrong(wrong_field):
    gold = dict(name='青溪公园', day_index=1, role='PLANNED', parent_id=None,
                span_start=3, span_end=7, expected_poi_ids=['correct'])
    actual = dict(name='青溪公园', day_index=1, role='PLANNED', parent_mention_id=None,
                  span_start=3, span_end=7, poi_id='incorrect')
    actual[wrong_field] = dict(role='OPTIONAL', day_index=2, parent_mention_id='wrong-parent')[wrong_field]
    label = dict(annotation_status='reviewed', annotator_type='implementation_agent', activities=[gold])
    result = compare_annotations(label, [actual])
    assert result['matched_places'] == 0
    assert result['wrong_auto_confirmations'] == 1
    assert result['unassessed_auto_confirmations'] == 0


def test_an_auto_confirmed_addition_without_gold_identity_remains_unassessed():
    label, actual = choices()
    actual.append(dict(name='新添地点', day_index=1, role='PLANNED', poi_id='unreviewed'))
    result = compare_annotations(label, actual)
    assert result['wrong_auto_confirmations'] == 0
    assert result['unassessed_auto_confirmations'] == 1
    assert not result['semantic_exact']


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', [None, 'missing', 'pending', 'wrong_nights'])
async def test_named_overnight_stay_is_visible_in_its_saved_lodging_area(damage):
    from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
    from app.trip_understanding.models import LodgingConstraintView
    from app.trip_understanding.pipeline import TripUnderstandingPipeline
    from tests.semantic_page_replays import FixedReplayPlaces

    source = '北京三天。全程住青溪酒店。\nDay1：青溪公园。\nDay2：文化街。\nDay3：星河公园。'
    activities = [dict(source_quote=name, place_name=name, role='PLANNED', day_index=day, category='景点')
                  for day, name in enumerate(['青溪公园', '文化街', '星河公园'], 1)]
    activities.insert(0, dict(source_quote='青溪酒店', place_name='青溪酒店', role='PLANNED', day_index=1,
                             category='住宿', lodging_event='OVERNIGHT', lodging_scope='WHOLE_TRIP',
                             lodging_evidence='全程住青溪酒店'))
    plan = _proposal_from_live_draft(source, SemanticDraft(destination='北京', activities=activities))
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=plan)
    # Saved lodging recovery uses a separate constraint collection, whereas
    # initial extraction may retain the legacy day-card representation.
    day = output.public_result.days[0]
    card = next(card for card in day.activities if card.category == '住宿')
    day.activities.remove(card)
    output.public_result.lodging_constraints = [LodgingConstraintView(
        **card.model_dump(), scope='WHOLE_TRIP', overnight_days=[1, 2])]
    output = deepcopy(output)
    if damage == 'missing':
        output.public_result.lodging_constraints.clear()
    elif damage == 'pending':
        output.public_result.lodging_constraints[0].status = 'NEEDS_CONFIRMATION'
    elif damage == 'wrong_nights':
        output.public_result.lodging_constraints[0].overnight_days = [1]
    rows = public_projection_observations(output)
    stay = next(row for row in rows if row['name'] == '青溪酒店')
    assert stay['visible_correct'] is (damage is None)


@pytest.mark.asyncio
async def test_one_visible_detail_cannot_cover_two_internal_occurrences():
    from tests.corpus_projection_case import replay
    output = await replay()
    child = next(m for m in output.proposal.mentions if m.parent_mention_id and m.detail_kind == 'VISIT')
    output.proposal.mentions.append(child.model_copy(update={'mention_id':'unprojected-visit',
        'span_start':child.span_start + 1, 'span_end':child.span_end + 1}))
    rows = [row for row in public_projection_observations(output) if row['name'] == child.atomic_place_name]
    assert len(rows) == 2 and sum(row['visible_correct'] for row in rows) == 1
