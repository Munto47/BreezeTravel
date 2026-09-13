"""Compact-only source ranges; fixed meanings are not live model improvements."""
import copy
import json

from jsonschema import Draft202012Validator
import pytest
from pydantic import ValidationError

from app.trip_understanding.compact_semantic_wire import (
    compact_draft_payload, compact_order_scope, complete_compact_items_from_truncated_json,
    expand_compact_payload, expand_order_scope,
)
from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
from tests.test_compact_semantic_wire import Client, compact, payload, provider, row, run


def anchor(quote, occurrence=1):
    return dict(quote=quote, occurrence=occurrence)


def ranged(value, start, end, *, group=0):
    value = compact(value)
    value['order_groups'][group].pop('scope_quote', None)
    value['order_groups'][group]['scope_range'] = dict(start=start, end=end)
    return value


def source_pair():
    source = '北京。\nDay1：青溪公园、月光桥。'
    value = payload([row('青溪公园'), row('月光桥')])
    value['order_groups'] = [dict(kind='INITIAL_ORDER', activity_indices=[0, 1],
        scope_quote='Day1：青溪公园、月光桥。')]
    return source, value


def assess(source, value):
    return _proposal_from_live_draft(source, SemanticDraft.model_validate(value), allow_partial=True)


def test_range_roundtrip_preserves_the_existing_typed_order_assessment():
    source, original = source_pair()
    wire = ranged(original, anchor('Day1：'), anchor('月光桥。'))
    expanded = expand_compact_payload(wire, source=source)
    assert SemanticDraft.model_validate(expanded) == SemanticDraft.model_validate(original)
    assert assess(source, expanded) == assess(source, original)
    assert wire['order_groups'][0]['scope_range']['start']['quote'] == 'Day1：'


def test_live_compact_schema_advertises_ranges_but_normal_schema_does_not_change():
    source, original = source_pair()
    instance = provider(Client())
    schema = instance.schema
    Draft202012Validator.check_schema(schema)
    wire = ranged(original, anchor('Day1：'), anchor('月光桥。'))
    Draft202012Validator(schema).validate(wire)
    group = schema['$defs']['CompactSemanticOrderGroup']
    assert 'scope_range' in group['required'] and 'scope_quote' not in group['properties']
    legacy = instance._read_draft(source, original)
    assert legacy == SemanticDraft.model_validate(original)


@pytest.mark.asyncio
async def test_range_provider_keeps_fixed_public_names_and_current_budget():
    source, original = source_pair()
    wire = ranged(original, anchor('Day1：'), anchor('月光桥。'))
    output, client = await run(source, wire)
    assert [(c.name, c.status) for c in output.public_result.days[0].activities] == [
        ('青溪公园', 'READY'), ('月光桥', 'READY')]
    assert output.public_result.coverage.complete
    assert output.proposal.order_assessment.groups[0].kind == 'INITIAL_ORDER'
    assert len(client.calls) == output.proposal.binding['external_calls'] == 1
    assert client.calls[0]['max_tokens'] == 4096
    assert client.calls[0]['messages'][1]['content'] == source


@pytest.mark.parametrize('bad', [
    {'start': {'quote': 'Day1：'}, 'end': anchor('月光桥。')},
    {'start': anchor('Day1：', True), 'end': anchor('月光桥。')},
    {'start': anchor('Day1：', '1'), 'end': anchor('月光桥。')},
    {'start': anchor('Day1：', 0), 'end': anchor('月光桥。')},
    {'start': anchor('Day1：', 2), 'end': anchor('月光桥。')},
    {'start': anchor('不存在'), 'end': anchor('月光桥。')},
    {'start': anchor('月光桥。'), 'end': anchor('Day1：')},
    {'start': anchor('Day1：'), 'end': anchor('月光桥。'), 'guess': True},
    None,
])
def test_bad_range_preserves_activities_but_never_assesses_them(bad):
    source, original = source_pair()
    wire = ranged(original, anchor('Day1：'), anchor('月光桥。'))
    wire['order_groups'][0]['scope_range'] = bad
    output = assess(source, expand_compact_payload(wire, source=source))
    assert [m.atomic_place_name for m in output.mentions] == ['青溪公园', '月光桥']
    assert not output.order_assessment.groups
    assert len(output.order_assessment.unassessed_mention_ids) == 2
    assert 'ORDER_ASSESSMENT_INCOMPLETE' in output.order_assessment.issues


