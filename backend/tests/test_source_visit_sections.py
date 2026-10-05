"""Overview cards keep the details of their own numbered guide section."""
import pytest

from app.trip_understanding.source_visit_supplement import apply_source_visit_supplement
from tests.test_source_visit_supplement import plan, row


SOURCE = ("北京。\nDay1：故宫博物院→景山公园\n一、行程安排\n"
          "1.故宫博物院\n游玩时长：约三小时\n这里可以欣赏古建筑。\n"
          "建议参观太和殿、御花园，了解建筑文化。\n"
          "2.景山公园\n游玩时长：约一小时\n必打卡景点\n万春亭\n可欣赏城市风景。")


def supplement(source, parents, items):
    before = plan(source, parents)
    after = apply_source_visit_supplement(source, before, items,
        parent_ids=[m.mention_id for m in before.mentions if not m.parent_mention_id])
    return before, after


def test_multiline_numbered_details_attach_to_overview_without_creating_visits():
    before, after = supplement(SOURCE, [("故宫博物院", 1, 1), ("景山公园", 1, 1)], [
        row(0, "VISIT", "太和殿", "建议参观太和殿、御花园，了解建筑文化。"),
        row(0, "VISIT", "御花园", "建议参观太和殿、御花园，了解建筑文化。"),
        row(1, "VISIT", "万春亭", "必打卡景点\n万春亭"),
    ])
    assert after.mentions[:2] == before.mentions
    assert [(m.atomic_place_name, m.parent_mention_id) for m in after.mentions[2:]] == [
        ("太和殿", before.mentions[0].mention_id), ("御花园", before.mentions[0].mention_id),
        ("万春亭", before.mentions[1].mention_id)]
    assert not after.diagnostics


@pytest.mark.parametrize("boundary", ["2.景山公园", "2.另一个公园", "Day2", "二、其他建议", "## 另一条路线"])
def test_numbered_section_never_borrows_details_past_a_boundary(boundary):
    source = "北京。\nDay1：故宫博物院\n1.故宫博物院\n简介：古建筑。\n" + boundary + "\n必打卡景点\n观景台"
    before, after = supplement(source, [("故宫博物院", 1, 1)], [
        row(0, "VISIT", "观景台", "必打卡景点\n观景台")])
    assert after.mentions == before.mentions
    assert after.diagnostics


def test_repeated_visit_cannot_use_an_overview_name_as_ownership_proof():
    source = "北京。\nDay1：故宫博物院，然后去景山公园，再访故宫博物院。\n1.故宫博物院\n必打卡景点\n太和殿"
    before, after = supplement(source, [("故宫博物院", 1, 1), ("景山公园", 1, 1), ("故宫博物院", 1, 2)], [
        row(0, "VISIT", "太和殿", "必打卡景点\n太和殿")])
    assert after.mentions == before.mentions
    assert after.diagnostics


def test_numbered_guide_preserves_optional_and_viewing_constraints():
    source = ("北京。\nDay1：故宫博物院\n1.故宫博物院\n简介：古建筑。\n"
              "如果有时间，参观珍宝馆。\n眺望景山公园。")
    before, after = supplement(source, [("故宫博物院", 1, 1)], [
        row(0, "VISIT", "珍宝馆", "如果有时间，参观珍宝馆。", optional=True),
        row(0, "VISIT", "景山公园", "眺望景山公园。")])
    assert len(after.mentions) == 2
    assert after.mentions[1].atomic_place_name == "珍宝馆"
    assert after.mentions[1].role == "OPTIONAL"
    assert after.mentions[1].parent_mention_id == before.mentions[0].mention_id
    assert after.diagnostics


