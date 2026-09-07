import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.guide_choices import explicit_optional_labels


@pytest.mark.parametrize("decoration", ["", "**"])
def test_two_suggested_streets_survive_when_both_omitted(decoration):
    source = (f"Day1：青溪公园散步。不想挤热闹街，可以走隔壁{decoration}星河巷{decoration}、"
              f"{decoration}云岚胡同{decoration}，人少些。")
    draft = SemanticDraft.model_validate({"destination": "上海", "activities": [
        {"source_quote": "青溪公园", "place_name": "青溪公园", "role": "PLANNED", "day_index": 1},
    ]})
    proposal = proposal_from_draft(source, draft)
    assert [(m.atomic_place_name, m.role.value, m.day_index) for m in proposal.mentions] == [
        ("青溪公园", "PLANNED", 1), ("星河巷", "OPTIONAL", 1), ("云岚胡同", "OPTIONAL", 1),
    ]
    assert all(source[m.span_start:m.span_end] == m.raw_text for m in proposal.mentions)


@pytest.mark.parametrize("source", [
    "并非可以走隔壁星河巷、云岚胡同。",
    "可以走隔壁星河巷、云岚胡同，不过已经取消。",
    "引用：可以走隔壁星河巷、云岚胡同。",
    "去年，可以走隔壁星河巷、云岚胡同。",
    "“可以走隔壁星河巷、云岚胡同。”",
    "> 可以走隔壁星河巷、云岚胡同。",
    "```\n可以走隔壁星河巷、云岚胡同。\n```",
    "https://example.com/可以走隔壁星河巷、云岚胡同。",
    "可以走隔壁星河巷18号、云岚胡同。",
    "可以走隔壁漂亮小路、附近胡同。",
    "可以走隔壁星河巷、云岚胡同的介绍是假的。",
])
def test_street_options_do_not_promote_negation_quotes_or_descriptions(source):
    source = "Day1：青溪公园。\n" + source
    assert explicit_optional_labels(source) == []


def test_without_a_literal_day_the_new_walking_rule_leaves_semantic_interpretation_alone():
    assert explicit_optional_labels("可以走隔壁星河巷、云岚胡同。") == []
