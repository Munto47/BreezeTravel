"""Explicit cancellation conflicts request semantic repair, never a local reorder."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.trip_understanding.experience_inference import (
    ExperienceQwenProvider,
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)


REVISION_SOURCE = (
    "初稿：第一天云岭寺、望星寺；第二天青溪湖、星河塔。"
    "最终修改为：望星寺取消，星河塔移到第一天云岭寺之后，第二天只留下青溪湖。旅行还是两天。"
)


def activity(name, day=1, occurrence=1):
    return {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": day, "occurrence": occurrence}


def draft(rows):
    return SemanticDraft.model_validate({"activities": rows})


def original_pair():
    return [activity("云岭寺"), activity("望星寺")]


@pytest.mark.parametrize("cancellation", ["望星寺取消。", "取消望星寺。", "不得不取消望星寺。"])
def test_affirmed_cancellation_cannot_leave_the_original_place_planned(cancellation):
    source = "Day1：云岭寺、望星寺。最终安排：" + cancellation
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(source, draft(original_pair()))
    assert error.value.issues


def test_obsolete_revision_with_a_cancelled_place_and_missing_moved_place_requires_repair():
    stale = [activity("云岭寺"), activity("望星寺"), activity("青溪湖", 2)]
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(REVISION_SOURCE, draft(stale))
    assert error.value.issues


def test_a_model_supplied_final_order_remains_valid_without_a_cancelled_place_record():
    corrected = [activity("云岭寺"), activity("星河塔", occurrence=2), activity("青溪湖", 2, occurrence=2)]
    proposal = proposal_from_draft(REVISION_SOURCE, draft(corrected))
    assert [(mention.atomic_place_name, mention.day_index) for mention in proposal.mentions] == [
        ("云岭寺", 1), ("星河塔", 1), ("青溪湖", 2),
    ]
    assert proposal.day_count == 2
    assert all(mention.role.value == "PLANNED" for mention in proposal.mentions)


@pytest.mark.parametrize("later_text", [
    "曾取消望星寺，最终恢复望星寺，仍按第一天执行。",
    "望星寺不取消。",
    "不必取消望星寺。",
    "没有取消望星寺。",
    "不是取消望星寺，只是晚一点去。",
    "如果下雨就取消望星寺，目前按原计划去。",
    "可能取消望星寺，目前尚未决定。",
    "引用旧攻略：‘取消望星寺’，本次仍去。",
    "> 引用：取消望星寺。\n本次安排照常。",
])
def test_restoration_negation_condition_or_quotation_does_not_override_a_valid_plan(later_text):
    source = "Day1：云岭寺、望星寺。\n" + later_text
    proposal = proposal_from_draft(source, draft(original_pair()))
    assert [(mention.atomic_place_name, mention.day_index) for mention in proposal.mentions] == [
        ("云岭寺", 1), ("望星寺", 1),
    ]
    assert all(mention.role.value == "PLANNED" for mention in proposal.mentions)


def test_cancelling_only_the_second_day_does_not_reject_the_first_days_same_place():
    source = "Day1：望星寺。\nDay2：望星寺。最终只取消第二天的望星寺，第一天照常。"
    proposal = proposal_from_draft(source, draft([activity("望星寺", 1)]))
    assert proposal.day_count == 2
    assert len(proposal.mentions) == 1
    assert proposal.mentions[0].atomic_place_name == "望星寺"
    assert proposal.mentions[0].day_index == 1
    assert proposal.mentions[0].span_start == source.index("望星寺")
    assert proposal.mentions[0].role.value == "PLANNED"


@pytest.mark.parametrize("quoted", [
    "> 最终安排：取消望星寺。\n这段是摘录，实际安排照常。",
    "‘最终安排：取消望星寺。’本次安排照常。",
])
def test_quoted_final_cancellation_is_not_the_current_plan(quoted):
    source = "Day1：望星寺。\n" + quoted
    proposal = proposal_from_draft(source, draft([activity("望星寺")]))
    assert [(item.atomic_place_name, item.role.value) for item in proposal.mentions] == [("望星寺", "PLANNED")]


@pytest.mark.asyncio
async def test_an_explicit_cancellation_conflict_uses_the_existing_same_request_repair():
    stale = [activity("云岭寺"), activity("望星寺"), activity("青溪湖", 2)]
    corrected = [activity("云岭寺"), activity("星河塔", occurrence=2), activity("青溪湖", 2, occurrence=2)]

    def response(rows):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"activities": rows})), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=40, completion_tokens=30), model="unit-test",
        )

    create = AsyncMock(side_effect=[response(stale), response(corrected)])
    provider = ExperienceQwenProvider(
        api_key="unit-only", base_url="https://example.invalid/v1", model="unit-test",
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    proposal = await provider.propose(REVISION_SOURCE)
    assert create.await_count == 2
    assert proposal.binding["repair_call_count"] == 1
    assert [(mention.atomic_place_name, mention.day_index) for mention in proposal.mentions] == [
        ("云岭寺", 1), ("星河塔", 1), ("青溪湖", 2),
    ]
    assert proposal.day_count == 2
