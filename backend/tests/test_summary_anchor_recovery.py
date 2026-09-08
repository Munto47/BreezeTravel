"""Constructed replies reproduce saved summary/body duplicates, not lost live raw."""
from __future__ import annotations

from copy import deepcopy

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
from app.trip_understanding.semantic_recovery import _identity, merge_preserved_activities
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_city_evidence_recovery import FixedCities, run


SOURCE = "北京。\nDay1｜什刹海（皇城核心）\n傍晚去什刹海。之后去景山公园。"


def replies(*, reverse=False, name="什刹海", role="PLANNED"):
    first = {"destination": "北京", "day_labels": ["Day1"], "activities": [
        {"source_quote": name, "place_name": name, "occurrence": 2 if reverse else 1,
         "role": role, "day_index": 1, "city": "北京", "city_evidence": "皇城核心", "category": "景点"},
        {"source_quote": "景山公园", "place_name": "景山公园", "role": "PLANNED",
         "day_index": 1, "city": "北京", "city_evidence": "北京", "category": "景点"},
    ]}
    second = deepcopy(first)
    second["activities"][0].update(occurrence=1 if reverse else 2, city_evidence="北京")
    return first, second


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_summary_and_unique_body_repair_keeps_one_body_visit_and_valid_city(reverse):
    first, second = replies(reverse=reverse)
    # A mixed failure still uses the ordinary whole-answer repair. City-only
    # failures have their own field patch and cannot change source anchors.
    first["activities"][0].update(start_time="09:00", timing_source="TEXT",
                                  time_evidence="不存在的09点预约")
    assert _proposal_from_live_draft(SOURCE, SemanticDraft.model_validate(second)).unprocessed_count == 0
    result, places = await run(SOURCE, first, second)
    planned = [m for m in result.proposal.mentions if m.role.value == "PLANNED"]
    assert [(m.atomic_place_name, m.span_start, m.span_end) for m in planned] == [
        ("什刹海", 22, 25), ("景山公园", 29, 33)]
    assert all(m.city_hint == "北京" for m in planned)
    assert places.calls == [("北京", "什刹海"), ("北京", "景山公园")]
    assert [card.name for card in result.public_result.days[0].activities] == ["什刹海", "景山公园"]
    assert result.public_result.coverage.confirmed_place_count == 2
    assert not any(d.category == "UNSUPPORTED_CITY_REMOVED" for d in result.proposal.diagnostics)
    references = [m for m in result.proposal.mentions if m.role.value == "REFERENCE"]
    assert [(m.atomic_place_name, m.span_start, m.span_end) for m in references] == [("什刹海", 9, 12)]
    assert references[0].parent_mention_id is None
    assert result.public_result.coverage.complete is True


@pytest.mark.parametrize("defect", [
    "no_body", "revisit", "different_day", "role_conflict", "bad_quote",
    "plain_daily_route", "duplicate_title", "changed_plan", "different_name", "choice_title", "relative_day_reference",
])
def test_summary_repair_does_not_merge_unproven_occurrences(defect):
    source = SOURCE
    first, second = replies()
    if defect == "no_body":
        source = source.replace("傍晚去什刹海。", "傍晚休息。")
    elif defect == "revisit":
        source += "晚上再去什刹海。"
    elif defect == "different_day":
        source = source.replace("傍晚去", "Day2：傍晚去")
        second["activities"][0]["day_index"] = 2
    elif defect == "role_conflict":
        second["activities"][0]["role"] = "OPTIONAL"
    elif defect == "bad_quote":
        second["activities"][0]["source_quote"] = "没有这段引用"
    elif defect == "plain_daily_route":
        source = source.replace("Day1｜", "Day1：")
    elif defect == "duplicate_title":
        source += "\nDay1｜什刹海"
    elif defect == "changed_plan":
        source += "\n更正：前者改到第二天。"
    elif defect == "different_name":
        second["activities"][0]["place_name"] = "什刹海景区"
    elif defect == "choice_title":
        source = source.replace("Day1｜", "Day1｜备选方案：")
    elif defect == "relative_day_reference":
        source = source.replace("傍晚去什刹海", "昨日去过什刹海")
    original = SemanticDraft.model_validate(first)
    repaired = SemanticDraft.model_validate(second)
    safe = _proposal_from_live_draft(source, original, allow_partial=True)
    merged = merge_preserved_activities(source, original, safe, repaired)
    original_span = _identity(source, original.activities[0])
    assert any(_identity(source, item) == original_span and item.city_evidence == "皇城核心"
               for item in merged.activities)
    assert original.activities[0].occurrence == 1


