from __future__ import annotations

from app.importing.confidence import candidate_confidence


def test_candidate_confidence_accepts_only_explicit_provider_aliases() -> None:
    candidate = {
        "place_id": "bj-badaling",
        "name": "八达岭长城",
        "city": "北京",
        "district": "延庆区",
    }

    without_alias, without_reasons = candidate_confidence(
        "长城（八达岭）", candidate, city="北京"
    )
    with_alias, with_reasons = candidate_confidence(
        "长城（八达岭）",
        {**candidate, "aliases": ["长城（八达岭）"]},
        city="北京",
    )

    assert without_alias < 0.90
    assert "NAME_ALIAS_EXACT" not in without_reasons
    assert with_alias >= 0.90
    assert "NAME_ALIAS_EXACT" in with_reasons
