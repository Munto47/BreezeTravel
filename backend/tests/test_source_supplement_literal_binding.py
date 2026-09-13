"""Literal second-answer spans tolerate decoration, never changed facts."""
import json
from pathlib import Path

import pytest

from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_city_evidence_recovery import FixedCities
from tests.test_semantic_supplement_budget import Client, provider
from tests.test_source_visit_supplement import apply, plan, row


@pytest.mark.asyncio
async def test_saved_two_answer_markdown_and_gate_actions_reach_their_original_parents():
    fixture = json.loads((Path(__file__).parent / "fixtures/live_source_supplement_markdown.json").read_text(encoding="utf-8"))
    client = Client(*fixture["responses"])
    output = await TripUnderstandingPipeline(provider(client), FixedCities()).run(fixture["source"])
    assert len(client.calls) == 2
    attached = [mention for mention in output.proposal.mentions if mention.parent_mention_id]
    assert len(attached) == 16
    assert sorted(int(issue.field.split("[")[1].split("]")[0]) for issue in output.proposal.diagnostics
                  if issue.category == "SOURCE_VISIT_UNRESOLVED") == fixture["expected_rejected_rows"]
    by_parent = {card.name: [detail.name for detail in card.source_details]
                 for day in output.public_result.days for card in day.activities if card.source_details}
    assert by_parent["故宫博物院"] == ["入口：午门", "太和殿", "乾清宫", "御花园", "珍宝馆", "钟表馆", "出口：神武门"]
    assert by_parent["景山公园"] == ["万春亭"]
    assert by_parent["天坛公园"] == ["入口：南门", "祈年殿", "回音壁", "圜丘", "出口：北门"]
    assert by_parent["国家博物馆"] == ["古代中国展厅"]
    assert by_parent["鸟巢"] == by_parent["水立方"] == ["仅看外观，不入内部"]
    for mention in attached:
        assert fixture["source"][mention.span_start:mention.span_end] == mention.raw_text
        assert fixture["source"][mention.role_evidence_start:mention.role_evidence_end] == mention.role_evidence
    assert not output.public_result.coverage.complete  # Self-parent rows and existing omissions remain.


@pytest.mark.parametrize("kind,quote,name", [("ENTRY", "南门进", "南门"), ("EXIT", "北门出", "北门")])
def test_unknown_venue_gate_action_keeps_the_whole_quote_occurrence(kind, quote, name):
    source = "苏州。\nDay1：青岚园，**南门进**、**北门出**，园内看听雨亭。"
    before = plan(source, [("青岚园", 1, 1)])
    result = apply(source, before, [row(0, kind, quote, "青岚园，南门进、北门出，园内看听雨亭")])
    assert result.unprocessed_count == 0
    child = result.mentions[-1]
    assert child.atomic_place_name == name
    assert source[child.span_start:child.span_end] == quote
    assert source[child.role_evidence_start:child.role_evidence_end] == child.role_evidence


@pytest.mark.parametrize("kind,source,evidence", [
    ("EXTERIOR_ONLY", "苏州。\nDay1：**青岚园**，**仅看外观**，不入园。", "青岚园，仅看外观，不入园"),
    ("PICKUP_ONLY", "苏州。\nDay1：**青岚园**，**只取寄存行李**，不进展厅。", "青岚园，只取寄存行李，不进展厅"),
])
def test_purpose_uses_reversible_action_coordinates_after_markdown(kind, source, evidence, monkeypatch):
    from app.trip_understanding import source_visit_supplement as module
    before = plan(source, [("青岚园", 1, 1)])
    original = module._scope_parent
    actions = []

    def observe(source, anchors, row, parent, roots, start, end, **kwargs):
        actions.append(source[start:end])
        return original(source, anchors, row, parent, roots, start, end, **kwargs)

    monkeypatch.setattr(module, "_scope_parent", observe)
    result = apply(source, before, [row(0, kind, "青岚园", evidence)])
    assert result.unprocessed_count == 0
    assert actions == (["仅看外观"] if kind == "EXTERIOR_ONLY" else ["取寄存行李"])
    child = result.mentions[-1]
    assert source[child.role_evidence_start:child.role_evidence_end] == child.role_evidence


