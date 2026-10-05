"""Postposed touring actions retain named members without widening ownership."""
import pytest

from tests.test_source_visit_sections import supplement
from tests.test_source_visit_supplement import row


@pytest.mark.parametrize('action', ['划船', '泛舟', '散步', '漫步', '拍照', '游览'])
def test_parenthetical_named_location_retains_its_postposed_action(action):
    evidence = f'青溪公园（翠玉湖{action}看白石桥）'
    source = '北京。\nDay1：' + evidence + '，之后去文化街。'
    before, after = supplement(source, [('青溪公园', 1, 1), ('文化街', 1, 1)], [
        row(0, 'VISIT', '翠玉湖', evidence), row(0, 'VISIT', '白石桥', evidence)])
    assert after.mentions[:2] == before.mentions
    assert [m.atomic_place_name for m in after.mentions[2:]] == ['翠玉湖', '白石桥']
    assert all(m.parent_mention_id == before.mentions[0].mention_id for m in after.mentions[2:])
    assert not after.diagnostics


@pytest.mark.parametrize('passage', [
    '青溪公园（不在翠玉湖划船）',
    '青溪公园（取消翠玉湖划船）',
    '青溪公园（翠玉湖不划船）',
    '青溪公园（翠玉湖划船价格另计）',
    '青溪公园（介绍翠玉湖划船）',
    '青溪公园（曾经在翠玉湖划船）',
    '青溪公园（远眺翠玉湖划船）',
    '青溪公园（附近翠玉湖划船）',
    '青溪公园（离开园区去翠玉湖划船）',
    '青溪公园。\nDay2：翠玉湖划船',
])
def test_postposed_location_action_does_not_override_reference_or_scope(passage):
    source = '北京。\nDay1：' + passage
    before, after = supplement(source, [('青溪公园', 1, 1)], [row(0, 'VISIT', '翠玉湖', '翠玉湖')])
    assert after.mentions == before.mentions
    assert after.diagnostics


@pytest.mark.parametrize('brackets', [('（', '）'), ('(', ')')])
def test_named_internal_members_can_precede_their_touring_action(brackets):
    left, right = brackets
    evidence = f'从南门进入青溪公园，沿中轴线{left}星河亭、翠玉湖、白石桥{right}游览'
    source = '北京。\nDay1：青溪公园\n' + evidence + '，之后去文化街。'
    before, after = supplement(source, [('青溪公园', 1, 1), ('文化街', 1, 1)], [
        row(0, 'VISIT', name, evidence) for name in ['星河亭', '翠玉湖', '白石桥']])
    assert after.mentions[:2] == before.mentions
    assert [m.atomic_place_name for m in after.mentions[2:]] == ['星河亭', '翠玉湖', '白石桥']
    assert all(m.parent_mention_id == before.mentions[0].mention_id for m in after.mentions[2:])
    assert not after.diagnostics


@pytest.mark.parametrize('passage', [
    '沿中轴线（星河亭、翠玉湖）不游览',
    '不沿中轴线（星河亭、翠玉湖）游览',
    '不打算沿中轴线（星河亭、翠玉湖）游览',
    '中轴线（星河亭、翠玉湖）是介绍里的名称',
    '离开青溪公园，沿中轴线（星河亭、翠玉湖）游览',
    'Day2：沿中轴线（星河亭、翠玉湖）游览',
    '附近沿中轴线（星河亭、翠玉湖）游览',
])
def test_postposed_action_cannot_borrow_an_unrelated_or_denied_internal_scope(passage):
    source = '北京。\nDay1：青溪公园\n' + passage
    before, after = supplement(source, [('青溪公园', 1, 1)], [row(0, 'VISIT', '星河亭', passage)])
    assert after.mentions == before.mentions
    assert after.diagnostics
