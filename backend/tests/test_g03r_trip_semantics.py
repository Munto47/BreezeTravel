from __future__ import annotations

import hashlib

import pytest

from app.trip_understanding.full_text import DeterministicTextInferenceProvider
from app.trip_understanding.models import InferenceProposal, ProposedMention
from app.trip_understanding.pipeline import TripUnderstandingPipeline
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


def test_qwen_day_title_does_not_upgrade_a_descriptive_reference_to_planned() -> None:
    source = "北京。Day 1 故宫是世界文化遗产。"
    start, end = _span(source, "故宫")
    draft = QwenSemanticDraft.model_validate(
        {
            "destination": {
                "basis": "EXPLICIT",
                "evidence_span_start": 0,
                "evidence_span_end": 2,
            },
            "mentions": [
                {
                    "span_start": start,
                    "span_end": end,
                    "role": "REFERENCE",
                    "atomic_place_name": "故宫",
                }
            ],
        }
    )

    normalized, _destination, counts = (
        QwenStructuredInferenceProvider._proposal_from_draft(source, draft)
    )

    assert [(item.atomic_place_name, item.role.value, item.day_index) for item in normalized] == [
        ("故宫", "REFERENCE", None)
    ]
    assert counts["local_role_reclassification_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    (
        "北京。Day 1 故宫是世界文化遗产。",
        "北京。Day 1 去年故宫游客很多。",
    ),
)
async def test_local_fallback_keeps_day_descriptions_out_of_planned_cards(
    source: str,
) -> None:
    proposal = await DeterministicTextInferenceProvider().propose(source)

    assert [item for item in proposal.mentions if item.role.value == "PLANNED"] == []
    assert all(item.atomic_place_name != "故宫是世界文化遗产" for item in proposal.mentions)
    assert all(item.atomic_place_name != "年故宫游客很多" for item in proposal.mentions)


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


@pytest.mark.asyncio
async def test_local_fallback_atomizes_unknown_planned_places_without_noise() -> None:
    source = (
        "第1天上午先到银杏秘境一号，午后步行到星河展馆二号。"
        "第 2 天安排云端花园三号和湖畔书屋四号。"
        "第三天先古巷茶室五号后山顶平台六号。"
        "网友推荐候鸟书店，但这不是本次安排；"
        "预约说明写着‘提前确认’，电话010-12345678，"
        "导航文字沿路线步行十分钟。"
        "详情https://example.invalid/plan?place=银杏秘境一号"
    )

    proposal = await DeterministicTextInferenceProvider().propose(source)
    planned = [item for item in proposal.mentions if item.role.value == "PLANNED"]

    assert [item.atomic_place_name for item in planned] == [
        "银杏秘境一号",
        "星河展馆二号",
        "云端花园三号",
        "湖畔书屋四号",
        "古巷茶室五号",
        "山顶平台六号",
    ]
    assert [(item.day_index, item.sequence_index) for item in planned] == [
        (1, 0),
        (1, 1),
        (2, 0),
        (2, 1),
        (3, 0),
        (3, 1),
    ]
    references = [item for item in proposal.mentions if item.role.value == "REFERENCE"]
    assert [(item.atomic_place_name, item.raw_text) for item in references] == [
        ("候鸟书店", "候鸟书店")
    ]
    assert all("http" not in item.raw_text for item in proposal.mentions)
    assert all("预约" not in (item.atomic_place_name or "") for item in proposal.mentions)
    assert all("010-" not in (item.atomic_place_name or "") for item in proposal.mentions)


@pytest.mark.asyncio
async def test_local_fallback_keeps_overlapping_real_occurrence_outside_url() -> None:
    source = (
        "第1天前往北京鼓楼。"
        "不要因为‘鼓楼很有名’就自动加入；"
        "网友曾提到鼓楼，但这不是本次安排；"
        "详情https://example.invalid/guide?place=北京鼓楼"
    )

    proposal = await DeterministicTextInferenceProvider().propose(source)
    atomic = [item for item in proposal.mentions if item.atomic_place_name]

    assert [(item.atomic_place_name, item.role.value) for item in atomic] == [
        ("北京鼓楼", "PLANNED"),
        ("鼓楼", "REFERENCE"),
    ]
    assert atomic[1].span_start == source.index("鼓楼", source.index("网友曾提到"))


@pytest.mark.asyncio
async def test_local_fallback_consumes_leading_action_before_atomic_places() -> None:
    source = (
        "上海三日游。Day 1 逛外滩和南京路步行街。"
        "D2 经过人民广场换乘，前往上海博物馆。"
        "Day 3 如果有时间可以去豫园。"
    )

    proposal = await DeterministicTextInferenceProvider().propose(source)
    by_name = {
        item.atomic_place_name: item
        for item in proposal.mentions
        if item.atomic_place_name
    }

    assert [
        (item.atomic_place_name, item.day_index, item.sequence_index)
        for item in proposal.mentions
        if item.role.value == "PLANNED"
    ] == [
        ("外滩", 1, 0),
        ("南京路步行街", 1, 1),
        ("上海博物馆", 2, 0),
    ]
    assert by_name["豫园"].role.value == "OPTIONAL"
    assert "逛外滩" not in by_name