def test_optional_legacy_scope_remains_readable_without_implicitly_picking_a_range():
    source, original = source_pair()
    assert SemanticDraft.model_validate(expand_compact_payload(compact(original))) == SemanticDraft.model_validate(original)
    wire = ranged(original, anchor('Day1：'), anchor('月光桥。'))
    wire['order_groups'][0]['scope_quote'] = '不同的旧范围'
    output = assess(source, expand_compact_payload(wire, source=source))
    assert len(output.order_assessment.unassessed_mention_ids) == 2


@pytest.mark.asyncio
async def test_correct_160_control_keeps_every_name_role_day_occurrence_and_order():
    names = [f'容量样本{i:03d}公园' for i in range(160)]
    source = '北京。\nDay1：' + '、'.join(names) + '。'
    value = payload([row(name, category='景点') for name in names])
    value['order_groups'] = [dict(kind='INITIAL_ORDER', activity_indices=list(range(160)), scope_quote=source)]
    wire = compact_draft_payload(SemanticDraft.model_validate(value), source=source)
    assert 'scope_quote' not in wire['order_groups'][0]
    Draft202012Validator(provider(Client()).schema).validate(wire)
    restored = expand_compact_payload(wire, source=source)
    assert SemanticDraft.model_validate(restored) == SemanticDraft.model_validate(value)
    assert assess(source, restored) == assess(source, value)
    output, client = await run(source, wire)
    assert [(m.atomic_place_name, m.role.value, m.day_index, source[m.span_start:m.span_end])
        for m in output.proposal.mentions] == [(n, 'PLANNED', 1, n) for n in names]
    assert [c.name for c in output.public_result.days[0].activities] == names
    assert all(c.status == 'READY' for c in output.public_result.days[0].activities)
    assert len(output.proposal.order_assessment.groups[0].member_mention_ids) == 160
    assert output.public_result.coverage.complete and len(client.calls) == 1


def test_same_names_on_two_days_and_explicit_unknown_roundtrip_without_merging():
    from tests.test_order_assessment_coverage import two_day_revisit
    source, value = two_day_revisit()
    for item in value['activities']:
        item['source_details'] = []
    wire = compact_draft_payload(SemanticDraft.model_validate(value), source=source)
    restored = expand_compact_payload(wire, source=source)
    before, after = assess(source, value), assess(source, restored)
    assert before == after and len(after.mentions) == 4
    assert after.mentions[0].span_start != after.mentions[2].span_start
    assert len(after.order_assessment.explicit_unknown_mention_ids) == 2
    assert not after.order_assessment.unassessed_mention_ids


def test_final_correction_keeps_later_occurrences_and_execution_order():
    source = '北京。\nDay1：星河公园、月光公园。\n更正：第一天最终改为月光公园，再去星河公园。'
    scope = '更正：第一天最终改为月光公园，再去星河公园。'
    value = payload([row(n, occurrence=2) for n in ['月光公园', '星河公园']])
    value['order_groups'] = [dict(kind='INITIAL_ORDER', activity_indices=[0, 1], scope_quote=scope)]
    wire = compact_draft_payload(SemanticDraft.model_validate(value), source=source)
    restored = expand_compact_payload(wire, source=source)
    assert assess(source, restored) == assess(source, value)
    output = assess(source, restored)
    assert [m.atomic_place_name for m in output.mentions] == ['月光公园', '星河公园']
    assert min(m.span_start for m in output.mentions) >= source.index('更正：')


