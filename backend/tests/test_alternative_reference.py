from types import SimpleNamespace as NS

import pytest

from app.trip_understanding.conditional_replacement import is_implicit_alternative_reference


@pytest.mark.parametrize('condition,tail,role,expected', [
    ('如果下雨', '暂按晴天主线整理，雨天方案保留为备选。', 'OPTIONAL', True),
    ('如果下雨', '雨天方案保留为备选。', 'OPTIONAL', True),
    ('如果下雨', '晴天方案保留为备选。', 'OPTIONAL', False),
    ('如果人多', '雨天方案保留为备选。', 'OPTIONAL', False),
    ('如果下雨', '雨天方案保留为备选，再去公园。', 'OPTIONAL', False),
    ('如果下雨', '雨天方案保留为备选。', 'PLANNED', False),
    ('如果下雨', 'Day2：雨天方案保留为备选。', 'OPTIONAL', False),
])
def test_reference_requires_adjacent_validated_rain_alternative(condition, tail, role, expected):
    prefix = '如果下雨，就把公园替换成城市规划馆；'
    source = prefix + tail
    target = NS(parent_mention_id=None, span_end=prefix.index('馆') + 1, role=role,
                replaces_mention_id='park', replacement_condition=condition, raw_text='城市规划馆', day_index=1)
    segment = {'start': len(prefix), 'text': tail}
    proposal = NS(mentions=[target])
    assert is_implicit_alternative_reference(source, proposal, segment, '城市规划馆', day_index=1) is expected
    assert not is_implicit_alternative_reference(source, proposal, segment, '另一座馆', day_index=1)
