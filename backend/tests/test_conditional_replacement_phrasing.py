"""Natural explicit fallback wording binds the existing default visit."""
import pytest

from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft


@pytest.mark.parametrize('local_evidence', [False, True])
@pytest.mark.parametrize('phrase', ['把青溪公园替换成', '把这次青溪公园的安排替换成', '将本次青溪公园的游览换成'])
def test_explicit_arrangement_replacement_preserves_default_and_binds_fallback(phrase, local_evidence):
    source = f'北京。Day1：青溪公园，园内慢慢走。如果下雨，就{phrase}星河博物馆，看室内展览。'
    draft = SemanticDraft.model_validate(dict(destination='北京', activities=[
        dict(source_quote='青溪公园', place_name='青溪公园', role='PLANNED', day_index=1),
        dict(source_quote='星河博物馆', place_name='星河博物馆', role='OPTIONAL', day_index=1,
             conditional_replacement=dict(target_quote='青溪公园', target_occurrence=1,
                condition_quote='如果下雨', evidence=source[source.index('如果' if local_evidence else '青溪公园'):]))]))
    plan = _proposal_from_live_draft(source, draft)
    assert plan.mentions[0].role == 'PLANNED'
    assert plan.mentions[1].role == 'OPTIONAL'
    assert plan.mentions[1].replaces_mention_id == plan.mentions[0].mention_id
    assert plan.mentions[1].replacement_condition == '如果下雨'
    assert not plan.diagnostics
    from app.trip_understanding.conditional_replacement import is_replacement_reference

    repeated = source.index('青溪公园', source.index('如果'))
    assert is_replacement_reference(source, plan, repeated, repeated + len('青溪公园'))
    assert not is_replacement_reference(source, plan, source.index('青溪公园'), source.index('青溪公园') + len('青溪公园'))


def test_repeated_default_remains_ambiguous_despite_this_visit_wording():
    source = '北京。Day1：青溪公园，再去星河街，然后再访青溪公园。如果下雨，把这次青溪公园的安排替换成星河博物馆。'
    visits = [('青溪公园', 1), ('星河街', 1), ('青溪公园', 2), ('星河博物馆', 1)]
    activities = [dict(source_quote=name, place_name=name, occurrence=n, role='PLANNED',day_index=1) for name,n in visits]
    activities[-1].update(role='OPTIONAL',conditional_replacement=dict(target_quote='青溪公园',target_occurrence=1,
        condition_quote='如果下雨', evidence=source[source.index('青溪公园'):]))
    plan = _proposal_from_live_draft(source, SemanticDraft.model_validate(dict(destination='北京',activities=activities)))
    assert plan.mentions[-1].replaces_mention_id is None
    assert any(d.category == 'CONDITIONAL_REPLACEMENT_UNRESOLVED' for d in plan.diagnostics)


@pytest.mark.parametrize('tail,expected', [('晴天仍按原来的公园计划走。', True),
    ('天气好的话仍走原定公园路线。', True),
    ('晴天仍按原来的公园计划走，再去青溪桥。', False), ('然后再去青溪公园。', False)])
def test_implicit_default_reference_never_hides_a_new_visit(tail, expected):
    from app.trip_understanding.conditional_replacement import is_implicit_default_reference
    from app.trip_understanding.source_inventory import source_segments

    source = '北京。Day1：青溪公园。如果下雨，把青溪公园替换成星河博物馆；' + tail
    draft = SemanticDraft.model_validate(dict(destination='北京', activities=[
        dict(source_quote='青溪公园', place_name='青溪公园', role='PLANNED', day_index=1),
        dict(source_quote='星河博物馆', place_name='星河博物馆', role='OPTIONAL', day_index=1,
            conditional_replacement=dict(target_quote='青溪公园', condition_quote='如果下雨',
                evidence=source[source.index('青溪公园'):]))]))
    proposal = _proposal_from_live_draft(source, draft)
    assert is_implicit_default_reference(source, proposal, source_segments(source)[-1], '青溪公园') is expected
    assert not is_implicit_default_reference(source, proposal, source_segments(source)[-1], '另一个公园')


@pytest.mark.parametrize('target_occurrence,expected', [(1, True), (2, False), (3, False)])
def test_replacement_inventory_reference_must_point_to_validated_default(target_occurrence, expected):
    from app.trip_understanding.source_inventory import inventory_covers, source_segments

    source = '北京。\nDay1：青溪公园。如果下雨，把青溪公园替换成星河博物馆。'
    draft = SemanticDraft.model_validate(dict(destination='北京', activities=[
        dict(source_quote='青溪公园', place_name='青溪公园', role='PLANNED', day_index=1),
        dict(source_quote='星河博物馆', place_name='星河博物馆', role='OPTIONAL', day_index=1,
            conditional_replacement=dict(target_quote='青溪公园', condition_quote='如果下雨',
                evidence=source[source.index('青溪公园'):]))]))
    proposal = _proposal_from_live_draft(source, draft)
    items = [[], [dict(quote='青溪公园', day_index=1, role='PLANNED')], [
        dict(quote='青溪公园', day_index=1, role='REFERENCE', refers_to_occurrence=target_occurrence),
        dict(quote='星河博物馆', day_index=1, role='OPTIONAL')]]
    raw = dict(segments=[dict(segment_index=s['index'], items=items[s['index']]) for s in source_segments(source)])
    assert inventory_covers(source, proposal, raw) is expected


@pytest.mark.parametrize('role,day,tail,expected', [
    ('REFERENCE', 1, '晴天仍按原来的公园计划走。', True),
    ('PLANNED', 1, '晴天仍按原来的公园计划走。', True),
    ('PLANNED', 2, '晴天仍按原来的公园计划走。', False),
    ('OPTIONAL', 1, '晴天仍按原来的公园计划走。', False),
    ('PLANNED', 1, '晴天仍按原来的公园计划走，再到青溪桥。', False),
])
@pytest.mark.parametrize('quote', ['青溪公园', '原来的公园计划'])
def test_contextual_default_review_cannot_cover_another_day_or_new_arrival(role, day, tail, expected, quote):
    from app.trip_understanding.source_inventory import inventory_covers, source_segments

    source = '北京。\nDay1：青溪公园。如果下雨，把青溪公园替换成星河博物馆；' + tail
    draft = SemanticDraft.model_validate(dict(destination='北京', activities=[
        dict(source_quote='青溪公园', place_name='青溪公园', role='PLANNED', day_index=1),
        dict(source_quote='星河博物馆', place_name='星河博物馆', role='OPTIONAL', day_index=1,
            conditional_replacement=dict(target_quote='青溪公园', condition_quote='如果下雨',
                evidence=source[source.index('青溪公园'):]))]))
    proposal = _proposal_from_live_draft(source, draft)
    items = [[], [dict(quote='青溪公园', day_index=1, role='PLANNED')], [
        dict(quote='青溪公园', day_index=1, role='REFERENCE'),
        dict(quote='星河博物馆', day_index=1, role='OPTIONAL')],
        [dict(quote=quote, day_index=day, role=role)]]
    raw = dict(segments=[dict(segment_index=s['index'], items=items[s['index']]) for s in source_segments(source)])
    assert inventory_covers(source, proposal, raw) is expected
