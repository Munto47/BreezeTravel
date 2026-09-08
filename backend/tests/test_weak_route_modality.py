"""Owner rule (2026-09-08): weak advice in a dated route is not a condition.

These fixed drafts isolate parser changes from the model's own role choices.
"""

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.guide_choices import explicit_optional_labels
from app.trip_understanding.pipeline import TripUnderstandingPipeline


def draft(rows):
    return SemanticDraft.model_validate({"destination": "北京", "activities": [
        {"source_quote": name, "place_name": name, "role": role, "day_index": 1}
        for name, role in rows
    ]})


@pytest.mark.parametrize("clause,names", [
    ("可以去星河公园散步。", ["星河公园"]),
    ("可去星河公园。", ["星河公园"]),
    ("星河公园可以打卡。", ["星河公园"]),
    ("星河公园可顺路参观。", ["星河公园"]),
    ("可前往【星河公园】。", ["星河公园"]),
    ("建议前往星河公园。", ["星河公园"]),
    ("可以走隔壁星河巷、云岚胡同。", ["星河巷", "云岚胡同"]),
])
@pytest.mark.asyncio
async def test_weak_route_advice_keeps_planned_cards_through_pipeline(clause, names):
    source = "北京。\nDay1：青溪公园。" + clause
    original = draft([("青溪公园", "PLANNED"), *[(name, "PLANNED") for name in names]])

    class Inference:
        async def propose(self, text):
            return proposal_from_draft(text, original)

    class NoIdentity:
        async def resolve(self, **values):
            return None  # Fixed unresolved identities; no service calls.

    assert explicit_optional_labels(source) == []
    output = await TripUnderstandingPipeline(Inference(), NoIdentity()).run(source)
    assert [(item.atomic_place_name, item.role.value, item.day_index) for item in output.proposal.mentions] == [
        (name, "PLANNED", 1) for name in ["青溪公园", *names]
    ]
    assert [card.name for card in output.public_result.days[0].activities] == ["青溪公园", *names]
    assert output.public_result.days[0].alternatives == []


@pytest.mark.parametrize("clause", [
    "如果有空，可以去星河公园。",
    "若有余力，可以去星河公园。",
    "时间充裕可以去星河公园。",
    "下午有空可去星河公园。",
    "如果下雨，星河公园可以参观。",
    "备选：星河公园可以打卡。",
])
def test_explicit_conditions_still_normalize_a_wrong_planned_role(clause):
    source = "北京。\nDay1：青溪公园。" + clause
    result = proposal_from_draft(source, draft([("青溪公园", "PLANNED"), ("星河公园", "PLANNED")]))
    assert [(item.atomic_place_name, item.role.value) for item in result.mentions] == [
        ("青溪公园", "PLANNED"), ("星河公园", "OPTIONAL"),
    ]


@pytest.mark.parametrize("prefix", [
    "如果有空，", "若有余力，", "不想挤热闹街，",
])
def test_walking_alternatives_require_a_real_condition(prefix):
    source = "Day1：青溪公园。" + prefix + "可以走隔壁星河巷、云岚胡同。"
    assert [source[a:b] for a, b in explicit_optional_labels(source)] == ["星河巷", "云岚胡同"]


@pytest.mark.parametrize("clause,role", [
    ("其他推荐：星河公园可以打卡。", "REFERENCE"),
    ("示例：星河公园可以打卡。", "REFERENCE"),
    ("星河公园可以打卡，但这次取消。", "EXCLUDED"),
    ("建议前往星河公园。", "OPTIONAL"),
])
def test_this_change_does_not_promote_model_options_references_or_cancellations(clause, role):
    source = "北京。\nDay1：青溪公园。\n" + clause
    result = proposal_from_draft(source, draft([("青溪公园", "PLANNED"), ("星河公园", role)]))
    assert [(item.atomic_place_name, item.role.value) for item in result.mentions] == [
        ("青溪公园", "PLANNED"), ("星河公园", role),
    ]


def test_a_previous_sentence_condition_does_not_reclassify_the_next_route_stop():
    source = "Day1：如果有空，可以去青溪公园。星河公园可以打卡。"
    assert [source[a:b] for a, b in explicit_optional_labels(source)] == ["青溪公园"]


def test_weak_omission_is_not_reintroduced_as_an_unselected_option():
    source = "Day1：青溪公园。Iris咖啡可以打卡。"
    from app.trip_understanding.experience_inference import _retain_explicit_optional_labels

    original = draft([("青溪公园", "PLANNED")])
    assert _retain_explicit_optional_labels(source, original) == original
