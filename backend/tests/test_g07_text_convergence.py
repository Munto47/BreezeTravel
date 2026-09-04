from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from copy import deepcopy

import pytest

from app.trip_understanding import pipeline as pipeline_module
from app.trip_understanding.full_text import build_full_text_pipeline
from app.trip_understanding.models import ActivityRole, DestinationBasis, ProposedMention
from app.trip_understanding.qwen_provider import (
    QwenExplicitDestinationDraft,
    QwenMentionDraft,
    QwenSemanticDraft,
    QwenStructuredInferenceProvider,
)
from evals.g07_text_convergence_v1.runner import (
    CASES_PATH,
    FROZEN_DATASET_SHA256,
    FROZEN_SCHEMA_SHA256,
    SCHEMA_PATH,
    _empty_observation,
    _run_pipeline_case,
    _score_case,
    load_cases,
    run_evaluation,
)


def _mention(name: str) -> ProposedMention:
    return ProposedMention(
        mention_id="mention-test",
        raw_text=name,
        span_start=0,
        span_end=len(name),
        role=ActivityRole.PLANNED,
        day_index=1,
        sequence_index=0,
        atomic_place_name=name,
    )


def test_public_dataset_is_frozen_strict_and_non_blind() -> None:
    payload = load_cases()
    cases = payload["cases"]

    assert payload["public_non_blind"] is True
    assert [case["case_id"] for case in cases] == [
        f"BT-COMPAT-{index:03d}" for index in range(1, 41)
    ]
    assert Counter(case["group"] for case in cases) == {
        "STRUCTURE_ROLE_DATE": 10,
        "TIME_CANCEL_RESCHEDULE": 10,
        "PLACE_CITY_CATEGORY": 8,
        "REVISION_MAP_PROVIDER": 6,
        "PRIVACY_PUBLIC_UX": 6,
    }
    cancellation_case = cases[19]
    assert cancellation_case["case_id"] == "BT-COMPAT-020"
    assert cancellation_case["input_text"] == (
        "杭州一日游。Day 1 去西湖；取消西湖风景名胜区。"
    )
    serialized_cases = json.dumps(cases, ensure_ascii=False).casefold()
    assert "frozen_blind" not in serialized_cases
    assert "agent_gate_v1" not in serialized_cases
    assert hashlib.sha256(CASES_PATH.read_bytes()).hexdigest() == FROZEN_DATASET_SHA256
    assert hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest() == FROZEN_SCHEMA_SHA256


def test_runner_reaches_text_r1_without_claiming_overall_delivery() -> None:
    result = run_evaluation()

    assert result["case_count"] == 40
    assert result["safe_pass_count"] == 40
    assert result["hard_safety_failure_count"] == 0
    assert result["evaluation_error_count"] == 0
    assert result["exact_pass_count"] >= 36
    assert result["text_compatibility_level"] in {"TEXT_COMPAT_R1", "TEXT_COMPAT_R2"}
    assert result["overall_delivery_level"] == "NOT_RUN"
    assert len(result["case_results"]) == 40
    assert all("input_text" not in item for item in result["case_results"])

    by_id = {item["case_id"]: item for item in result["case_results"]}
    assert by_id["BT-COMPAT-039"]["status"] == "SAFE_DEGRADE"
    assert by_id["BT-COMPAT-039"]["degradation_codes"] == [
        "UI_COLOR_E2E_NOT_RUN"
    ]
    assert by_id["BT-COMPAT-040"]["status"] == "SAFE_DEGRADE"
    assert by_id["BT-COMPAT-040"]["degradation_codes"] == [
        "HOME_AND_MOBILE_E2E_NOT_RUN"
    ]
    assert result["degradation_list"] == [
        {"case_id": "BT-COMPAT-005", "codes": ["SAFE_OMISSION"]},
        {"case_id": "BT-COMPAT-039", "codes": ["UI_COLOR_E2E_NOT_RUN"]},
        {
            "case_id": "BT-COMPAT-040",
            "codes": ["HOME_AND_MOBILE_E2E_NOT_RUN"],
        },
    ]


