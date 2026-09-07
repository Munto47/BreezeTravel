"""Whole-document context, literal day scopes, partial results and call accounting."""
import json
from types import SimpleNamespace

import pytest

from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.semantic_sections import DayStructure, anchored_sections


def source():
    return "旅行背景：" + "准备充分。" * 190 + "\nDay1：星河公园。\nDay2：月光桥。"


class ScopedClient:
    def __init__(self, *, second_fails=False, cross_day=False):
        self.calls = []
        self.second_fails = second_fails
        self.cross_day = cross_day
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        prompt = kwargs["messages"][0]["content"]
        if "只分析旅行原文的全局结构" in prompt:
            payload = {"cross_day_dependencies": self.cross_day, "sections": [
                {"day_index": 1, "start_quote": "Day1"}, {"day_index": 2, "start_quote": "Day2"}]}
        elif "本次只整理原文第2天" in prompt and self.second_fails:
            payload = None
        elif "本次只整理原文第1天" in prompt:
            payload = {"activities": [{"source_quote": "星河公园", "place_name": "星河公园", "day_index": 1, "role": "PLANNED", "role_evidence": "Day1：星河公园"}]}
        elif "本次只整理原文第2天" in prompt:
            payload = {"activities": [{"source_quote": "月光桥", "place_name": "月光桥", "day_index": 2, "role": "PLANNED", "role_evidence": "Day2：月光桥"}]}
        else:
            payload = {"activities": [
                {"source_quote": "星河公园", "place_name": "星河公园", "day_index": 1, "role": "PLANNED"},
                {"source_quote": "月光桥", "place_name": "月光桥", "day_index": 2, "role": "PLANNED"}]}
        return SimpleNamespace(model="test-model", usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(payload) if payload else "{bad"))])


def provider(client):
    return ExperienceQwenProvider(api_key="test", base_url="https://test.invalid", model="test-model", client=client)


@pytest.mark.asyncio
async def test_model_day_sections_rebind_original_spans_and_count_every_call():
    text = source()
    client = ScopedClient()
    result = await provider(client).propose(text)
    assert [(item.atomic_place_name, item.day_index) for item in result.mentions] == [("星河公园", 1), ("月光桥", 2)]
    assert all(text[item.span_start:item.span_end] == item.raw_text for item in result.mentions)
    assert all(text[item.role_evidence_start:item.role_evidence_end] == item.role_evidence for item in result.mentions)
    assert result.binding["external_calls"] == len(client.calls) == 3
    assert result.binding["input_tokens"] == 30 and result.binding["output_tokens"] == 60
    assert result.binding["day_scopes_completed"] == 2


@pytest.mark.asyncio
async def test_failed_day_does_not_destroy_usable_day_or_claim_full_coverage():
    client = ScopedClient(second_fails=True)
    result = await provider(client).propose(source())
    assert [item.day_index for item in result.mentions] == [1]
    assert result.day_count == 2 and result.unprocessed_count > 0
    assert result.binding["outcome"] == "PARTIAL_RESULT"
    assert result.binding["external_calls"] == len(client.calls) == 4
    assert any(issue.category == "DAY_SECTION_UNPROCESSED" for issue in result.diagnostics)


@pytest.mark.asyncio
async def test_cross_day_dependencies_remain_whole_document_with_all_calls_counted():
    client = ScopedClient(cross_day=True)
    result = await provider(client).propose(source())
    assert len(result.mentions) == 2
    assert result.binding["day_scope_fallback"] is True
    assert result.binding["external_calls"] == len(client.calls) == 2


def test_model_cannot_invent_or_omit_day_boundaries():
    plan = DayStructure.model_validate({"cross_day_dependencies": False, "sections": [
        {"day_index": 1, "start_quote": "Day1"}, {"day_index": 3, "start_quote": "Day2"}]})
    assert anchored_sections(source(), plan) == []


def chinese_day_plan(*, cross_day=False):
    return DayStructure.model_validate({"cross_day_dependencies": cross_day, "sections": [
        {"day_index": 1, "start_quote": "第一天"},
        {"day_index": 2, "start_quote": "第二天", "occurrence": 1}]})


@pytest.mark.parametrize("previous_tail", [
    "返回酒店，为第二天的高强度步行养精蓄锐。",
    "住在附近，以便第二天清晨能轻松开启行程。",
])
def test_day_reference_in_previous_paragraph_reanchors_to_unique_heading(previous_tail):
    text = "第一天，游览星河公园。" + previous_tail + "\n\n第二天，前往月光桥。"
    sections = anchored_sections(text, chinese_day_plan())
    assert sections == [(1, 0, text.rindex("第二天")), (2, text.rindex("第二天"), len(text))]
    assert previous_tail in text[sections[0][1]:sections[0][2]]
    assert text[sections[1][1]:sections[1][2]] == "第二天，前往月光桥。"


@pytest.mark.parametrize("prefix,suffix", [("**", "**"), ("## ", ""), ("> **", "**"), ("2. ", "")])
def test_heading_boundary_accepts_markdown_without_losing_source_offsets(prefix, suffix):
    text = f"第一天：星河公园。\n\n{prefix}第二天：月光桥。{suffix}"
    sections = anchored_sections(text, chinese_day_plan())
    assert sections[1] == (2, text.index("第二天"), len(text))


@pytest.mark.parametrize("text", [
    "第一天，游览星河公园，准备第二天去月光桥。",
    "第一天，游览星河公园。第二天，游览月光桥。",
    "第一天：星河公园。\n第二天：月光桥。\n第二天：重复摘要。",
    "第二天：月光桥。\n第一天：星河公园。",
])
def test_unsupported_or_ambiguous_headings_remain_whole_document(text):
    assert anchored_sections(text, chinese_day_plan()) == []


def test_unique_heading_does_not_override_cross_day_dependency():
    text = "第一天：星河公园。\n第二天：月光桥。\n后来将月光桥改到第一天。"
    assert anchored_sections(text, chinese_day_plan(cross_day=True)) == []


@pytest.mark.asyncio
async def test_reanchored_day_inputs_keep_previous_tail_and_original_place_spans():
    previous_tail = "返回酒店，为第二天的高强度步行养精蓄锐。"
    text = "旅行背景：" + "准备充分。" * 190 + "\n第一天：星河公园。" + previous_tail + "\n\n第二天：月光桥。"

    class ProseReferenceClient(ScopedClient):
        async def create(self, **kwargs):
            if "只分析旅行原文的全局结构" in kwargs["messages"][0]["content"]:
                self.calls.append(kwargs)
                return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
                    choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                        content=chinese_day_plan().model_dump_json()))])
            response = await super().create(**kwargs)
            payload = json.loads(response.choices[0].message.content)
            for activity in payload["activities"]:
                activity.pop("role_evidence", None)
            response.choices[0].message.content = json.dumps(payload)
            return response

    client = ProseReferenceClient()
    result = await provider(client).propose(text)
    first = next(call for call in client.calls if "本次只整理原文第1天" in call["messages"][0]["content"])
    second = next(call for call in client.calls if "本次只整理原文第2天" in call["messages"][0]["content"])
    assert previous_tail in first["messages"][-1]["content"]
    assert previous_tail not in second["messages"][-1]["content"]
    assert "第二天：月光桥。" in second["messages"][-1]["content"]
    assert [(item.atomic_place_name, item.day_index) for item in result.mentions] == [("星河公园", 1), ("月光桥", 2)]
    assert all(text[item.span_start:item.span_end] == item.raw_text for item in result.mentions)
    assert result.binding["external_calls"] == len(client.calls) == 3
