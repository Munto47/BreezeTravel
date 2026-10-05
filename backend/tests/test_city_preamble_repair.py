"""A city-only repair can use the source's explicit document scope."""
import pytest

from app.trip_understanding.city_metadata import CityMetadataPatch, apply_city_metadata
from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft


@pytest.mark.parametrize('preamble,city,expected', [
    ('深圳两天亲子路线。', '深圳', True),
    ('从北京出发到深圳。', '北京', False),
    ('从北京出发到深圳。', '深圳', False),
    ('北京路步行街附近走走。', '北京', False),
    ('喜欢深圳菜。', '深圳', False),
    ('北京两天亲子路线。', '深圳', False),
])
def test_preamble_repair_reuses_city_scope_checks_and_preserves_visit(preamble, city, expected):
    source = preamble + '\nDay1：青溪公园。'
    draft = SemanticDraft.model_validate(dict(destination=city, activities=[dict(source_quote='青溪公园',
        place_name='青溪公园', role='PLANNED', day_index=1, city=city, city_evidence='青溪公园')]))
    before = _proposal_from_live_draft(source, draft, allow_partial=True)
    _, after, count = apply_city_metadata(source, draft, before, [CityMetadataPatch(
        index=0, city=city, city_evidence='青溪公园')])
    assert count == int(expected)
    assert after.mentions[0].model_dump(exclude={'city_hint', 'city_evidence'}) == before.mentions[0].model_dump(exclude={'city_hint', 'city_evidence'})
    if expected:
        assert after.mentions[0].city_evidence == preamble
        assert not after.diagnostics
    else:
        assert after.diagnostics == before.diagnostics