@pytest.mark.parametrize(
    "name",
    [
        "故宫博物院讲解",
        "景山公园拍照",
        "故宫博物院10",
        "2小时",
        "公厕",
        "停车场",
        "充电站",
        "故宫入口",
        "故宫出口",
    ],
)
def test_final_atomic_gate_rejects_action_time_and_facility_noise(name: str) -> None:
    assert pipeline_module.is_atomic_planned_place(_mention(name)) is False


def test_final_atomic_gate_keeps_unknown_but_atomic_place() -> None:
    assert pipeline_module.is_atomic_planned_place(_mention("未知地点甲")) is True


@pytest.mark.parametrize(
    ("source_text", "place", "expected"),
    [
        ("北京一日游。Day 1 上午去故宫博物院。", "故宫博物院", "上午"),
        ("北京一日游。Day 1 14:00去故宫博物院。", "故宫博物院", "14:00"),
        ("北京一日游。Day 1 下午2点去故宫博物院。", "故宫博物院", "14:00"),
        ("北京一日游。Day 1 晚上7:30去故宫博物院。", "故宫博物院", "19:30"),
        ("北京一日游。Day 1 去故宫博物院10:00到达。", "故宫博物院", "10:00"),
        ("北京；D1：14:00去故宫博物院。", "故宫博物院", "14:00"),
        ("北京一日游。Day 1 去故宫博物院，开放时间08:30-17:00。", "故宫博物院", None),
        ("北京一日游。Day 1 去故宫博物院，步行10分钟到景山公园。", "景山公园", None),
        ("北京一日游。Day 1 交通14:00到景山公园。", "景山公园", None),
        ("北京一日游。Day 1 车程14:00到景山公园。", "景山公园", None),
        ("北京一日游。Day 1 路线14:00到景山公园。", "景山公园", None),
    ],
)
def test_shared_time_hint_has_local_visit_ownership(
    source_text: str,
    place: str,
    expected: str | None,
) -> None:
    derive = getattr(pipeline_module, "derive_visit_time_hint", None)
    assert derive is not None
    start = source_text.index(place)
    assert derive(source_text, start, start + len(place)) == expected


def test_deterministic_pipeline_handles_repeat_restore_and_negated_cancel() -> None:
    source_text = (
        "北京一日游。Day 1 上午去故宫博物院，下午再次去故宫博物院；"
        "原计划去天坛公园，后来取消，最后明确恢复原方案去天坛公园；"
        "不得不取消颐和园；并不取消景山公园。"
    )
    output = asyncio.run(build_full_text_pipeline().run(source_text))
    cards = [card for day in output.public_result.days for card in day.activities]
    roles = [
        (item.compiled.mention.atomic_place_name, item.compiled.mention.role.value)
        for item in output.activities
    ]

    assert [(card.name, card.time_hint) for card in cards] == [
        ("故宫博物院", "上午"),
        ("故宫博物院", "下午"),
        ("天坛公园", None),
    ]
    assert ("颐和园", "EXCLUDED") in roles
    assert ("景山公园", "REFERENCE") in roles


@pytest.mark.parametrize(
    "phrase",
    [
        "并不取消",
        "没有取消",
        "不打算撤掉",
        "不得取消",
        "不能取消",
        "无法取消",
        "未取消",
        "取消不了",
        "不是取消",
        "并非取消",
        "不想取消",
        "不希望取消",
        "不需要取消",
        "没必要取消",
        "别再取消",
        "并不是要取消",
    ],
)
def test_negated_cancellation_never_removes_an_existing_plan(phrase: str) -> None:
    output = asyncio.run(
        build_full_text_pipeline().run(
            f"北京一日游。Day 1 去景山公园；{phrase}景山公园。"
        )
    )
    cards = [card.name for day in output.public_result.days for card in day.activities]
    roles = [
        item.compiled.mention.role
        for item in output.activities
        if item.compiled.mention.atomic_place_name == "景山公园"
    ]

    assert cards == ["景山公园"]
    assert ActivityRole.EXCLUDED not in roles


