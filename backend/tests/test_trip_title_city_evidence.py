"""Tour titles scope visits without turning geographic POI words into cities."""
import pytest
from pydantic import ValidationError

from app.trip_understanding.experience_inference import SemanticActivity, SemanticDraft, _proposal_from_live_draft


@pytest.mark.parametrize('preamble,city,accepted', [
    ('导语：深圳海边轻松一日游路线来了。', '深圳', True),
    ('2026广州五一旅游攻略', '广州', True),
    ('北京三天文化旅行', '北京', True),
    ('从北京出发，到深圳一日游。', '北京', False),
    ('从北京出发，到深圳一日游。', '深圳', False),
    ('上海行程里尝尝北京菜，一日游。', '北京', False),
    ('北京大学一日游', '北京', False),
])
def test_undated_source_title_is_checked_against_all_city_evidence(preamble, city, accepted):
    source = preamble + '\n先去青溪公园，然后去星河广场。'
    draft = SemanticDraft(destination=city, activities=[dict(source_quote=n, place_name=n,
        role='PLANNED', day_index=1, city=city, city_evidence=preamble) for n in ['青溪公园','星河广场']])
    plan = _proposal_from_live_draft(source, draft, allow_partial=True)
    assert all((m.city_hint == city) is accepted for m in plan.mentions)


@pytest.mark.parametrize('suffix,accepted', [('湖划船', True), ('湖泛舟', True), ('湖州两地', False), ('湖北两地', False)])
def test_lake_activity_does_not_move_other_visits_to_another_city(suffix, accepted):
    source = '北京三天行程。\nDay2：青溪公园。昆明' + suffix + '。'
    draft = SemanticDraft(destination='北京', activities=[dict(source_quote='青溪公园', place_name='青溪公园',
        role='PLANNED', day_index=2, city='北京', city_evidence='北京三天行程')])
    plan = _proposal_from_live_draft(source, draft, allow_partial=True)
    assert (plan.mentions[0].city_hint == '北京') is accepted


def test_null_optional_lodging_list_does_not_consume_semantic_repair():
    base = dict(source_quote='青溪公园', place_name='青溪公园', role='PLANNED', day_index=1)
    assert SemanticActivity(**base, lodging_excluded_nights=None).lodging_excluded_nights == []
    assert SemanticActivity(**base, lodging_excluded_nights=[2]).lodging_excluded_nights == [2]
    for invalid in [[0], [15], 'none']:
        with pytest.raises(ValidationError):
            SemanticActivity(**base, lodging_excluded_nights=invalid)


@pytest.mark.parametrize('tail,accepted', [('北京路至沙面', True), ('北京路到沙面', True),
                                        ('北京路线与上海路线', False), ('北京到上海', False)])
def test_street_route_connector_does_not_become_a_destination_city(tail, accepted):
    source = '广州三天旅游攻略。\nDay1：青溪公园。交通：' + tail + '。'
    draft = SemanticDraft(destination='广州', activities=[dict(source_quote='青溪公园', place_name='青溪公园',
        role='PLANNED', day_index=1, city='广州', city_evidence='广州三天旅游攻略')])
    plan = _proposal_from_live_draft(source, draft, allow_partial=True)
    assert (plan.mentions[0].city_hint == '广州') is accepted
