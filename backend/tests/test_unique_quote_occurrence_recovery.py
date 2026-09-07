"""Synthetic occurrence-index recovery; not evidence about a live guide failure."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    _recover_unique_quote_occurrences,
    proposal_from_draft,
)
from app.trip_understanding.models import ActivityRole
from app.trip_understanding.pipeline import TripUnderstandingPipeline


def activity(name="星河公园", *, quote=None, occurrence=2, day=1, role="PLANNED", **values):
    return {"source_quote": quote or name, "place_name": name, "occurrence": occurrence,
            "day_index": day, "role": role, "category": "景点", **values}


def draft_for(rows):
    return SemanticDraft.model_validate({"destination": "北京", "activities": rows})


def test_a_unique_literal_quote_changes_only_the_out_of_range_occurrence():
    source = "09:00 已预约星河博物馆，停留60分钟。"
    draft = draft_for([activity("星河博物馆", occurrence=3, start_time="09:00",
        visit_duration_minutes=60, timing_source="TEXT", locked=True, fixed_commitment=True,
        time_evidence=source)])
    repaired = _recover_unique_quote_occurrences(source, draft)
    expected = draft.model_dump()
    expected["activities"][0]["occurrence"] = 1
    assert repaired.model_dump() == expected
    assert draft.activities[0].occurrence == 3
    proposal = proposal_from_draft(source, draft)
    mention = proposal.mentions[0]
    assert mention.atomic_place_name == mention.raw_text == "星河博物馆"
    assert mention.day_index == 1 and mention.role == ActivityRole.PLANNED
    assert (mention.start_time, mention.visit_duration_minutes, mention.locked, mention.fixed_commitment) == (
        "09:00", 60, True, True,
    )
    assert source[mention.span_start:mention.span_end] == mention.raw_text


@pytest.mark.parametrize("source,day", [
    ("Day1\n星河公园。", 1),
    ("Day1\n自由活动。\nDay2\n星河公园。", 2),
    ("星河公园。", 1),
])
def test_unique_quote_recovers_only_in_its_matching_day(source, day):
    proposal = proposal_from_draft(source, draft_for([activity(day=day)]))
    assert len(proposal.mentions) == 1
    mention = proposal.mentions[0]
    assert (mention.atomic_place_name, mention.role, mention.day_index) == ("星河公园", ActivityRole.PLANNED, day)
    assert mention.span_start == source.index("星河公园")
    assert source[mention.span_start:mention.span_end] == "星河公园"


@pytest.mark.asyncio
async def test_recovered_optional_quote_is_visible_and_makes_no_place_query():
    source = "Day1\n备选：星河公园。"
    draft = draft_for([activity(role="OPTIONAL")])

    class DraftProvider:
        async def propose(self, text):
            return proposal_from_draft(text, draft)

    class NoSearch:
        async def resolve(self, **values):
            raise AssertionError("An optional place cannot trigger automatic search")

    output = await TripUnderstandingPipeline(DraftProvider(), NoSearch()).run(source)
    assert len(output.proposal.mentions) == 1
    mention = output.proposal.mentions[0]
    assert mention.role == ActivityRole.OPTIONAL and mention.day_index == 1
    assert mention.atomic_place_name == "星河公园" and mention.span_start == source.index("星河公园")
    assert not output.activities[0].compiled.eligible_for_place_search
    assert output.public_result.days[0].activities == []
    assert [item.name for item in output.public_result.days[0].alternatives] == ["星河公园"]


@pytest.mark.parametrize("source,rows", [
    ("Day1：星河公园。", [activity()]),
    ("Day1\n星河公园，晚上再去星河公园。", [activity(occurrence=3)]),
    ("Day1\n星河公园。\nDay2\n自由活动。", [activity(day=2)]),
    ("星河公园。", [activity(day=2)]),
    ("Day1\n星河公园。", [activity(), activity(occurrence=1, role="REFERENCE")]),
    ("Day1\n今天去星河公园。", [activity(), {"source_quote": "今天去星河公园", "place_name": None,
        "occurrence": 1, "day_index": 1, "role": "REFERENCE"}]),
    ("Day1\n星河公园。", [activity("云岭公园")]),
    ("Day1\n星河公园游览。", [activity("星河公园游览")]),
    ("Day1\n先去星河公园。", [activity(quote="先去星河公园")]),
])
def test_ambiguous_wrong_day_claimed_or_nonliteral_quotes_cannot_recover(source, rows):
    draft = draft_for(rows)
    assert _recover_unique_quote_occurrences(source, draft).model_dump() == draft.model_dump()
    with pytest.raises(SourceAnchorValidationError):
        proposal_from_draft(source, draft)


@pytest.mark.parametrize("role", ["REFERENCE", "EXCLUDED", "PASS_THROUGH"])
def test_non_visit_roles_do_not_use_occurrence_recovery(role):
    source = "Day1\n星河公园。"
    draft = draft_for([activity(role=role)])
    assert _recover_unique_quote_occurrences(source, draft).model_dump() == draft.model_dump()
    with pytest.raises(SourceAnchorValidationError):
        proposal_from_draft(source, draft)


@pytest.mark.parametrize("narrative", [
    "更正：星河公园待定。",
    "取消星河公园。",
    "将星河公园移到次日。",
    "如果下雨再去星河公园。",
    "> 如果下雨去星河公园。",
    "引用：星河公园。",
])
def test_changed_cancelled_moved_or_conditional_quotes_still_require_repair(narrative):
    source = "Day1\n" + narrative
    draft = draft_for([activity()])
    assert _recover_unique_quote_occurrences(source, draft).model_dump() == draft.model_dump()
    with pytest.raises(SourceAnchorValidationError):
        proposal_from_draft(source, draft)