def test_day_overview_and_prose_share_the_same_visit_without_losing_internal_details():
    from app.trip_understanding.source_visit_sections import is_section_reference

    source = '北京。\n第二天留给青溪公园和文化街。先在青溪公园内看星河亭、翠玉湖；之后去文化街。'
    before, after = supplement(source, [('青溪公园', 2, 1), ('文化街', 2, 1)], [
        row(0, 'VISIT', name, '先在青溪公园内看星河亭、翠玉湖') for name in ['星河亭', '翠玉湖']])
    assert [m.atomic_place_name for m in after.mentions] == ['青溪公园', '文化街', '星河亭', '翠玉湖']
    assert all(m.parent_mention_id == before.mentions[0].mention_id for m in after.mentions[2:])
    assert not after.diagnostics
    for parent in before.mentions:
        start = source.rindex(parent.atomic_place_name)
        assert is_section_reference(source, parent, before.mentions, (start, start + len(parent.atomic_place_name)))


@pytest.mark.parametrize('placement,accepted', [('overview', False), ('body', False), ('later', True)])
def test_unnamed_meal_never_crashes_or_lends_a_narrative_parent(placement, accepted):
    from app.trip_understanding.source_visit_sections import narrative_visit_anchor

    overview = '第二天留给青溪公园和文化街'
    body = '先在青溪公园内'
    if placement == 'overview':
        overview += '，午餐'
    if placement == 'body':
        body += '午餐，再'
    source = '北京。\n' + overview + '。' + body + '看星河亭。'
    if placement == 'later':
        source += '午餐。'
    proposal = plan(source, [('青溪公园', 2, 1), ('文化街', 2, 1)])
    parent = proposal.mentions[0]
    start = source.index('午餐')
    meal = parent.model_copy(update={'mention_id': 'unnamed-meal', 'atomic_place_name': None,
        'raw_text': '午餐', 'span_start': start, 'span_end': start + 2, 'category_hint': '餐饮'})
    roots = [*proposal.mentions, meal]
    anchor = narrative_visit_anchor(source, parent, roots, source.index('星河亭'))
    assert bool(anchor) is accepted
    if accepted:
        assert anchor == (source.rindex('青溪公园'), source.rindex('青溪公园') + len('青溪公园'))


@pytest.mark.parametrize('overview,body', [
    ('第二天先去青溪公园再到文化街。', '先在青溪公园内看星河亭。'),
    ('第二天留给青溪公园和文化街。', '第三天先在青溪公园内看星河亭。'),
    ('第二天留给青溪公园和文化街。', '再次去青溪公园内看星河亭。'),
    ('第二天留给青溪公园和文化街。', '先在青溪公园内游览，再去文化街看星河亭。'),
])
def test_overview_scope_never_erases_revisits_day_or_external_stops(overview, body):
    source = '北京。\n' + overview + body
    before, after = supplement(source, [('青溪公园', 2, 1), ('文化街', 2, 1)], [row(0, 'VISIT', '星河亭', body)])
    assert after.mentions == before.mentions
    assert after.diagnostics


@pytest.mark.parametrize('route,day,accepted', [
    ('路线：青溪公园 —— 文化街', 'Day1：城市观光', True),
    ('游览路线：青溪公园→文化街', '第一天：城市观光', True),
    ('路线：先去青溪公园，再去文化街', 'Day1：城市观光', False),
    ('路线：青溪公园→文化街→青溪公园', 'Day1：城市观光', False),
    ('路线：青溪公园→文化街→未识别地点', 'Day1：城市观光', False),
    ('路线：青溪公园→文化街', 'Day2：城市观光', False),
    ('路线：青溪公园→文化街', '其他城市建议', False),
])
def test_route_line_below_day_title_owns_only_its_unique_numbered_section(route, day, accepted):
    source = f'北京。\n{day}\n{route}\n1. 青溪公园\n园内参观星河亭。\n2. 文化街\n自由散步。'
    before, after = supplement(source, [('青溪公园', 1, 1), ('文化街', 1, 1)], [
        row(0, 'VISIT', '星河亭', '园内参观星河亭。')])
    children = [m for m in after.mentions if m.parent_mention_id]
    assert bool(children) is accepted
    if accepted:
        assert children[0].parent_mention_id == before.mentions[0].mention_id
        assert not after.diagnostics
    else:
        assert after.diagnostics