@pytest.mark.parametrize('bad_evidence', [False, True])
def test_hard_precedence_full_evidence_remains_required_and_validated(bad_evidence):
    from tests.test_source_order_runtime import prepared
    source, value = prepared('REQUIRED_PRECEDENCE')
    if bad_evidence:
        value['order_groups'][0]['required_precedence'][0]['evidence'] = '没有这个先后要求'
    wire = compact_draft_payload(SemanticDraft.model_validate(value), source=source)
    assert wire['order_groups'][0]['required_precedence'] == value['order_groups'][0]['required_precedence']
    restored = expand_compact_payload(wire, source=source)
    output = assess(source, restored)
    assert output == assess(source, value)
    if bad_evidence:
        assert not output.order_assessment.groups and len(output.order_assessment.unassessed_mention_ids) == 4
    else:
        group = output.order_assessment.groups[0]
        assert group.hard_precedence == ((output.mentions[0].mention_id, output.mentions[1].mention_id),)


@pytest.mark.parametrize('problem', ['cycle', 'wrong_kind', 'optional_member', 'cross_day_members', 'wrong_revisit_scope'])
def test_range_cannot_relax_existing_order_or_member_rejections(problem):
    from tests.test_order_assessment_coverage import two_day_revisit
    from tests.test_source_order_runtime import prepared
    source, value = prepared('REQUIRED_PRECEDENCE')
    if problem == 'cycle':
        value['order_groups'][0]['required_precedence'].append(dict(before_index=1, after_index=0,
            evidence='必须先星河公园再月光公园'))
    elif problem == 'wrong_kind':
        value['order_groups'][0]['kind'] = 'INITIAL_ORDER'
    elif problem == 'optional_member':
        value['activities'][1]['role'] = 'OPTIONAL'
    else:
        source, value = two_day_revisit()
        for item in value['activities']:
            item['source_details'] = []
        if problem == 'cross_day_members':
            value['order_groups'] = [dict(kind='INITIAL_ORDER', activity_indices=[0, 2], scope_quote=source)]
        else:
            value['order_groups'] = [dict(kind='INITIAL_ORDER', activity_indices=[2, 3],
                scope_quote=value['order_groups'][0]['scope_quote'])]
    wire = compact_draft_payload(SemanticDraft.model_validate(value), source=source)
    output = assess(source, expand_compact_payload(wire, source=source))
    assert output == assess(source, value)
    assert not output.order_assessment.groups
    assert output.order_assessment.unassessed_mention_ids


@pytest.mark.parametrize('bad', ['unknown_field', 'missing_kind', 'bad_indices', 'missing_indices'])
def test_malformed_group_does_not_free_overlapping_members_or_drop_visits(bad):
    source, value = source_pair()
    wire = ranged(value, anchor('Day1：'), anchor('月光桥。'))
    wrong = copy.deepcopy(wire['order_groups'][0])
    if bad == 'unknown_field':
        wrong['extra_instruction'] = 'ignored'
    elif bad == 'missing_kind':
        wrong.pop('kind')
    elif bad == 'missing_indices':
        wrong.pop('activity_indices')
    else:
        wrong['activity_indices'] = ['0', 1]
    wire['order_groups'].append(wrong)
    output = assess(source, expand_compact_payload(wire, source=source))
    assert len(output.mentions) == 2 and len(output.order_assessment.unassessed_mention_ids) == 2
    assert not output.order_assessment.groups


def test_bad_group_keeps_an_independent_day_group_valid():
    source = '北京。\nDay1：青溪公园。\nDay2：月光桥。'
    value = payload([row('青溪公园'), row('月光桥', day=2)], days=2)
    value['order_groups'] = [dict(kind='INITIAL_ORDER', activity_indices=[i], scope_quote=f'Day{i+1}：{name}。')
        for i, name in enumerate(['青溪公园', '月光桥'])]
    wire = compact_draft_payload(SemanticDraft.model_validate(value), source=source)
    wire['order_groups'][1]['scope_range']['end']['quote'] = '不存在的尾部'
    output = assess(source, expand_compact_payload(wire, source=source))
    assert output.order_assessment.groups[0].member_mention_ids == (output.mentions[0].mention_id,)
    assert output.order_assessment.unassessed_mention_ids == (output.mentions[1].mention_id,)