@pytest.mark.asyncio
async def test_reverse_repair_cannot_borrow_a_city_from_the_summary():
    source = "## Day1 北京｜星河公园（皇城核心）\n抵达上海后去星河公园。"
    first, second = replies(reverse=True, name="星河公园")
    first["activities"] = first["activities"][:1]
    second["activities"] = second["activities"][:1]
    first["activities"][0].update(start_time="09:00", timing_source="TEXT", time_evidence="没有这段时间")
    result, places = await run(source, first, second, FixedCities())
    body = [m for m in result.proposal.mentions if m.atomic_place_name == "星河公园"
            and m.span_start == source.index("星河公园", source.index("\n") + 1)]
    assert body and body[0].city_hint is None
    assert ("北京", "星河公园") not in places.calls
    assert result.public_result.coverage.complete is False


@pytest.mark.parametrize("reverse", [False, True])
def test_bad_city_patch_does_not_become_valid_by_changing_the_source_occurrence(reverse):
    first, second = replies(reverse=reverse)
    second["activities"][0].update(city="上海", city_evidence="北京")
    original = SemanticDraft.model_validate(first)
    safe = _proposal_from_live_draft(SOURCE, original, allow_partial=True)
    merged = merge_preserved_activities(SOURCE, original, safe, SemanticDraft.model_validate(second))
    result = _proposal_from_live_draft(SOURCE, merged, allow_partial=True)
    visits = [m for m in result.mentions if m.atomic_place_name == "什刹海" and m.role.value == "PLANNED"]
    assert [(m.span_start, m.span_end, m.city_hint, m.city_evidence) for m in visits] == [
        (22, 25, None, "皇城核心")]
    assert result.unprocessed_count == 1


def test_reverse_alignment_preserves_valid_district_and_time_fields():
    source = "## Day1 北京海淀区｜星河公园\n09:00参观星河公园，停留60分钟，10:00离开。"
    first, second = replies(reverse=True, name="星河公园")
    first["activities"] = first["activities"][:1]
    first["activities"][0].update(city_evidence="Day1 北京海淀区", start_time="09:00", end_time="10:00",
        visit_duration_minutes=60, timing_source="TEXT", time_evidence="09:00参观星河公园，停留60分钟，10:00离开")
    second["activities"] = second["activities"][:1]
    original = SemanticDraft.model_validate(first)
    safe = _proposal_from_live_draft(source, original)
    assert safe.mentions[0].visit_duration_minutes == 60
    merged = merge_preserved_activities(source, original, safe, SemanticDraft.model_validate(second))
    result = _proposal_from_live_draft(source, merged)
    visit = next(m for m in result.mentions if m.role.value == "PLANNED")
    assert visit.city_evidence == "Day1 北京海淀区"
    assert (visit.start_time, visit.end_time, visit.visit_duration_minutes) == ("09:00", "10:00", 60)
    assert visit.span_start == source.index("星河公园", source.index("\n") + 1)