def test_later_true_cancellation_revokes_plan_and_final_restore_reactivates() -> None:
    cancelled = asyncio.run(
        build_full_text_pipeline().run(
            "北京一日游。Day 1 去景山公园；不得不取消景山公园。"
        )
    )
    cancelled_cards = [
        card.name for day in cancelled.public_result.days for card in day.activities
    ]
    cancelled_roles = [
        item.compiled.mention.role
        for item in cancelled.activities
        if item.compiled.mention.atomic_place_name == "景山公园"
    ]

    assert cancelled_cards == []
    assert cancelled_roles == [ActivityRole.REFERENCE, ActivityRole.EXCLUDED]

    restored = asyncio.run(
        build_full_text_pipeline().run(
            "北京一日游。Day 1 去景山公园；取消景山公园；"
            "最后明确恢复原方案去景山公园。"
        )
    )
    restored_cards = [
        card.name for day in restored.public_result.days for card in day.activities
    ]
    restored_roles = [
        item.compiled.mention.role
        for item in restored.activities
        if item.compiled.mention.atomic_place_name == "景山公园"
    ]

    assert restored_cards == ["景山公园"]
    assert restored_roles == [
        ActivityRole.REFERENCE,
        ActivityRole.EXCLUDED,
        ActivityRole.PLANNED,
    ]


def test_cancellation_target_never_removes_same_name_visit_on_another_day() -> None:
    output = asyncio.run(
        build_full_text_pipeline().run(
            "北京两日游。Day 1 去故宫博物院。"
            "Day 2 不去故宫博物院，改去景山公园。"
        )
    )
    cards = [
        (day.label, card.name, card.time_hint)
        for day in output.public_result.days
        for card in day.activities
    ]
    palace_roles = [
        item.compiled.mention.role
        for item in output.activities
        if item.compiled.mention.atomic_place_name == "故宫博物院"
    ]

    assert cards == [
        ("Day 1", "故宫博物院", None),
        ("Day 2", "景山公园", None),
    ]
    assert palace_roles == [ActivityRole.PLANNED, ActivityRole.EXCLUDED]


def test_explicit_time_cancellation_only_removes_matching_occurrence() -> None:
    output = asyncio.run(
        build_full_text_pipeline().run(
            "北京一日游。Day 1 上午去故宫博物院，下午再次去故宫博物院；"
            "取消下午的故宫博物院。"
        )
    )
    cards = [
        (card.name, card.time_hint)
        for day in output.public_result.days
        for card in day.activities
    ]
    palace_roles = [
        item.compiled.mention.role
        for item in output.activities
        if item.compiled.mention.atomic_place_name == "故宫博物院"
    ]

    assert cards == [("故宫博物院", "上午")]
    assert palace_roles == [
        ActivityRole.PLANNED,
        ActivityRole.REFERENCE,
        ActivityRole.EXCLUDED,
    ]


def test_ambiguous_same_day_cancellation_keeps_occurrences_pending() -> None:
    output = asyncio.run(
        build_full_text_pipeline().run(
            "北京一日游。Day 1 上午去故宫博物院，下午再次去故宫博物院；"
            "取消故宫博物院。"
        )
    )
    cards = [
        (card.name, card.time_hint, card.status)
        for day in output.public_result.days
        for card in day.activities
    ]
    palace_activities = [
        item
        for item in output.activities
        if item.compiled.mention.atomic_place_name == "故宫博物院"
    ]

    assert cards == [
        ("故宫博物院", "上午", "NEEDS_CONFIRMATION"),
        ("故宫博物院", "下午", "NEEDS_CONFIRMATION"),
    ]
    assert [item.compiled.mention.role for item in palace_activities] == [
        ActivityRole.PLANNED,
        ActivityRole.PLANNED,
        ActivityRole.EXCLUDED,
    ]
    assert all(
        item.compiled.eligible_for_place_search is False
        for item in palace_activities[:2]
    )
    assert output.resolution_receipt["attempted_count"] == 0
    assert output.resolution_receipt["unique_resolution_count"] == 0
    assert output.resolution_receipt["place_external_call_count"] == 0


def test_cancellation_ordinal_does_not_mix_contained_place_names() -> None:
    output = asyncio.run(
        build_full_text_pipeline().run(
            "杭州一日游。Day 1 上午去西湖，下午去西湖风景名胜区；"
            "取消第一次西湖风景名胜区。"
        )
    )
    cards = [
        (card.name, card.time_hint)
        for day in output.public_result.days
        for card in day.activities
    ]
    roles_by_name = [
        (item.compiled.mention.atomic_place_name, item.compiled.mention.role)
        for item in output.activities
    ]

    assert cards == [("西湖", "上午")]
    assert roles_by_name == [
        ("西湖", ActivityRole.PLANNED),
        ("西湖风景名胜区", ActivityRole.REFERENCE),
        ("第一次西湖风景名胜区", ActivityRole.EXCLUDED),
    ]