@pytest.mark.parametrize('source,scope', [
    ('星河公园、月光桥。\n星河公园、月光桥。', dict(start=anchor('星河公园', 2), end=anchor('月光桥。', 2))),
    ('Day1：星河公园。\r\nDay2：月光桥。', dict(start=anchor('星河公园。\nDay2'), end=anchor('月光桥。'))),
    ('Day1：**星河公园**、月光桥。', dict(start=anchor('Day1：星河公园'), end=anchor('月光桥。'))),
    ('起点' + '说明' * 2500 + '终点', dict(start=anchor('起点'), end=anchor('终点'))),
])
def test_range_rejects_repeated_scope_implicit_normalization_and_old_size_overflow(source, scope):
    with pytest.raises(ValueError):
        expand_order_scope(source, scope)


def test_choice_evidence_and_optional_roles_are_not_range_encoded():
    source = '北京。\nDay1：青溪公园。另选月光桥或晨光亭。'
    value = payload([row('青溪公园'), row('月光桥', role='OPTIONAL'), row('晨光亭', role='OPTIONAL')],
        choice_groups=[dict(scope_quote='月光桥或晨光亭', branches=[dict(activity_indices=[1]), dict(activity_indices=[2])])])
    value['order_groups'] = [dict(kind='INITIAL_ORDER', activity_indices=[0], scope_quote='Day1：青溪公园。')]
    wire = compact_draft_payload(SemanticDraft.model_validate(value), source=source)
    assert wire['choice_groups'] == value['choice_groups']
    restored = expand_compact_payload(wire, source=source)
    assert SemanticDraft.model_validate(restored) == SemanticDraft.model_validate(value)
    assert assess(source, restored) == assess(source, value)


@pytest.mark.asyncio
@pytest.mark.parametrize('header_first', [False, True])
async def test_cut_range_never_completes_order_and_timeout_keeps_closed_activities(header_first):
    source, value = source_pair()
    blocks = compact(value)['blocks']
    start = json.dumps(dict(blocks=blocks, destination='北京', day_labels=[None], unprocessed_quotes=[]), ensure_ascii=False)[:-1]
    group = ('"kind":"INITIAL_ORDER","activity_indices":[0,1],' if header_first else '')
    text = start + ',"order_groups":[{' + group + '"scope_range":{"start":{"quote":"Day1：","occurrence":1},"end":{"quote":"月'
    recovered = complete_compact_items_from_truncated_json(text)
    assert 'order_groups' not in recovered
    output, client = await run(source, (text, 'length'), TimeoutError())
    assert [c.name for c in output.public_result.days[0].activities] == ['青溪公园', '月光桥']
    assert len(output.proposal.order_assessment.unassessed_mention_ids) == 2
    assert not output.public_result.coverage.complete
    assert output.proposal.binding['external_calls'] == len(client.calls) == 2


def test_encoder_does_not_repair_an_actual_nonliteral_old_scope():
    source, value = source_pair()
    value['order_groups'][0]['scope_quote'] = '改写过的原文范围'
    wire = compact_draft_payload(SemanticDraft.model_validate(value), source=source)
    assert wire['order_groups'] == value['order_groups']
    assert len(assess(source, expand_compact_payload(wire, source=source)).order_assessment.unassessed_mention_ids) == 2
    with pytest.raises(ValueError):
        compact_order_scope(source, '改写过的原文范围')