@pytest.mark.asyncio
async def test_local_fallback_starts_a_new_capture_at_each_action_anchor() -> None:
    source = "D2先去上海博物馆再去外滩。"

    proposal = await DeterministicTextInferenceProvider().propose(source)

    assert [
        (item.atomic_place_name, item.role.value, item.day_index, item.sequence_index)
        for item in proposal.mentions
    ] == [
        ("上海博物馆", "PLANNED", 2, 0),
        ("外滩", "PLANNED", 2, 1),
    ]


@pytest.mark.asyncio
async def test_local_fallback_preserves_controlled_other_city_metadata() -> None:
    unique_city = await DeterministicTextInferenceProvider().propose(
        "Day 1 去北京路步行街。"
    )
    unknown_city = await DeterministicTextInferenceProvider().propose(
        "Day 1 去星河秘境一号。"
    )
    multiple_cities = await DeterministicTextInferenceProvider().propose(
        "Day 1 去北京路步行街。Day 2 去南普陀寺。"
    )

    assert unique_city.destination_name == "广州"
    assert [item.atomic_place_name for item in unique_city.mentions] == [
        "北京路步行街"
    ]
    assert unknown_city.destination_name == "目的地待确认"
    assert multiple_cities.destination_name == "目的地待确认"
    assert [item.atomic_place_name for item in multiple_cities.mentions] == [
        "北京路步行街",
        "南普陀寺",
    ]


@pytest.mark.asyncio
async def test_resolver_and_public_cards_share_the_same_atomic_planned_gate() -> None:
    source = "未知地点甲；只写描述；010-12345678；预约说明；前往地点丙；推荐地点乙"
    values = [
        ("未知地点甲", "未知地点甲", "PLANNED", 1),
        ("只写描述", None, "PLANNED", 1),
        ("010-12345678", "010-12345678", "PLANNED", 1),
        ("预约说明", "预约说明", "PLANNED", 1),
        ("前往地点丙", "地点丙", "PLANNED", 1),
        ("推荐地点乙", "推荐地点乙", "REFERENCE", None),
    ]

    class Provider:
        async def propose(self, source_text: str) -> InferenceProposal:
            mentions = []
            for index, (raw, atomic, role, day) in enumerate(values):
                start = source_text.index(raw)
                mentions.append(
                    ProposedMention(
                        mention_id=f"gate-{index}",
                        raw_text=raw,
                        span_start=start,
                        span_end=start + len(raw),
                        role=role,
                        day_index=day,
                        sequence_index=index,
                        atomic_place_name=atomic,
                    )
                )
            return InferenceProposal(
                source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                destination_name="目的地待确认",
                destination_basis="SOFT_ASSUMPTION",
                mentions=mentions,
                binding={"provider": "test-double", "external_calls": 0},
            )

    class Resolver:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def resolve(
            self,
            *,
            city: str,
            atomic_place_name: str,
            category_hint: str | None = None,
        ):
            del city, category_hint
            self.calls.append(atomic_place_name)
            return None

    resolver = Resolver()
    output = await TripUnderstandingPipeline(Provider(), resolver).run(source)
    eligible = [
        item.compiled.mention.atomic_place_name
        for item in output.activities
        if item.compiled.eligible_for_place_search
    ]
    cards = [card.name for day in output.public_result.days for card in day.activities]

    assert resolver.calls == eligible == cards == ["未知地点甲"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    (
        "北京 Day 1：上午去吃午饭。",
        "北京 Day 1：下午安排自由活动。",
        "北京 Day 1：晚上去酒店休息。",
        "北京 Day 1：上午去看看风景。",
    ),
)
async def test_generic_activity_prose_never_becomes_an_executable_place(
    source: str,
) -> None:
    class Resolver:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def resolve(
            self,
            *,
            city: str,
            atomic_place_name: str,
            category_hint: str | None = None,
        ):
            del city, category_hint
            self.calls.append(atomic_place_name)
            return None

    resolver = Resolver()
    output = await TripUnderstandingPipeline(
        DeterministicTextInferenceProvider(),
        resolver,
    ).run(source)

    assert resolver.calls == []
    assert output.compiler_receipt["eligible_place_count"] == 0
    assert output.resolution_receipt["attempted_count"] == 0
    assert [
        card
        for day in output.public_result.days
        for card in day.activities
    ] == []
