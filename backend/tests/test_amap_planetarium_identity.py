"""Recorded public AMap rows: a planetarium is not one of its exhibits."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.constraints.amap_types import classify_amap_type_signals
from app.schemas.place import PlaceCategory
from app.trip_understanding import candidates
from app.trip_understanding.amap_place import AmapPlaceResolver


SNAPSHOT = json.loads((Path(__file__).parent / "fixtures/amap_beijing_planetarium.json").read_text(encoding="utf-8"))
PARENT_ID = "B000A88EDQ"


def _rows():
    return deepcopy(SNAPSHOT["pois"])


def _client(rows, observed):
    async def respond(request):
        observed.append(request)
        # The saved first query excluded 140700; its no-types retry had the parent.
        filtered = rows
        types = request.url.params.get("types")
        if types and "140700" not in types.split("|"):
            filtered = [row for row in rows if row["typecode"] != "140700"]
        return httpx.Response(200, json={"status": "1", "infocode": "10000", "pois": filtered})
    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


@pytest.mark.asyncio
@pytest.mark.parametrize("hint", ["景点", None])
async def test_recorded_planetarium_is_automatically_the_parent(hint):
    observed = []
    async with _client(_rows(), observed) as client:
        result = await AmapPlaceResolver(api_key="fixed-response-only", client=client).resolve(
            city="北京", atomic_place_name="北京天文馆", category_hint=hint,
        )
    assert result.place and result.place.canonical_place_id == PARENT_ID
    assert result.place.name == "北京天文馆"
    assert result.place.area_or_address == "西直门外大街138号"
    assert len(observed) == 1
    assert "140700" in observed[0].url.params["types"].split("|")


@pytest.mark.asyncio
async def test_manual_search_ranks_parent_first_and_preserves_distinct_children(monkeypatch):
    observed = []
    async with _client(_rows(), observed) as client:
        monkeypatch.setattr(candidates, "get_settings", lambda: SimpleNamespace(
            amap_api_key="fixed-response-only", trip_understanding_provider_mode="live"))
        monkeypatch.setattr(candidates, "AmapPlaceResolver", lambda **kwargs: AmapPlaceResolver(**kwargs, client=client))
        result = await candidates.search_candidates(city="北京", query="北京天文馆", category_hint="景点")
    assert result and result[0].canonical_place_id == "amap:" + PARENT_ID
    assert result[0].name == "北京天文馆"
    child = next(item for item in result if item.name == "北京天文馆-A馆展区")
    assert child.canonical_place_id == "amap:B0FFFE4I06"
    assert child.canonical_place_id != result[0].canonical_place_id
    assert len(observed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing", "different_name", "wrong_city", "ambiguous", "child_alias", "parent_for_child", "category_conflict"])
async def test_planetarium_identity_boundaries_remain_pending(mutation):
    rows = _rows()
    parent = next(row for row in rows if row["id"] == PARENT_ID)
    query = "北京天文馆"
    if mutation in {"missing", "child_alias"}:
        rows = [row for row in rows if row["id"] != PARENT_ID]
        if mutation == "child_alias":
            for row in rows:
                if row["typecode"] == "140700":
                    row["alias"] = query
    elif mutation == "different_name":
        parent["name"] = "星河天文馆"
    elif mutation == "wrong_city":
        parent.update(pname="上海市", cityname="上海市", adname="浦东新区", adcode="310115", location="121.48,31.23")
    elif mutation == "ambiguous":
        rows.append({**parent, "id": "synthetic-distinct-parent"})
    elif mutation == "parent_for_child":
        rows = [parent]
        parent["alias"] = "北京天文馆-A馆展区"
        query = parent["alias"]
    elif mutation == "category_conflict":
        parent["type"] = "科教文化服务;图书馆;图书馆"
    async with _client(rows, []) as client:
        result = await AmapPlaceResolver(api_key="fixed-response-only", client=client).resolve(
            city="北京", atomic_place_name=query, category_hint="景点",
        )
    assert result.place is None


@pytest.mark.asyncio
@pytest.mark.parametrize("child_id", ["B0FFFE4I06", "B000A8486U", "B000A87752"])
async def test_each_child_keeps_its_own_identity_despite_a_parent_alias(child_id):
    child = next(row for row in _rows() if row["id"] == child_id)
    child["alias"] = "北京天文馆"
    async with _client([child], []) as client:
        provider = AmapPlaceResolver(api_key="fixed-response-only", client=client)
        parent_result = await provider.resolve(city="北京", atomic_place_name="北京天文馆", category_hint="景点")
        child_result = await provider.resolve(city="北京", atomic_place_name=child["name"], category_hint="景点")
    assert parent_result.place is None
    assert child_result.place and child_result.place.canonical_place_id == child_id
    assert child_result.place.name == child["name"]


@pytest.mark.parametrize("code,label,complete,category", [
    ("140700", "科教文化服务;天文馆;天文馆", True, PlaceCategory.ATTRACTION),
    ("140000", "科教文化服务;科教文化场所;科教文化场所", False, PlaceCategory.UNKNOWN),
    ("141200", "科教文化服务;学校;学校", False, PlaceCategory.UNKNOWN),
    ("140600", "科教文化服务;科技馆;科技馆", True, PlaceCategory.ATTRACTION),
    ("140100", "科教文化服务;博物馆;博物馆", True, PlaceCategory.ATTRACTION),
])
def test_only_the_visitor_facing_planetarium_subtype_is_added(code, label, complete, category):
    signals = classify_amap_type_signals(code, label)
    assert signals.complete is complete
    assert signals.category is category