@pytest.mark.parametrize('blocks', [None, {}, 1, 'invalid'])
def test_malformed_blocks_reaches_the_original_type_validation(blocks):
    source, value = source_pair()
    wire = ranged(value, anchor('Day1：'), anchor('月光桥。'))
    wire['blocks'] = blocks
    with pytest.raises(ValidationError) as error:
        expand_compact_payload(wire, source=source)
    assert any(e['loc'] == ('blocks',) for e in error.value.errors())


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing_occurrence', 'nonliteral', 'unknown_field'])
async def test_bad_range_keeps_confirmed_public_activities_but_not_complete(failure):
    source, value = source_pair()
    wire = ranged(value, anchor('Day1：'), anchor('月光桥。'))
    if failure == 'missing_occurrence':
        del wire['order_groups'][0]['scope_range']['end']['occurrence']
    elif failure == 'nonliteral':
        wire['order_groups'][0]['scope_range']['end']['quote'] = '没有这段文字'
    else:
        wire['order_groups'][0]['guessed'] = True
    output, client = await run(source, wire)
    assert [(c.name, c.status) for c in output.public_result.days[0].activities] == [
        ('青溪公园', 'READY'), ('月光桥', 'READY')]
    assert len(output.proposal.order_assessment.unassessed_mention_ids) == 2
    assert output.proposal.unprocessed_count > 0 and not output.public_result.coverage.complete
    assert len(client.calls) == 1


def test_bad_range_preserves_the_original_eighty_pending_fragments_without_trimming_or_overflow():
    source, value = source_pair()
    wire = ranged(value, anchor('Day1：'), anchor('月光桥。'))
    wire['order_groups'][0]['scope_range']['end']['quote'] = '没有这段文字'
    wire['unprocessed_quotes'] = ['青溪公园'] * 80
    restored = expand_compact_payload(wire, source=source)
    assert restored['unprocessed_quotes'] == wire['unprocessed_quotes']
    assert len(SemanticDraft.model_validate(restored).activities) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('length', [1200, 50000])
async def test_bad_range_is_global_and_does_not_cover_known_omissions_in_long_source(length):
    source, value = source_pair()
    source += '随后游览故宫博物院。\n'
    source += '说明' * ((length - len(source)) // 2) + '。' * ((length - len(source)) % 2)
    assert len(source) == length
    wire = ranged(value, anchor('Day1：'), anchor('不存在的范围'))
    assert expand_compact_payload(wire, source=source)['unprocessed_quotes'] == []
    output, client = await run(source, wire)
    known = [d for d in output.proposal.diagnostics if d.category == 'KNOWN_PLACE_UNCLASSIFIED']
    order = [d for d in output.proposal.diagnostics if d.category == 'ORDER_EVIDENCE_UNPROCESSED']
    assert any(source[d.span_start:d.span_end] == '故宫博物院' for d in known)
    assert len(order) == 1 and order[0].span_start is None and order[0].span_end is None
    assert output.proposal.unprocessed_count == len(known) + 1
    assert sum(output.proposal.unprocessed_by_day.values()) == len(known)
    assert [c.name for c in output.public_result.days[0].activities] == ['青溪公园', '月光桥']
    assert not output.public_result.coverage.complete and len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['UNKNOWN', 'omitted'])
async def test_compact_explicit_unknown_or_missing_assessment_is_not_invalid_evidence(kind):
    source, value = source_pair()
    value['order_groups'][0]['kind'] = 'UNKNOWN'
    wire = compact_draft_payload(SemanticDraft.model_validate(value), source=source)
    if kind == 'omitted':
        wire['order_groups'] = []
    output, _ = await run(source, wire)
    assert not output.proposal.diagnostics and output.proposal.unprocessed_count == 0
    assert output.public_result.coverage.complete
    assert not output.proposal.order_assessment.groups


@pytest.mark.asyncio
async def test_repaired_range_does_not_inherit_a_first_answer_failure_marker():
    source, value = source_pair()
    wrong = ranged(value, anchor('Day1：'), anchor('不存在的范围'))
    correct = ranged(value, anchor('Day1：'), anchor('月光桥。'))
    # A closed first reply with a truncated finish reason consumes the ordinary
    # existing second attempt; no new order-specific request is introduced.
    output, client = await run(source, (wrong, 'length'), correct)
    assert [c.name for c in output.public_result.days[0].activities] == ['青溪公园', '月光桥']
    assert len(output.proposal.order_assessment.groups) == 1
    assert output.proposal.unprocessed_count == 0 and output.public_result.coverage.complete
    assert len(client.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['INITIAL_ORDER', 'REQUIRED_PRECEDENCE'])