def test_same_named_visit_on_another_day_remains_separate():
    source = "北京。\nDay1｜星河公园\n下午去星河公园。\nDay2｜再访\n晚上再去星河公园。"
    first, second = replies(name="星河公园")
    first["activities"] = first["activities"][:1]
    second["activities"] = second["activities"][:1]
    return_visit = {**first["activities"][0], "day_index": 2, "occurrence": 3, "city_evidence": "北京"}
    first["activities"].append(return_visit)
    second["activities"].append(deepcopy(return_visit))
    original = SemanticDraft.model_validate(first)
    safe = _proposal_from_live_draft(source, original, allow_partial=True)
    merged = merge_preserved_activities(source, original, safe, SemanticDraft.model_validate(second))
    result = _proposal_from_live_draft(source, merged)
    visits = [m for m in result.mentions if m.role.value == "PLANNED"]
    assert [(m.atomic_place_name, m.day_index, m.span_start) for m in visits] == [
        ("星河公园", 1, source.index("星河公园", source.index("下午"))),
        ("星河公园", 2, source.index("星河公园", source.index("晚上"))),
    ]


def test_chinese_markdown_summary_keeps_optional_role_on_the_body():
    source = "北京。\n## **第一天**｜星河公园\n如果时间充裕，可以去星河公园。"
    first, second = replies(name="星河公园", role="OPTIONAL")
    first["activities"] = first["activities"][:1]
    second["activities"] = second["activities"][:1]
    original = SemanticDraft.model_validate(first)
    safe = _proposal_from_live_draft(source, original, allow_partial=True)
    merged = merge_preserved_activities(source, original, safe, SemanticDraft.model_validate(second))
    result = _proposal_from_live_draft(source, merged)
    assert [(m.role.value, m.span_start) for m in result.mentions] == [
        ("OPTIONAL", source.rindex("星河公园")), ("REFERENCE", source.index("星河公园"))]


def test_forward_alignment_cannot_make_valid_heading_time_unbound():
    source = "北京。\n## Day1｜09:00参观星河公园\n上午去星河公园。"
    first, second = replies(name="星河公园")
    first["activities"] = first["activities"][:1]
    first["activities"][0].update(city_evidence="北京", start_time="09:00", timing_source="TEXT",
                                  time_evidence="09:00参观星河公园")
    second["activities"] = second["activities"][:1]
    original = SemanticDraft.model_validate(first)
    safe = _proposal_from_live_draft(source, original)
    assert safe.mentions[0].start_time == "09:00"
    merged = merge_preserved_activities(source, original, safe, SemanticDraft.model_validate(second))
    protected = next(item for item in merged.activities if _identity(source, item) == _identity(source, original.activities[0]))
    assert (protected.role.value, protected.start_time, protected.time_evidence) == (
        "PLANNED", "09:00", "09:00参观星河公园")


def test_forward_alignment_does_not_reuse_valid_city_for_a_different_city_visit():
    source = "## Day1 北京｜星河公园\n抵达上海后去星河公园。"
    first, second = replies(name="星河公园")
    first["activities"] = first["activities"][:1]
    first["activities"][0].update(city_evidence="Day1 北京")
    second["activities"] = second["activities"][:1]
    second["activities"][0].update(city="上海", city_evidence="抵达上海后去星河公园")
    original = SemanticDraft.model_validate(first)
    safe = _proposal_from_live_draft(source, original)
    assert safe.mentions[0].city_hint == "北京"
    merged = merge_preserved_activities(source, original, safe, SemanticDraft.model_validate(second))
    protected = next(item for item in merged.activities if _identity(source, item) == _identity(source, original.activities[0]))
    assert (protected.role.value, protected.city, protected.city_evidence) == ("PLANNED", "北京", "Day1 北京")