@pytest.mark.parametrize("verb", ["取消", "后来不去"])
def test_cancellation_long_name_never_removes_only_planned_short_name(
    verb: str,
) -> None:
    output = asyncio.run(
        build_full_text_pipeline().run(
            f"杭州一日游。Day 1 去西湖；{verb}西湖风景名胜区。"
        )
    )
    cards = [
        (day.label, card.name, card.status)
        for day in output.public_result.days
        for card in day.activities
    ]
    activities = [
        (
            item.compiled.mention.atomic_place_name,
            item.compiled.mention.role,
            item.compiled.eligible_for_place_search,
        )
        for item in output.activities
    ]

    assert cards == [("Day 1", "西湖", "NEEDS_CONFIRMATION")]
    assert activities == [
        ("西湖", ActivityRole.PLANNED, True),
        ("西湖风景名胜区", ActivityRole.EXCLUDED, False),
    ]
    assert (
        output.inference_binding.get("terminal_cancellation_reclassification_count", 0)
        == 0
    )


def test_conditional_cancellation_keeps_plan_pending_without_resolution() -> None:
    output = asyncio.run(
        build_full_text_pipeline().run(
            "北京一日游。Day 1 去故宫博物院；如果下雨就取消故宫博物院。"
        )
    )
    cards = [card for day in output.public_result.days for card in day.activities]

    assert [(card.name, card.status) for card in cards] == [
        ("故宫博物院", "NEEDS_CONFIRMATION")
    ]
    assert output.resolution_receipt["attempted_count"] == 0
    assert output.resolution_receipt["unique_resolution_count"] == 0
    assert output.resolution_receipt["place_external_call_count"] == 0


def test_unresolved_cancellation_intent_keeps_plan_pending() -> None:
    output = asyncio.run(
        build_full_text_pipeline().run(
            "北京一日游。Day 1 去故宫博物院；还没决定是否取消故宫博物院。"
        )
    )
    cards = [card for day in output.public_result.days for card in day.activities]

    assert [(card.name, card.status) for card in cards] == [
        ("故宫博物院", "NEEDS_CONFIRMATION")
    ]
    assert output.resolution_receipt["attempted_count"] == 0


def test_relative_day_cancellation_targets_only_the_explicit_day() -> None:
    output = asyncio.run(
        build_full_text_pipeline().run(
            "北京两日游。Day 1 上午去故宫博物院。"
            "Day 2 下午再次去故宫博物院；取消前一天的故宫博物院。"
        )
    )
    cards = [
        (day.label, card.name, card.time_hint)
        for day in output.public_result.days
        for card in day.activities
    ]

    assert cards == [("Day 2", "故宫博物院", "下午")]


def test_conflicting_time_and_ordinal_selectors_never_delete_a_visit() -> None:
    output = asyncio.run(
        build_full_text_pipeline().run(
            "北京一日游。Day 1 上午去故宫博物院，下午再次去故宫博物院；"
            "取消上午第二次故宫博物院。"
        )
    )
    cards = [
        (card.name, card.time_hint, card.status)
        for day in output.public_result.days
        for card in day.activities
    ]

    assert cards == [
        ("故宫博物院", "上午", "NEEDS_CONFIRMATION"),
        ("故宫博物院", "下午", "NEEDS_CONFIRMATION"),
    ]
    assert output.resolution_receipt["attempted_count"] == 0


