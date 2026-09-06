"""The bounded repair receives literal help without changing source authority."""
import json

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, SourceAnchorValidationError, _repair_prompt, proposal_from_draft
from tests.test_experience_inference import Client, provider


def test_repair_labels_are_structured_and_repeated_names_keep_every_occurrence():
    source = "Day1：星河公园。\nDay2：先到星河公园，再去青溪桥。"
    previous = json.dumps({"activities": [{"place_name": "星河公园"}, {"place_name": "青溪桥"}]})
    error = SourceAnchorValidationError(
        [{"field": "activities[0].source_quote", "category": "SOURCE_DAY_QUOTE_MISMATCH"}],
        [json.dumps({"source_quote": "星河公园", "occurrence": 2}), "青溪桥"],
    )
    message = _repair_prompt(source, previous, error)
    data = json.loads(message.split("\n", 1)[1])
    assert data["missing_labels"] == [{"source_quote": "星河公园", "occurrence": 2}, {"place_name": "青溪桥"}]
    assert data["literal_nouns"] == [
        {"source_quote": "星河公园", "occurrences": [{"occurrence": 1, "preceding_day": 1}, {"occurrence": 2, "preceding_day": 2}]},
        {"source_quote": "青溪桥", "occurrences": [{"occurrence": 1, "preceding_day": 2}]},
    ]
    assert "二选一" not in message  # Do not bury this error in unrelated instructions.


@pytest.mark.parametrize("previous", ["{bad", "null", "[]", '"text"', '{"activities":null}', '{"activities":[null,3,{}]}'])
def test_malformed_previous_json_still_gets_one_safe_repair_request(previous):
    message = _repair_prompt("普通原文", previous, ValueError("OUTPUT_TRUNCATED"))
    assert json.loads(message.split("\n", 1)[1])["literal_nouns"] == []


def test_candidates_do_not_add_absent_names_or_treat_url_day_as_a_heading():
    previous = json.dumps({"activities": [{"place_name": "假的公园"}, {"place_name": "星河公园"}]})
    data = json.loads(_repair_prompt("https://example.test/Day8 星河公园", previous, ValueError()).split("\n", 1)[1])
    assert data["literal_nouns"] == [{"source_quote": "星河公园", "occurrences": [{"occurrence": 1, "preceding_day": None}]}]


def test_repeated_candidates_are_bounded_and_deduplicated():
    names = [f"星河{i}公园" for i in range(160)]
    source = "，".join(name for name in names for _ in range(20))
    previous = json.dumps({"activities": [{"place_name": name} for name in names] * 2})
    data = json.loads(_repair_prompt(source, previous, ValueError()).split("\n", 1)[1])
    assert sum(len(noun["occurrences"]) for noun in data["literal_nouns"]) <= 320
    assert all(len(noun["occurrences"]) <= 16 for noun in data["literal_nouns"])


@pytest.mark.asyncio
async def test_repair_still_validates_and_does_not_log_literal_help():
    source = "Day1：星河公园。"
    wrong = {"activities": [{"source_quote": "未出现的引用", "place_name": "星河公园", "role": "PLANNED", "day_index": 1}]}
    correct = {"activities": [{"source_quote": "星河公园", "place_name": "星河公园", "role": "PLANNED", "day_index": 1}]}
    client = Client(json.dumps(wrong), json.dumps(correct))
    result = await provider(client).propose(source)
    assert [mention.atomic_place_name for mention in result.mentions] == ["星河公园"]
    assert len(client.calls) == 2
    assert "星河公园" in client.calls[1]["messages"][-1]["content"]
    assert "星河公园" not in json.dumps(result.binding, ensure_ascii=False)
    assert result.binding["calls"][0]["outcome"] == "SOURCE_QUOTE_NOT_FOUND"


def test_day_error_does_not_hide_other_errors_from_the_only_repair():
    source = "Day1：市区\n星河公园。\nDay2：重访\n上午星河公园，晚上再回星河公园。青溪桥。"
    draft = SemanticDraft.model_validate({"activities": [
        {"source_quote": "星河公园", "place_name": "星河公园", "role": "PLANNED", "day_index": 2,
         "start_time": "09:00", "time_evidence": "不存在的时间"},
        {"source_quote": "不存在的引用", "place_name": "青溪桥", "role": "PLANNED", "day_index": 2},
    ]})
    with pytest.raises(SourceAnchorValidationError) as caught:
        proposal_from_draft(source, draft)
    assert {issue["category"] for issue in caught.value.issues} >= {
        "SOURCE_DAY_QUOTE_MISMATCH", "TIME_EVIDENCE_NOT_IN_SOURCE", "SOURCE_QUOTE_NOT_FOUND",
    }
    assert caught.value.issues[0]["field"] == "activities[0].occurrence"
    assert caught.value.repair_draft is not None
    hint = json.loads(caught.value.repair_hints[0])
    assert hint["body_occurrences_in_proposed_day"] == [2, 3]


@pytest.mark.asyncio
async def test_repair_field_indices_refer_to_the_expanded_assistant_draft():
    source = "Day1：星河公园、云岭公园，接着青溪桥。"
    first = {"activities": [
        {"source_quote": "星河公园、云岭公园", "place_name": "星河公园、云岭公园", "role": "PLANNED", "day_index": 1},
        {"source_quote": "不存在的引用", "place_name": "青溪桥", "role": "PLANNED", "day_index": 1},
    ]}
    second = {"activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1}
        for name in ["星河公园", "云岭公园", "青溪桥"]
    ]}
    client = Client(json.dumps(first), json.dumps(second))
    result = await provider(client).propose(source)
    repair_messages = client.calls[1]["messages"]
    actual = json.loads(repair_messages[-2]["content"])["activities"]
    data = json.loads(repair_messages[-1]["content"].split("\n", 1)[1])
    assert [row["place_name"] for row in actual] == ["星河公园", "云岭公园", "青溪桥"]
    assert {"field": "activities[2].source_quote", "category": "SOURCE_QUOTE_NOT_FOUND"} in data["errors"]
    assert [item.atomic_place_name for item in result.mentions] == ["星河公园", "云岭公园", "青溪桥"]
