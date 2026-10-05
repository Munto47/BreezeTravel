"""Actual raw replay and controlled repairs; all place identities are fixed."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_experience_source_anchors import CapturedClient, provider
from tests.test_semantic_day_sections import RecordingPlaces


class FixedCities(RecordingPlaces):
    async def resolve(self, **query):
        if query["city"] not in {"北京", "上海"}:
            return None
        return await super().resolve(**query)


def row(name, day=1, city="北京", evidence="城市核心", **extra):
    return dict(source_quote=name, place_name=name, role="PLANNED", day_index=day,
                city=city, city_evidence=evidence, category="景点", **extra)


async def run(source, first, second, places=None, *, relative_only=True):
    client = CapturedClient(first, second)
    places = places or FixedCities()
    output = await TripUnderstandingPipeline(provider(client, relative_only=relative_only), places).run(source)
    assert len(client.calls) == 2
    assert output.inference_binding["repair_call_count"] == 1
    return output, places


@pytest.mark.asyncio
@pytest.mark.parametrize("corrected", [False, True])
async def test_saved_owner_city_failure_repairs_without_rewriting_original_itinerary(corrected):
    fixture = json.loads((Path(__file__).parent / "fixtures/live_owner_invalid_city_evidence.json").read_text(encoding="utf-8"))
    first = fixture["model_response"]
    # Explicit city-only patch protocol; a whole itinerary is not a valid patch.
    # The saved 22 raw rows expand two literal place lists into 24 current
    # draft slots. Patch indices address that current draft, not the raw list.
    second = {"activities": [{"index": index, "city": None if corrected else "北京",
        "city_evidence": None if corrected else "皇城核心"} for index in range(24)]}
    output, places = await run(fixture["source"], first, second)
    assert [[card.name for card in day.activities] for day in output.public_result.days] == [
        ["天安门广场", "故宫博物院", "景山公园", "什刹海", "后海"],
        ["鸟巢", "水立方"],
        ["天坛公园", "国家博物馆", "前门大街", "大栅栏", "杨梅竹斜街"],
    ]
    assert output.public_result.coverage.confirmed_place_count == (12 if corrected else 0)
    assert output.public_result.coverage.unresolved_place_count == (0 if corrected else 12)
    assert len(places.calls) == (12 if corrected else 0)
    assert all(city == "北京" for city, _name in places.calls)
    assert sum(issue.category == "UNSUPPORTED_CITY_REMOVED" for issue in output.proposal.diagnostics) == (0 if corrected else 24)
    assert any(issue.category == "KNOWN_PLACE_UNCLASSIFIED" for issue in output.proposal.diagnostics)
    assert output.public_result.coverage.complete is False  # Other raw omissions remain.
    for item in output.proposal.mentions:
        assert fixture["source"][item.span_start:item.span_end] == item.raw_text


@pytest.mark.asyncio
async def test_independent_invalid_city_quote_gets_one_bounded_repair():
    first = {"destination": "北京", "activities": [row("故宫博物院")]}
    second = {"activities": [{"index": 0, "city": "北京", "city_evidence": "北京一日游"}]}
    output, places = await run("北京一日游。\nDay1｜城市核心：故宫博物院。", first, second)
    assert places.calls == [("北京", "故宫博物院")]
    assert output.public_result.coverage.complete is True
    assert output.proposal.mentions[0].city_evidence == "北京一日游"


@pytest.mark.asyncio
async def test_repair_timeout_keeps_original_safe_names_and_city_warning():
    first = {"destination": "北京", "activities": [row("故宫博物院", city="上海", evidence="上海城市核心")]}
    output, places = await run("北京一日游。\nDay1｜城市核心：故宫博物院。", first, TimeoutError())
    assert places.calls == []
    assert [card.name for card in output.public_result.days[0].activities] == ["故宫博物院"]
    assert output.public_result.coverage.complete is False
    assert output.public_result.coverage.unprocessed_count == 1


@pytest.mark.asyncio
async def test_clearing_city_evidence_cannot_promote_a_city_inside_a_place_name():
    first = {"destination": "北京", "activities": [row("北京路步行街", evidence="北京路步行街")]}
    second = {"activities": [{"index": 0, "city": None, "city_evidence": None}]}
    output, places = await run("Day1：北京路步行街。", first, second)
    assert places.calls == []
    assert output.public_result.coverage.unresolved_place_count == 1
    assert output.public_result.coverage.complete is False


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_kind", ["clear", "wrong_day", "wrong_city"])
async def test_city_repair_cannot_absorb_an_unassigned_stop_into_document_city(repair_kind):
    source = "Day1 北京：星河公园。\nDay2 上海：月光桥。"
    first = {"destination": "北京", "activities": [row("星河公园"), row("月光桥", 2, "上海", "Day2 上海")]}
    replacement = {"clear": (None, None), "wrong_day": ("上海", "Day2 上海"), "wrong_city": ("杭州", "Day1 北京")}[repair_kind]
    second = {"activities": [{"index": 0, "city": replacement[0], "city_evidence": replacement[1]}]}
    output, places = await run(source, first, second)
    assert places.calls == [("上海", "月光桥")]
    assert [(item.atomic_place_name, item.day_index, item.role.value) for item in output.proposal.mentions] == [
        ("星河公园", 1, "PLANNED"), ("月光桥", 2, "PLANNED")]
    assert output.public_result.coverage.complete is False
    assert output.public_result.days[0].activities[0].status == "NEEDS_CONFIRMATION"


@pytest.mark.asyncio
async def test_valid_city_evidence_and_source_district_survive_another_stop_repair():
    source = "Day1 北京：海淀区星河公园。\nDay2 上海：月光桥。"
    first = {"destination": "北京", "activities": [row("星河公园", evidence="Day1 北京"), row("月光桥", 2, "上海")]}
    second = {"activities": [
        {"index": 0, "city": "北京", "city_evidence": "北京"},
        {"index": 1, "city": "上海", "city_evidence": "Day2 上海"},
    ]}
    output, _places = await run(source, first, second, FixedCities(districts={"北京": "110101", "上海": "310101"}))
    assert output.proposal.mentions[0].city_evidence == "Day1 北京"
    assert output.proposal.mentions[1].city_evidence == "Day2 上海"
    assert output.public_result.days[0].activities[0].status == "NEEDS_CONFIRMATION"
    assert output.activities[0].resolver_receipt["failure_category"] == "SOURCE_DISTRICT_MISMATCH"
    assert output.public_result.days[1].activities[0].status == "READY"


@pytest.mark.asyncio
async def test_filtered_missing_name_row_cannot_shift_city_repair_to_a_valid_occurrence():
    source = "北京一日游。\nDay1：旅行开始，星河公园，月光桥。"
    first = {"destination": "北京", "activities": [
        {"source_quote": "旅行开始", "role": "REFERENCE"},
        row("星河公园", evidence="北京一日游"), row("月光桥"),
    ]}
    second = copy.deepcopy(first)
    second["activities"][0]["place_name"] = None
    second["activities"][1]["city_evidence"] = "北京"  # Valid but cannot replace the prior valid proof.
    second["activities"][2]["city_evidence"] = "北京一日游"
    output, _places = await run(source, first, second)
    named = [item for item in output.proposal.mentions if item.atomic_place_name]
    assert [(item.atomic_place_name, item.city_evidence) for item in named] == [
        ("星河公园", "北京一日游"), ("月光桥", "北京一日游")]
    assert output.public_result.coverage.complete is True


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_quote", ["星河公园", "游览星河公园"])
@pytest.mark.parametrize("relative_only", [False, True], ids=["legacy-time", "current-relative"])
async def test_filtered_missing_name_row_keeps_time_repair_on_its_original_occurrence(bad_quote, relative_only):
    source = "北京一日游。\nDay1：旅行开始，游览星河公园，10:00游览月光桥。"
    first = {"destination": "北京", "activities": [
        {"source_quote": "旅行开始", "role": "REFERENCE"},
        row("星河公园", evidence="北京一日游", start_time="09:00", timing_source="TEXT", time_evidence="不存在的预约时间"),
        row("月光桥", evidence="北京一日游", start_time="10:00", timing_source="TEXT", time_evidence="10:00游览月光桥"),
    ]}
    second = copy.deepcopy(first)
    first["activities"][1]["source_quote"] = bad_quote
    second["activities"][1]["source_quote"] = bad_quote
    second["activities"][0]["place_name"] = None
    for item in second["activities"][1:]:
        item.update(start_time=None, timing_source="UNSPECIFIED", time_evidence=None)
    output, places = await run(source, first, second, relative_only=relative_only)
    named = [item for item in output.proposal.mentions if item.atomic_place_name]
    assert [(item.atomic_place_name, item.day_index, item.role.value, item.city_hint, item.city_evidence)
            for item in named] == [
        ("星河公园", 1, "PLANNED", "北京", "北京一日游"),
        ("月光桥", 1, "PLANNED", "北京", "北京一日游"),
    ]
    assert [(item.span_start, item.span_end) for item in named] == [
        (source.index(name), source.index(name) + len(name)) for name in ("星河公园", "月光桥")]
    assert all(source[item.span_start:item.span_end] == item.raw_text == item.atomic_place_name for item in named)
    assert places.calls == [("北京", "星河公园"), ("北京", "月光桥")]
    assert [card.name for day in output.public_result.days for card in day.activities] == ["星河公园", "月光桥"]
    assert output.public_result.coverage.confirmed_place_count == 2
    if relative_only:
        assert all(item.start_time is None and item.end_time is None and item.visit_duration_minutes is None
                   and item.time_hint is None and not item.locked and not item.fixed_commitment
                   for item in output.proposal.mentions)
        first_errors = output.inference_binding["calls"][0]["validation_errors"]
        assert not any("TIME" in item["category"] for item in first_errors)
    else:
        # Legacy compatibility still protects the valid time on its own visit;
        # filtering the nameless row must not shift repair authority onto it.
        assert [(item.atomic_place_name, item.start_time) for item in named] == [("星河公园", None), ("月光桥", "10:00")]
    assert output.public_result.coverage.complete is True
    assert output.public_result.coverage.unprocessed_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("other_defect", ["missing_name", "invalid_time"])
async def test_a_still_invalid_second_answer_cannot_supply_a_city_repair(other_defect):
    source = "北京一日游。\nDay1：故宫博物院，景山公园。"
    first = {"destination": "北京", "activities": [row("故宫博物院", city="上海", evidence="上海城市核心"), row("景山公园", evidence="北京一日游")]}
    second = copy.deepcopy(first)
    second["activities"][0]["city_evidence"] = "北京一日游"
    second["activities"][0]["city"] = "北京"
    if other_defect == "missing_name":
        second["activities"][1].pop("place_name")
    else:
        second["activities"][1].update(start_time="09:00", timing_source="TEXT", time_evidence="不存在的预约时间")
    output, places = await run(source, first, second)
    assert places.calls == [("北京", "景山公园")]
    assert output.proposal.mentions[0].city_hint is None
    assert output.proposal.mentions[0].city_evidence == first["activities"][0]["city_evidence"]
    assert output.public_result.coverage.complete is False
    assert any(issue.category == "UNSUPPORTED_CITY_REMOVED" for issue in output.proposal.diagnostics)


def test_shared_multi_place_quote_does_not_authorize_timing_by_containment():
    from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
    from app.trip_understanding.semantic_recovery import merge_preserved_activities

    source = "北京一日游。\nDay1：星河公园、月光桥。"
    first = SemanticDraft.model_validate({"destination": "北京", "activities": [
        {**row("星河公园", evidence="北京一日游"), "source_quote": "星河公园、月光桥",
         "start_time": "09:00", "timing_source": "TEXT", "time_evidence": "不存在的时间"},
        {**row("月光桥", evidence="北京一日游"), "source_quote": "星河公园、月光桥"},
    ]})
    partial = _proposal_from_live_draft(source, first, allow_partial=True)
    second = first.model_copy(deep=True)
    second.activities[0] = second.activities[0].model_copy(update={"start_time": None, "timing_source": "UNSPECIFIED", "time_evidence": None})
    merged = merge_preserved_activities(source, first, partial, second)
    assert merged.activities[0].start_time == "09:00"  # Ambiguous diagnostic stays untrusted for final validation.
    assert merged.activities[1].start_time is None
    assert _proposal_from_live_draft(source, merged, allow_partial=True).unprocessed_count > 0