def test_qwen_postprocessing_corrects_time_and_negated_cancel_without_a_call() -> None:
    source_text = (
        "北京一日游。Day 1 下午2点去故宫博物院；"
        "并不取消景山公园；不打算撤掉天坛公园。"
    )
    palace_start = source_text.index("故宫博物院")
    park_start = source_text.index("景山公园")
    temple_start = source_text.index("天坛公园")
    draft = QwenSemanticDraft(
        destination=QwenExplicitDestinationDraft(
            basis=DestinationBasis.EXPLICIT,
            evidence_span_start=0,
            evidence_span_end=2,
        ),
        mentions=[
            QwenMentionDraft(
                span_start=palace_start,
                span_end=palace_start + len("故宫博物院"),
                role=ActivityRole.REFERENCE,
                atomic_place_name="故宫博物院",
            ),
            QwenMentionDraft(
                span_start=park_start,
                span_end=park_start + len("景山公园"),
                role=ActivityRole.EXCLUDED,
                atomic_place_name="景山公园",
            ),
            QwenMentionDraft(
                span_start=temple_start,
                span_end=temple_start + len("天坛公园"),
                role=ActivityRole.EXCLUDED,
                atomic_place_name="天坛公园",
            ),
        ],
    )

    mentions, destination, _counts = QwenStructuredInferenceProvider._proposal_from_draft(
        source_text, draft
    )
    by_name = {mention.atomic_place_name: mention for mention in mentions}

    assert destination == "北京"
    assert by_name["故宫博物院"].role == ActivityRole.PLANNED
    assert by_name["故宫博物院"].time_hint == "14:00"
    assert by_name["景山公园"].role == ActivityRole.REFERENCE
    assert by_name["景山公园"].day_index is None
    assert by_name["天坛公园"].role == ActivityRole.REFERENCE


@pytest.mark.parametrize(
    "phrase",
    [
        "并不取消",
        "没有取消",
        "不打算撤掉",
        "不得取消",
        "不能取消",
        "无法取消",
        "未取消",
        "取消不了",
    ],
)
def test_qwen_postprocessing_never_turns_negated_cancel_into_exclusion(
    phrase: str,
) -> None:
    source_text = f"北京一日游。Day 1 {phrase}景山公园。"
    start = source_text.index("景山公园")
    draft = QwenSemanticDraft(
        destination=QwenExplicitDestinationDraft(
            basis=DestinationBasis.EXPLICIT,
            evidence_span_start=0,
            evidence_span_end=2,
        ),
        mentions=[
            QwenMentionDraft(
                span_start=start,
                span_end=start + len("景山公园"),
                role=ActivityRole.EXCLUDED,
                atomic_place_name="景山公园",
            )
        ],
    )

    mentions, _destination, _counts = QwenStructuredInferenceProvider._proposal_from_draft(
        source_text, draft
    )

    assert len(mentions) == 1
    assert mentions[0].role == ActivityRole.REFERENCE
    assert mentions[0].day_index is None


def test_qwen_postprocessing_keeps_forced_cancel_as_excluded() -> None:
    source_text = "北京一日游。Day 1 不得不取消颐和园。"
    start = source_text.index("颐和园")
    draft = QwenSemanticDraft(
        destination=QwenExplicitDestinationDraft(
            basis=DestinationBasis.EXPLICIT,
            evidence_span_start=0,
            evidence_span_end=2,
        ),
        mentions=[
            QwenMentionDraft(
                span_start=start,
                span_end=start + len("颐和园"),
                role=ActivityRole.REFERENCE,
                atomic_place_name="颐和园",
            )
        ],
    )

    mentions, _destination, _counts = QwenStructuredInferenceProvider._proposal_from_draft(
        source_text, draft
    )

    assert len(mentions) == 1
    assert mentions[0].role == ActivityRole.EXCLUDED
    assert mentions[0].day_index is None


def test_qwen_postprocessing_keeps_only_explicit_final_reschedule_planned() -> None:
    source_text = (
        "北京一日游。Day 1 原计划去故宫博物院，"
        "后来改到颐和园，最后确定去天坛公园。"
    )
    names = ("故宫博物院", "颐和园", "天坛公园")
    draft = QwenSemanticDraft(
        destination=QwenExplicitDestinationDraft(
            basis=DestinationBasis.EXPLICIT,
            evidence_span_start=0,
            evidence_span_end=2,
        ),
        mentions=[
            QwenMentionDraft(
                span_start=(start := source_text.index(name)),
                span_end=start + len(name),
                role=ActivityRole.PLANNED,
                atomic_place_name=name,
            )
            for name in names
        ],
    )

    mentions, _destination, _counts = QwenStructuredInferenceProvider._proposal_from_draft(
        source_text, draft
    )
    roles = {mention.atomic_place_name: mention.role for mention in mentions}

    assert roles == {
        "故宫博物院": ActivityRole.REFERENCE,
        "颐和园": ActivityRole.OPTIONAL,
        "天坛公园": ActivityRole.PLANNED,
    }


