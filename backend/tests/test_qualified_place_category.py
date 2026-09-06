"""A campus qualifier cannot erase its parent venue's explicit category."""
from __future__ import annotations

import httpx
import pytest

from app.schemas.place import PlaceCategory
from app.trip_understanding.amap_place import AmapPlaceResolver, _evaluate_candidates


_VENUE_TYPES = [
    ("博物馆", "140100", "科教文化服务;博物馆;博物馆"),
    ("美术馆", "140400", "科教文化服务;美术馆;美术馆"),
    ("科技馆", "140600", "科教文化服务;科技馆;科技馆"),
    ("图书馆", "140500", "科教文化服务;图书馆;图书馆"),
]


def _poi(name: str, code: str, label: str) -> dict:
    return {
        "id": "synthetic-qualified-venue",
        "name": name,
        "typecode": code,
        "type": label,
        "cityname": "上海市",
        "pname": "上海市",
        "adname": "浦东新区",
        "adcode": "310115",
        "location": "121.54,31.22",
    }


def _decision(query: str, row: dict):
    return _evaluate_candidates(
        [row], city="上海", canonical_name=query, safe_aliases=(),
        expected_category=PlaceCategory.ATTRACTION,
        expected_district="浦东新区", atomic=query,
    )


@pytest.mark.parametrize("kind,code,label", _VENUE_TYPES)
@pytest.mark.parametrize("qualifier", ["南馆", "人民广场馆", "之江馆区"])
@pytest.mark.parametrize("source_has_parentheses", [False, True])
def test_qualified_venue_keeps_parent_kind_while_accepting_same_scope_formatting(
    kind, code, label, qualifier, source_has_parentheses,
):
    parent = "星河" + kind
    query = parent + (f"（{qualifier}）" if source_has_parentheses else qualifier)
    provider_name = parent + (qualifier if source_has_parentheses else f"({qualifier})")

    correct = _decision(query, _poi(provider_name, code, label))
    assert correct.selected is not None
    assert correct.selected.raw["name"] == provider_name

    wrong_code, wrong_label = (
        ("140100", "科教文化服务;博物馆;博物馆")
        if kind == "图书馆"
        else ("140500", "科教文化服务;图书馆;图书馆")
    )
    conflicting = _decision(query, _poi(provider_name, wrong_code, wrong_label))
    assert conflicting.selected is None


@pytest.mark.parametrize("name", ["星河博物院孤山馆区", "星河博物院（孤山馆区）"])
def test_museum_institute_remains_same_venue_kind_for_a_named_campus(name):
    result = _decision(name, _poi("星河博物院(孤山馆区)", "140100", "科教文化服务;博物馆;博物馆"))
    assert result.selected is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("code,label,accepted", [
    ("140100", "科教文化服务;博物馆;博物馆", True),
    ("140500", "科教文化服务;图书馆;图书馆", False),
])
async def test_campus_type_conflict_cannot_become_auto_matched(code, label, accepted):
    def respond(_request):
        return httpx.Response(200, json={
            "status": "1", "infocode": "10000",
            "pois": [_poi("星河博物馆(南馆)", code, label)],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        outcome = await AmapPlaceResolver(api_key="synthetic-only", client=client).resolve(
            city="上海", atomic_place_name="星河博物馆南馆", category_hint="景点",
        )

    assert (outcome.place is not None) is accepted
    assert (outcome.receipt.get("status") == "AUTO_MATCHED") is accepted
