"""Explicit review corrections stay within the supplied internal occurrence."""
from copy import deepcopy

import pytest

from app.trip_understanding.detail_review import apply_detail_reviews, reviewable_details
from tests.test_source_visit_supplement import apply, plan, row


SOURCE = '北京。\nDay1：青溪公园，园内参观星河亭，看看花草，东门出。'


def provisional():
    result = apply(SOURCE, plan(SOURCE, [('青溪公园', 1, 1)]), [
        row(0, 'VISIT', '星河亭', '园内参观星河亭'),
        row(0, 'VISIT', '花草', '看看花草'),
        row(0, 'EXIT', '东门', '东门出'),
    ])
    assert len(result.mentions) == 4
    return result.model_copy(update={'binding': {'_reviewable_details': reviewable_details(result)}})


def test_only_explicit_description_review_removes_a_provisional_detail():
    before = provisional()
    before.binding['_detail_reviews'] = [dict(detail_index=1, classification='GENERAL_DESCRIPTION', evidence='看看花草')]
    after = apply_detail_reviews(SOURCE, before)
    assert [m.atomic_place_name for m in after.mentions] == ['青溪公园', '星河亭', '东门']
    assert after.mentions[0] == before.mentions[0]
    assert len(before.mentions) == 4


@pytest.mark.parametrize('reviews', [[],
    [dict(detail_index=1, classification='NAMED_VISIT', evidence='看看花草')],
    [dict(detail_index=1, classification='GENERAL_DESCRIPTION', evidence='另一处的花草')],
    [dict(detail_index=99, classification='GENERAL_DESCRIPTION', evidence=SOURCE)],
    [dict(detail_index=1, classification='GENERAL_DESCRIPTION', evidence='看看花草')] * 2,
])
def test_missing_invalid_or_conflicting_review_preserves_every_detail(reviews):
    before = provisional()
    before.binding['_detail_reviews'] = reviews
    assert apply_detail_reviews(SOURCE, before).mentions == before.mentions


def test_review_cannot_remove_a_root_or_exit_even_with_forged_supplied_index():
    before = provisional()
    for target in (before.mentions[0], before.mentions[-1]):
        forged = deepcopy(before)
        forged.binding['_reviewable_details'][0].update(mention_id=target.mention_id, start=target.span_start, end=target.span_end)
        forged.binding['_detail_reviews'] = [dict(detail_index=0, classification='GENERAL_DESCRIPTION', evidence=SOURCE)]
        assert apply_detail_reviews(SOURCE, forged).mentions == before.mentions


def test_conflicting_internal_inventory_preserves_detail_and_requires_review():
    before = provisional()
    before.binding['_detail_reviews'] = [dict(detail_index=0, classification='GENERAL_DESCRIPTION', evidence=SOURCE)]
    before.binding['_source_inventory'] = dict(segments=[dict(segment_index=1, items=[
        dict(quote='星河亭', day_index=1, role='PLANNED', kind='INTERNAL', parent_quote='青溪公园')])])
    after = apply_detail_reviews(SOURCE, before)
    assert after.mentions == before.mentions
    assert after.unprocessed_count == before.unprocessed_count + 1
    assert after.diagnostics[-1].category == 'SOURCE_VISIT_UNRESOLVED'
    assert apply_detail_reviews(SOURCE, after).unprocessed_count == after.unprocessed_count


@pytest.mark.asyncio
async def test_live_two_answer_path_does_not_reintroduce_a_corrected_description():
    from tests.test_semantic_supplement_budget import Client, provider

    first = dict(destination='北京', day_labels=[None], activities=[dict(source_quote='青溪公园',
        place_name='青溪公园', role='PLANNED', day_index=1, category='景点', source_details=[
            dict(kind='VISIT', source_quote=name, optional=False, evidence=evidence)
            for name, evidence in [('星河亭', '园内参观星河亭'), ('花草', '看看花草')]])])
    second = dict(city_fields=[], source_visits=[], detail_reviews=[dict(detail_index=1,
        classification='GENERAL_DESCRIPTION', evidence='看看花草')])
    client = Client(first, second)
    result = await provider(client).propose(SOURCE)
    assert [m.atomic_place_name for m in result.mentions] == ['青溪公园', '星河亭']
    assert len(client.calls) == 2
    assert result.unprocessed_count > 0  # The omitted exit still requires attention.