def test_qwen_postprocessing_cannot_emit_non_atomic_action_suffix() -> None:
    source_text = "北京一日游。Day 1 故宫博物院讲解。"
    start = source_text.index("故宫博物院讲解")
    draft = QwenSemanticDraft(
        destination=QwenExplicitDestinationDraft(
            basis=DestinationBasis.EXPLICIT,
            evidence_span_start=0,
            evidence_span_end=2,
        ),
        mentions=[
            QwenMentionDraft(
                span_start=start,
                span_end=start + len("故宫博物院讲解"),
                role=ActivityRole.PLANNED,
                atomic_place_name="故宫博物院讲解",
            )
        ],
    )

    mentions, _destination, _counts = QwenStructuredInferenceProvider._proposal_from_draft(
        source_text, draft
    )

    assert len(mentions) == 1
    assert mentions[0].atomic_place_name == "故宫博物院"
    assert mentions[0].raw_text == "故宫博物院"
    assert mentions[0].span_end - mentions[0].span_start == len("故宫博物院")
    assert mentions[0].role == ActivityRole.PLANNED
    assert pipeline_module.is_atomic_planned_place(mentions[0]) is True


def test_qwen_postprocessing_downgrades_introduction_and_old_names() -> None:
    source_text = "北京一日游。Day 1 故宫博物院介绍：旧称紫禁城。"
    palace_start = source_text.index("故宫博物院")
    old_name_start = source_text.index("紫禁城")
    draft = QwenSemanticDraft(
        destination=QwenExplicitDestinationDraft(
            basis=DestinationBasis.EXPLICIT,
            evidence_span_start=0,
            evidence_span_end=2,
        ),
        mentions=[
            QwenMentionDraft(
                span_start=palace_start,
                span_end=palace_start + len("故宫博物院"),
                role=ActivityRole.PLANNED,
                atomic_place_name="故宫博物院",
            ),
            QwenMentionDraft(
                span_start=old_name_start,
                span_end=old_name_start + len("紫禁城"),
                role=ActivityRole.PLANNED,
                atomic_place_name="紫禁城",
            ),
        ],
    )

    mentions, _destination, _counts = QwenStructuredInferenceProvider._proposal_from_draft(
        source_text, draft
    )

    assert mentions
    assert all(mention.role == ActivityRole.REFERENCE for mention in mentions)
    assert all(mention.day_index is None for mention in mentions)
    assert all(
        pipeline_module.is_atomic_planned_place(mention) is False
        for mention in mentions
    )


def test_scorer_rejects_unexpected_executable_output_and_unproven_degradation() -> None:
    case = load_cases()["cases"][7]
    observation = _empty_observation()
    observation["ordered_cards"] = [
        {"day_index": 1, "cards": [{"name": "圆明园", "time_hint": None}]}
    ]
    observation["eligible_names"] = ["圆明园"]
    observation["auto_matched_names"] = ["圆明园"]
    observation["eligible_count"] = 1
    observation["auto_matched_count"] = 1
    observation["facts"] = {"NO_DANGEROUS_OUTPUT"}

    result = _score_case(case, observation)

    assert result["status"] == "DANGEROUS_FAIL"
    assert "UNEXPECTED_EXECUTABLE_CARD" in result["failure_codes"]
    assert result["degradation_codes"] == []


def test_scorer_rejects_duration_disguised_as_visit_time() -> None:
    case = load_cases()["cases"][16]
    observation = asyncio.run(_run_pipeline_case(case))
    observation = deepcopy(observation)
    observation["ordered_cards"][0]["cards"][0]["time_hint"] = "2小时"
    observation["mentions"][0]["time_hint"] = "2小时"

    result = _score_case(case, observation)

    assert result["status"] == "DANGEROUS_FAIL"
    assert "UNSAFE_TIME_HINT" in result["failure_codes"]


def test_scorer_rejects_unlisted_degradation_code() -> None:
    case = load_cases()["cases"][7]
    observation = _empty_observation()
    observation["degradations"] = ["SAFE_OMISSION"]

    result = _score_case(case, observation)

    assert result["status"] == "DANGEROUS_FAIL"
    assert "UNAUTHORIZED_DEGRADATION" in result["failure_codes"]
