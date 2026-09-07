"""Independent end-to-end source recovery regressions, with no live services."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)
from app.trip_understanding.models import ActivityRole
from app.trip_understanding.pipeline import TripUnderstandingPipeline


def activity(name, *, quote=None, role="PLANNED", occurrence=1, **values):
    return {"place_name": name, "source_quote": quote or name, "role": role,
            "occurrence": occurrence, "day_index": 1, **values}


async def run_trip(source, rows):
    draft = SemanticDraft.model_validate({"destination": "北京", "activities": rows})

    class DraftProvider:
        async def propose(self, text):
            return proposal_from_draft(text, draft)

    class RecordingResolver:
        def __init__(self):
            self.calls = []

        async def resolve(self, **values):
            self.calls.append(values)
            return None

    resolver = RecordingResolver()
    output = await TripUnderstandingPipeline(DraftProvider(), resolver).run(source)
    return output, resolver.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("source,quote", [
    ("Day1：14:00 入住星河酒店。", "入住"),
    ("Day1：14:00 在星河酒店办理入住。", "办理入住"),
])
async def test_short_checkin_quote_cannot_silently_erase_an_explicit_hotel(source, quote):
    try:
        output, calls = await run_trip(source, [activity("酒店", quote=quote, category="住宿")])
    except SourceAnchorValidationError as exc:
        # This incomplete draft can request semantic repair. It cannot be
        # accepted as a genuinely unnamed check-in while losing the hotel.
        assert exc.issues
        return
    assert [item.atomic_place_name for item in output.proposal.mentions] == ["星河酒店"]
    assert [item.name for item in output.public_result.days[0].activities] == ["星河酒店"]
    assert [call["atomic_place_name"] for call in calls] == ["星河酒店"]


@pytest.mark.asyncio
async def test_restriction_on_the_following_line_cannot_add_a_second_planned_stop():
    source = "Day1：**星河公园、云岭公园**。\n两个只选星河公园。"
    try:
        output, calls = await run_trip(source, [activity("星河公园")])
    except SourceAnchorValidationError as exc:
        assert exc.issues
        return
    assert [item.atomic_place_name for item in output.proposal.mentions
            if item.role == ActivityRole.PLANNED] == ["星河公园"]
    assert [item.name for item in output.public_result.days[0].activities] == ["星河公园"]
    assert [call["atomic_place_name"] for call in calls] == ["星河公园"]


@pytest.mark.asyncio
async def test_binary_title_does_not_reanchor_and_deduplicate_a_later_optional_visit():
    source = "Day1：二选一：云岭坡人少，星河湾更出名\n晚上20:00还可以去云岭坡拍星空。"
    output, calls = await run_trip(source, [
        activity("云岭坡", role="OPTIONAL"),
        activity("星河湾", role="OPTIONAL"),
        activity("云岭坡", role="OPTIONAL", occurrence=2, start_time="20:00", timing_source="TEXT",
                 time_evidence="晚上20:00还可以去云岭坡拍星空"),
    ])
    mentions = output.proposal.mentions
    assert len(mentions) == len(output.activities) == 3
    assert [item.atomic_place_name for item in mentions] == ["云岭坡", "星河湾", "云岭坡"]
    assert [item.role for item in mentions] == [ActivityRole.OPTIONAL] * 3
    assert [item.start_time for item in mentions] == [None, None, "20:00"]
    assert [item.span_start for item in mentions] == [source.index("云岭坡"), source.index("星河湾"), source.rindex("云岭坡")]
    assert all(source[item.span_start:item.span_end] == item.raw_text == item.atomic_place_name for item in mentions)
    assert output.proposal.unprocessed_count == 0
    assert output.public_result.days[0].activities == []
    assert calls == []


@pytest.mark.asyncio
async def test_a_genuinely_unnamed_checkin_remains_visible_without_a_place_search():
    source = "Day1：14:00 入住放行李。"
    output, calls = await run_trip(source, [activity("酒店", quote="入住放行李", category="住宿")])
    assert len(output.proposal.mentions) == 1
    mention = output.proposal.mentions[0]
    assert mention.atomic_place_name is None and mention.raw_text == "入住放行李"
    assert mention.role == ActivityRole.PLANNED and mention.category_hint == "住宿"
    cards = output.public_result.days[0].activities
    assert len(cards) == 1 and cards[0].category == "住宿" and cards[0].status == "NEEDS_CONFIRMATION"
    assert calls == []


@pytest.mark.asyncio
async def test_a_correct_literal_hotel_quote_keeps_its_named_card():
    source = "Day1：14:00 入住星河酒店。"
    output, calls = await run_trip(source, [activity("星河酒店", category="住宿")])
    assert [item.atomic_place_name for item in output.proposal.mentions] == ["星河酒店"]
    assert [item.name for item in output.public_result.days[0].activities] == ["星河酒店"]
    assert [call["atomic_place_name"] for call in calls] == ["星河酒店"]


@pytest.mark.asyncio
async def test_an_unrestricted_bold_list_still_recovers_its_second_planned_stop():
    source = "Day1：**星河公园、云岭公园**。"
    output, calls = await run_trip(source, [activity("星河公园")])
    assert [item.name for item in output.public_result.days[0].activities] == ["星河公园", "云岭公园"]
    assert [call["atomic_place_name"] for call in calls] == ["星河公园", "云岭公园"]


@pytest.mark.asyncio
async def test_a_later_explicit_planned_visit_keeps_its_role_time_and_second_occurrence():
    source = "Day1：二选一：云岭坡人少，星河湾更出名\n晚上20:00再去云岭坡拍星空。"
    output, calls = await run_trip(source, [
        activity("云岭坡", role="OPTIONAL"),
        activity("星河湾", role="OPTIONAL"),
        activity("云岭坡", occurrence=2, start_time="20:00", timing_source="TEXT",
                 time_evidence="晚上20:00再去云岭坡拍星空"),
    ])
    assert len(output.proposal.mentions) == 3
    night = output.proposal.mentions[-1]
    assert night.role == ActivityRole.PLANNED and night.start_time == "20:00"
    assert night.span_start == source.rindex("云岭坡")
    assert [item.name for item in output.public_result.days[0].activities] == ["云岭坡"]
    assert [call["atomic_place_name"] for call in calls] == ["云岭坡"]
