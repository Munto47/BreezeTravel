from __future__ import annotations

import pytest

from app.trip_understanding.full_text import DeterministicTextInferenceProvider
from app.trip_understanding.qwen_provider import (
    QwenSemanticDraft,
    QwenStructuredInferenceProvider,
)


def _span(source: str, name: str) -> tuple[int, int]:
    start = source.index(name)
    return start, start + len(name)


def test_qwen_day_titles_and_source_order_override_model_array_order() -> None:
    source = (
        "第1天 去故宫博物院；"
        "第 2 天 去景山公园；"
        "第三天 去天坛公园；"
        "Day 4 去北海公园；"
        "D5 去恭王府"
    )
    names = ["故宫博物院", "景山公园", "天坛公园", "北海公园", "恭王府"]
    mentions = []
    for name in reversed(names):
        start, end = _span(source, name)
        mentions.append(
            {
                "span_start": start,
                "span_end": end,
                "role": "PLANNED",
                "atomic_place_name": name,
            }
        )
    draft = QwenSemanticDraft.model_validate(
        {
            "destination": {
                "basis": "SOFT_ASSUMPTION",
                "name": "北京",
                "evidence_span_start": None,
                "evidence_span_end": None,
            },
            "mentions": mentions,
        }
    )

    normalized, _destination, _counts = (
        QwenStructuredInferenceProvider._proposal_from_draft(source, draft)
    )

    assert [item.atomic_place_name for item in normalized] == names
    assert [item.day_index for item in normalized] == [1, 2, 3, 4, 5]
    assert [item.sequence_index for item in normalized] == [0, 0, 0, 0, 0]


@pytest.mark.asyncio
async def test_local_fallback_uses_nearest_supported_day_title() -> None:
    source = (
        "第1天 去故宫博物院；"
        "第 2 天 去景山公园；"
        "第三天 去天坛公园；"
        "Day 4 去北海公园；"
        "D5 去恭王府"
    )

    proposal = await DeterministicTextInferenceProvider().propose(source)
    planned = [item for item in proposal.mentions if item.role.value == "PLANNED"]

    assert [item.atomic_place_name for item in planned] == [
        "故宫博物院",
        "景山公园",
        "天坛公园",
        "北海公园",
        "恭王府",
    ]
    assert [item.day_index for item in planned] == [1, 2, 3, 4, 5]
    assert [item.sequence_index for item in planned] == [0, 0, 0, 0, 0]


@pytest.mark.asyncio
async def test_only_true_planned_mentions_receive_day_one_without_a_title() -> None:
    source = "去故宫博物院；听说天坛公园很有名"

    proposal = await DeterministicTextInferenceProvider().propose(source)
    by_name = {item.atomic_place_name: item for item in proposal.mentions}

    assert by_name["故宫博物院"].role.value == "PLANNED"
    assert by_name["故宫博物院"].day_index == 1
    assert by_name["天坛公园"].role.value == "REFERENCE"
    assert by_name["天坛公园"].day_index is None


def test_qwen_applies_five_role_priority_per_real_occurrence() -> None:
    source = (
        "第1天 前往北京鼓楼。"
        "不要把‘错误地点’加入行程。"
        "如果当天太累，河坊街可以完全不去；"
        "只是路过杭州东站换乘；"
        "网友曾提到鼓楼，但这不是本次安排；"
        "已经决定排除北京环球影城。"
    )

    def mention(name: str, role: str) -> dict[str, object]:
        start = source.index(name)
        return {
            "span_start": start,
            "span_end": start + len(name),
            "role": role,
            "atomic_place_name": name,
        }

    draft = QwenSemanticDraft.model_validate(
        {
            "destination": {
                "basis": "SOFT_ASSUMPTION",
                "name": "北京、杭州",
                "evidence_span_start": None,
                "evidence_span_end": None,
            },
            "mentions": [
                mention("北京鼓楼", "PLANNED"),
                mention("错误地点", "PLANNED"),
                mention("河坊街", "EXCLUDED"),
                mention("杭州东站", "PLANNED"),
                mention("北京环球影城", "REFERENCE"),
            ],
        }
    )

    normalized, _destination, counts = (
        QwenStructuredInferenceProvider._proposal_from_draft(source, draft)
    )

    assert [(item.atomic_place_name, item.role.value) for item in normalized] == [
        ("北京鼓楼", "PLANNED"),
        ("河坊街", "OPTIONAL"),
        ("杭州东站", "PASS_THROUGH"),
        ("鼓楼", "REFERENCE"),
        ("北京环球影城", "EXCLUDED"),
    ]
    reference = next(item for item in normalized if item.atomic_place_name == "鼓楼")
    assert reference.span_start == source.index("鼓楼", source.index("网友曾提到"))
    assert all(item.atomic_place_name != "错误地点" for item in normalized)
    assert counts["explicit_role_recovery_count"] == 1


@pytest.mark.asyncio
async def test_local_fallback_keeps_five_roles_and_skips_meta_examples() -> None:
    source = (
        "第1天 前往北京鼓楼。"
        "不要把‘错误地点’加入行程。"
        "如果当天太累，河坊街可以完全不去；"
        "只是路过杭州东站换乘；"
        "网友曾提到鼓楼，但这不是本次安排；"
        "已经决定排除北京环球影城。"
    )

    proposal = await DeterministicTextInferenceProvider().propose(source)

    assert [(item.atomic_place_name, item.role.value) for item in proposal.mentions] == [
        ("北京鼓楼", "PLANNED"),
        ("河坊街", "OPTIONAL"),
        ("杭州东站", "PASS_THROUGH"),
        ("鼓楼", "REFERENCE"),
        ("北京环球影城", "EXCLUDED"),
    ]
    assert proposal.mentions[0].day_index == 1
    assert all(item.day_index is None for item in proposal.mentions[1:])