def test_matching_gate_phrase_never_moves_to_the_next_days_occurrence():
    source = "苏州。\nDay1：青岚园，**南门进**。\nDay2：听雨园，**南门进**。"
    before = plan(source, [("青岚园", 1, 1), ("听雨园", 2, 1)])
    wrong = apply(source, before, [row(1, "ENTRY", "南门进", "听雨园，南门进", occurrence=1)])
    assert wrong.mentions == before.mentions and wrong.unprocessed_count == 1
    correct = apply(source, before, [row(1, "ENTRY", "南门进", "听雨园，南门进", occurrence=2)])
    assert correct.unprocessed_count == 0
    child = correct.mentions[-1]
    assert child.parent_mention_id == before.mentions[1].mention_id
    assert child.span_start == source.rindex("南门进") and child.raw_text == "南门进"
    assert child.atomic_place_name == "南门" and child.day_index == 2


def test_unknown_internal_name_uses_only_paired_inline_decoration():
    source = "苏州。\nDay1：青岚园，园内先看**听雨亭**，再看`晴岚阁`。"
    before = plan(source, [("青岚园", 1, 1)])
    result = apply(source, before, [row(0, "VISIT", name, "园内先看听雨亭，再看晴岚阁")
                                  for name in ("听雨亭", "晴岚阁")])
    assert result.unprocessed_count == 0
    assert [item.atomic_place_name for item in result.mentions[1:]] == ["听雨亭", "晴岚阁"]
    for child in result.mentions[1:]:
        assert child.role_evidence == source[child.role_evidence_start:child.role_evidence_end]


@pytest.mark.parametrize("source,kind,quote,evidence,occurrence", [
    ("苏州。\nDay1：青岚园，**南门进**北门出。", "EXIT", "南门进", "青岚园，南门进北门出", 1),
    ("苏州。\nDay1：青岚园，**南门出**北门进。", "ENTRY", "南门出", "青岚园，南门出北门进", 1),
    ("苏州。\nDay1：青岚园，不从**南门进**，从北门进。", "ENTRY", "南门进", "青岚园，不从南门进，从北门进", 1),
    ("苏州。\nDay1：青岚园，不**从**南门进，从北门进。", "ENTRY", "南门进", "青岚园，不从南门进，从北门进", 1),
    ("苏州。\nDay1：青岚园，不`从`南门进，从北门进。", "ENTRY", "南门进", "青岚园，不从南门进，从北门进", 1),
    ("苏州。\nDay1：青岚园，园内不__参观__听雨亭。", "VISIT", "听雨亭", "青岚园，园内不参观听雨亭", 1),
    ("苏州。\nDay1：青岚园，时间**充裕**参观听雨亭。", "VISIT", "听雨亭", "青岚园，时间**充裕**参观听雨亭", 1),
    ("苏州。\nDay1：南门涮肉吃饭。青岚园，**南门进**北门出。", "ENTRY", "南门", "青岚园，南门进北门出", 1),
    ("苏州。\nDay1：青岚园，**南门进**北门出。", "ENTRY", "南门进", "青岚园，南门进北门出", 2),
    ("苏州。\nDay1：青岚园，**南门出**北门进。", "ENTRY", "南门", "青岚园，南门进北门出", 1),
    ("苏州。\nDay1：青岚园，*南门进，北门出。", "ENTRY", "南门进", "青岚园，南门进，北门出", 1),
    ("苏州。\nDay1：青岚园，南门进，备注[关闭](临时)。", "ENTRY", "南门进", "青岚园，南门进，备注关闭", 1),
])
def test_gate_normalization_never_repairs_direction_occurrence_or_factual_text(source, kind, quote, evidence, occurrence):
    before = plan(source, [("青岚园", 1, 1)])
    result = apply(source, before, [row(0, kind, quote, evidence, occurrence=occurrence)])
    assert result.mentions == before.mentions
    assert result.unprocessed_count == before.unprocessed_count + 1


@pytest.mark.parametrize("direction,kind", [("进", "ENTRY"), ("出", "EXIT")])
@pytest.mark.parametrize("prefix", ["不从", "不**从**", "不要由", "并非经"])
def test_short_gate_evidence_cannot_omit_source_negation(direction, kind, prefix):
    source = f"北京。\nDay1：青岚园，{prefix}南门{direction}，从北门{direction}。"
    before = plan(source, [("青岚园", 1, 1)])
    quote = f"南门{direction}"
    result = apply(source, before, [row(0, kind, quote, quote)])
    assert result.mentions == before.mentions
    assert result.unprocessed_count == 1