@pytest.mark.asyncio
@pytest.mark.parametrize("heading,body", [
    ("## Day1 上午游览星河公园", "晚上再去星河公园。"),
    ("## Day1 上午游览星河公园", "晚上游览星河公园。"),
    ("## Day1｜上午游览星河公园", "晚上游览星河公园。"),
    ("Day1｜上午：星河公园", "晚上游览星河公园。"),
    ("Day1｜星河公园（上午）", "晚上游览星河公园。"),
])
@pytest.mark.parametrize("reverse", [False, True])
async def test_markdown_or_separator_cannot_turn_a_real_first_visit_into_a_summary(heading, body, reverse):
    # These are controlled two-answer fragments, not saved live model replies.
    # A heading may contain the actual morning visit. The independently valid
    # evening fragment must add that visit instead of replacing the morning.
    source = f"北京。\n{heading}\n{body}"
    def fragment(occurrence):
        return SemanticDraft.model_validate({"destination": "北京", "activities": [{
            "source_quote": "星河公园", "place_name": "星河公园", "occurrence": occurrence,
            "role": "PLANNED", "day_index": 1, "category": "景点", "city": "北京", "city_evidence": "北京",
        }]})
    original, repaired = fragment(2 if reverse else 1), fragment(1 if reverse else 2)
    safe = _proposal_from_live_draft(source, original)
    merged = merge_preserved_activities(source, original, safe, repaired)
    proposal = _proposal_from_live_draft(source, merged)
    visits = [item for item in proposal.mentions if item.role.value == "PLANNED"]
    assert [item.span_start for item in visits] == [source.index("星河公园"), source.rindex("星河公园")]
    assert not any(item.role.value == "REFERENCE" for item in proposal.mentions)
    output = await TripUnderstandingPipeline(None, FixedCities()).run(source, prepared_plan=proposal)
    assert [card.name for card in output.public_result.days[0].activities] == ["星河公园", "星河公园"]
    spans = {item.compiled.public_activity_token: item.compiled.mention.span_start for item in output.activities}
    assert [spans[card.activity_token] for card in output.public_result.days[0].activities] == [
        source.index("星河公园"), source.rindex("星河公园")]
    assert output.public_result.coverage.confirmed_place_count == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_literal_route_summary_and_numbered_body_still_reconcile(reverse):
    source = "北京。\n## Day1｜中轴线：天安门 — 故宫 — 景山 — 什刹海（皇城核心）\n4. 傍晚去什刹海。\n5. 去景山公园。"
    first, second = replies(reverse=reverse)
    original = SemanticDraft.model_validate(first)
    safe = _proposal_from_live_draft(source, original, allow_partial=True)
    merged = merge_preserved_activities(source, original, safe, SemanticDraft.model_validate(second))
    proposal = _proposal_from_live_draft(source, merged)
    visits = [item for item in proposal.mentions if item.atomic_place_name == "什刹海" and item.role.value == "PLANNED"]
    assert len(visits) == 1 and visits[0].span_start == source.rindex("什刹海")
    assert any(item.atomic_place_name == "什刹海" and item.role.value == "REFERENCE"
               and item.span_start == source.index("什刹海") for item in proposal.mentions)


@pytest.mark.asyncio
@pytest.mark.parametrize("wording", ["晚上再去", "晚上返回"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_explicit_body_return_keeps_both_occurrences_and_unresolved_order(wording, reverse):
    source = f"北京。\n## Day1｜星河公园\n{wording}星河公园。"
    first, second = replies(reverse=reverse, name="星河公园")
    first["activities"] = first["activities"][:1]
    first["activities"][0]["city_evidence"] = "北京"
    second["activities"] = second["activities"][:1]
    original = SemanticDraft.model_validate(first)
    safe = _proposal_from_live_draft(source, original)
    merged = merge_preserved_activities(source, original, safe, SemanticDraft.model_validate(second))
    proposal = _proposal_from_live_draft(source, merged)
    assert sorted((item.role.value, item.span_start) for item in proposal.mentions) == [
        ("PLANNED", source.index("星河公园")), ("PLANNED", source.rindex("星河公园"))]
    output = await TripUnderstandingPipeline(None, FixedCities()).run(source, prepared_plan=proposal)
    assert [card.name for card in output.public_result.days[0].activities] == ["星河公园", "星河公园"]
    assert output.public_result.coverage.confirmed_place_count == 2
    assert output.public_result.coverage.unprocessed_count > 0
    assert output.public_result.coverage.complete is False