async def test_bad_range_preserves_first_hard_edges_until_the_same_constraints_are_repaired(kind):
    from tests.test_source_order_runtime import prepared
    source, value = prepared('REQUIRED_PRECEDENCE')
    wrong = compact_draft_payload(SemanticDraft.model_validate(value), source=source)
    wrong['order_groups'][0]['scope_range']['end']['quote'] = '错误尾部'
    expanded = expand_compact_payload(wrong, source=source)
    assert expanded['order_groups'][0]['kind'] == 'REQUIRED_PRECEDENCE'
    assert expanded['order_groups'][0]['required_precedence'] == value['order_groups'][0]['required_precedence']
    correct = compact_draft_payload(SemanticDraft.model_validate(value), source=source)
    correct['order_groups'][0]['kind'] = kind
    if kind == 'INITIAL_ORDER':
        correct['order_groups'][0]['required_precedence'] = []
    output, client = await run(source, (wrong, 'length'), correct)
    assert [c.name for c in output.public_result.days[0].activities] == [a['place_name'] for a in value['activities']]
    assert len(client.calls) == 2
    if kind == 'INITIAL_ORDER':
        assert not output.proposal.order_assessment.groups
        assert output.proposal.unprocessed_count > 0 and not output.public_result.coverage.complete
    else:
        group = output.proposal.order_assessment.groups[0]
        assert len(group.hard_precedence) == 1
        assert output.proposal.unprocessed_count == 0 and output.public_result.coverage.complete


@pytest.mark.asyncio
@pytest.mark.parametrize('second_error', ['wrong_day', 'wrong_revisit', 'bad_quote', 'missing_member'])
async def test_second_range_cannot_clear_pending_for_different_or_invalid_visits(second_error):
    source, value = source_pair()
    wrong = ranged(value, anchor('Day1：'), anchor('不存在的范围'))
    correct = ranged(value, anchor('Day1：'), anchor('月光桥。'))
    if second_error == 'wrong_day':
        correct['blocks'][0]['day_index'] = 2
    elif second_error == 'wrong_revisit':
        correct['blocks'][0]['items'][0]['occurrence'] = 2
    elif second_error == 'bad_quote':
        correct['blocks'][0]['items'][0]['quote'] = '假的原文'
    else:
        correct['order_groups'][0]['activity_indices'] = [0]
    output, client = await run(source, (wrong, 'length'), correct)
    assert [c.name for c in output.public_result.days[0].activities] == ['青溪公园', '月光桥']
    assert not output.public_result.coverage.complete and output.proposal.unprocessed_count > 0
    assert len(client.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('distinct_evidence', [False, True])
async def test_duplicate_hard_edge_uses_validated_wire_evidence_without_parallel_zip(distinct_evidence):
    from tests.test_source_order_runtime import prepared
    source, value = prepared('REQUIRED_PRECEDENCE')
    group = value['order_groups'][0]
    duplicate = dict(group['required_precedence'][0])
    if distinct_evidence:
        duplicate['evidence'] = group['scope_quote']
    group['required_precedence'].append(duplicate)
    bound = assess(source, value).order_assessment.groups[0]
    assert len(bound.hard_precedence) == 1 and len(bound.evidence_spans) == 2
    # Preserve a reproduction of the old parallel-list assumption, not a
    # change to the existing order binder's duplicate handling.
    with pytest.raises(ValueError):
        list(zip(bound.hard_precedence, bound.evidence_spans, strict=True))
    correct = compact_draft_payload(SemanticDraft.model_validate(value), source=source)
    wrong = copy.deepcopy(correct)
    wrong['order_groups'][0]['scope_range']['end']['quote'] = '不存在的范围'
    output, client = await run(source, (wrong, 'length'), correct)
    assert [c.name for c in output.public_result.days[0].activities] == [a['place_name'] for a in value['activities']]
    assert output.proposal.order_assessment.groups == (bound,)
    assert output.public_result.coverage.complete and len(client.calls) == 2
