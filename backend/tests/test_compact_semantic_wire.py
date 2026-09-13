"""Lossless development wire and real local provider/pipeline, zero services."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS

from jsonschema import Draft202012Validator
import pytest
from pydantic import ValidationError

from app.trip_understanding.compact_semantic_wire import (
    compact_draft_payload, complete_compact_items_from_truncated_json, expand_compact_payload,
)
from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.experience_inference import ExperienceQwenProvider, SemanticDraft
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces


def row(name, day=1, role="PLANNED", **extra):
    return {"source_quote": name, "place_name": name, "day_index": day, "role": role, "source_details": [], **extra}


def payload(rows, days=1, **extra):
    return dict(destination="北京", day_labels=[None] * days, activities=rows,
        unprocessed_quotes=[], order_groups=[], **extra)


def compact(value):
    return compact_draft_payload(SemanticDraft.model_validate(value))


class Client:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.chat = NS(completions=self)

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        assert len(self.calls) <= 2
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        answer, reason = answer if isinstance(answer, tuple) else (answer, "stop")
        return NS(model="fixed", usage=NS(prompt_tokens=11, completion_tokens=13),
            choices=[NS(finish_reason=reason, message=NS(content=answer if isinstance(answer, str)
                else json.dumps(answer, ensure_ascii=False)))])


def provider(client, **kwargs):
    return ExperienceQwenProvider(api_key="fixed", base_url="https://fixed.invalid", model="fixed",
        client=client, enable_compact_wire=True, enable_day_sections=False, **kwargs)


async def run(source, *answers, **kwargs):
    client = Client(*answers)
    result = await TripUnderstandingPipeline(provider(client, **kwargs), FixedReplayPlaces(), relative_only=True).run(source)
    return result, client


def test_default_wire_worker_contract_is_unchanged_and_legacy_reader_is_separate():
    instance = ExperienceQwenProvider(api_key="fixed", base_url="https://fixed.invalid", model="fixed", client=Client())
    assert not instance.enable_compact_wire and not instance.enable_focused_repair
    assert "activities" in instance.schema["properties"] and "blocks" not in instance.schema["properties"]
    assert "本请求只改变JSON表示" not in instance.prompt
    old = payload([row("青溪公园")])
    assert provider(Client())._read_draft("北京。Day1：青溪公园。", old) == SemanticDraft.model_validate(old)


def test_schema_declares_block_semantics_and_retains_active_business_fields():
    instance = provider(Client())
    schema = instance.schema
    Draft202012Validator.check_schema(schema)
    block = schema["$defs"]["CompactSemanticBlock"]
    assert set(block["required"]) == {"day_index", "role", "category", "detail_mode", "items"}
    assert block["properties"]["items"]["maxItems"] == 160
    item = schema["$defs"]["CompactSemanticItem"]
    assert item["required"] == ["name"]
    assert {"city", "city_evidence", "meal_role", "lodging_event", "source_details", "occurrence"} <= item["properties"].keys()
    assert not {"place_name", "source_quote", "role", "day_index", "start_time", "time_evidence"} & item["properties"].keys()
    assert schema["properties"]["day_labels"]["items"] == {"type": "null"}
    Draft202012Validator(schema).validate(compact(payload([row("青溪公园")])))


@pytest.mark.parametrize("field", ["day_index", "role", "category", "detail_mode", "items"])
def test_missing_block_header_never_defaults_to_planned_or_empty(field):
    value = compact(payload([row("青溪公园")]))
    del value["blocks"][0][field]
    with pytest.raises(ValidationError):
        expand_compact_payload(value)


@pytest.mark.parametrize("field,value", [("role", "GUESS"), ("day_index", 15), ("detail_mode", "GUESS"), ("extra", "ignored")])
def test_unknown_or_out_of_range_header_is_rejected(field, value):
    encoded = compact(payload([row("青溪公园")]))
    encoded["blocks"][0][field] = value
    with pytest.raises(ValidationError):
        expand_compact_payload(encoded)


def test_roundtrip_preserves_flat_order_revisits_choices_quotes_and_all_existing_fields():
    rows = [row("星河酒店", category="住宿", lodging_event="OVERNIGHT", lodging_scope="DAY", lodging_evidence="入住星河酒店"),
        row("星河公园", category="景点", city="北京", city_evidence="北京"),
        row("青溪桥", role="OPTIONAL"), row("晨光亭", role="OPTIONAL"),
        row("星河公园", day=2, occurrence=2, category="景点"),
        dict(source_quote="晚餐想吃面", place_name=None, role="PLANNED", day_index=2,
            category="餐饮", meal_role="DINNER", source_details=[]),
        row("星河酒店", day=2, occurrence=2, category="住宿", lodging_event="LUGGAGE_PICKUP", lodging_evidence="回星河酒店取行李"),
    ]
    rows[1]["source_quote"] = "游览星河公园"
    value = payload(rows, 2, choice_groups=[dict(scope_quote="青溪桥或晨光亭", branches=[
        dict(activity_indices=[2]), dict(activity_indices=[3])])])
    value["order_groups"] = [dict(kind="INITIAL_ORDER", activity_indices=[1], scope_quote="游览星河公园")]
    draft = SemanticDraft.model_validate(value)
    encoded = compact_draft_payload(draft)
    restored = SemanticDraft.model_validate(expand_compact_payload(encoded))
    assert restored == draft
    assert restored.choice_groups == draft.choice_groups and restored.order_groups == draft.order_groups
    assert [a.role for a in restored.activities] == [a.role for a in draft.activities]
    assert [a.source_quote for a in restored.activities] == [a.source_quote for a in draft.activities]


def test_absent_details_are_not_encoded_as_empty_and_null_name_requires_its_quote():
    value = payload([row("青溪公园")])
    del value["activities"][0]["source_details"]
    encoded = compact(value)
    assert encoded["blocks"][0]["detail_mode"] == "UNASSESSED"
    restored = SemanticDraft.model_validate(expand_compact_payload(encoded))
    assert "source_details" not in restored.activities[0].model_fields_set
    assert restored.unprocessed_quotes == ["青溪公园"]
    encoded["blocks"][0]["items"][0]["name"] = None
    with pytest.raises(ValueError, match="ANONYMOUS_QUOTE"):
        expand_compact_payload(encoded)
    del encoded["blocks"][0]["items"][0]["name"]
    with pytest.raises(ValidationError):
        expand_compact_payload(encoded)


@pytest.mark.parametrize("headers_first", [True, False])
def test_truncation_never_guesses_headers_even_if_complete_items_precede_them(headers_first):
    header = '"day_index":1,"role":"PLANNED","category":"景点","detail_mode":"EMPTY",'
    tail = '"items":[{"name":"青溪公园"},{"name":"未闭合'
    text = '{"destination":"北京","blocks":[{' + (header if headers_first else "") + tail
    recovered = complete_compact_items_from_truncated_json(text)
    if headers_first:
        assert [r["place_name"] for r in expand_compact_payload(recovered)["activities"]] == ["青溪公园"]
    else:
        assert recovered is None


def test_complete_previous_blocks_survive_an_unsafe_later_items_first_block():
    value = compact(payload([row("青溪公园")]))
    block = json.dumps(value["blocks"][0], ensure_ascii=False)
    text = '{"blocks":[' + block + ',{"items":[{"name":"月光桥"},{'
    recovered = complete_compact_items_from_truncated_json(text)
    assert len(expand_compact_payload(recovered)["activities"]) == 1


@pytest.mark.parametrize("fields_before_blocks", [False, True])
def test_truncation_preserves_complete_top_fields_on_both_sides_of_closed_blocks(fields_before_blocks):
    block = compact(payload([row("青溪公园")]))["blocks"]
    fields = dict(destination="北京", day_labels=[None, None], unprocessed_quotes=["第二天待整理"], choice_groups=[])
    value = {**fields, "blocks": block} if fields_before_blocks else {"blocks": block, **fields}
    text = json.dumps(value, ensure_ascii=False)[:-1] + ',"order_groups":[{"kind":"INITIAL_ORDER","activity_indices":[0],"scope_quote":"未闭合'
    recovered = complete_compact_items_from_truncated_json(text)
    assert all(recovered[k] == v for k, v in fields.items())
    assert recovered["blocks"] == block
    assert "order_groups" not in recovered  # An unfinished scope is never assessed.


def test_truncation_does_not_invent_an_unfinished_late_city_or_skip_it_to_later_text():
    blocks = compact(payload([row("青溪公园")]))["blocks"]
    text = json.dumps({"blocks": blocks}, ensure_ascii=False)[:-1] + ',"destination":"北'
    recovered = complete_compact_items_from_truncated_json(text)
    assert "destination" not in recovered and recovered["blocks"] == blocks


def test_complete_late_unknown_field_is_retained_for_strict_validation():
    blocks = compact(payload([row("青溪公园")]))["blocks"]
    text = json.dumps({"blocks": blocks, "unrecognized": "not silently discarded"})[:-1] + ',"destination":"北'
    recovered = complete_compact_items_from_truncated_json(text)
    assert recovered["unrecognized"] == "not silently discarded"
    with pytest.raises(ValidationError):
        expand_compact_payload(recovered)


@pytest.mark.asyncio
async def test_late_complete_city_keeps_fixed_identity_after_truncated_order_and_timeout():
    source = "北京。\nDay1：青溪公园。"
    blocks = compact(payload([row("青溪公园")]))["blocks"]
    text = json.dumps(dict(blocks=blocks, destination="北京", day_labels=[None], unprocessed_quotes=[]),
        ensure_ascii=False)[:-1] + ',"order_groups":[{"kind":"INITIAL_ORDER","activity_indices":[0],"scope_quote":"Day1'
    result, client = await run(source, (text, "length"), TimeoutError())
    assert result.proposal.destination_name == "北京"
    assert [(c.name, c.status) for c in result.public_result.days[0].activities] == [("青溪公园", "READY")]
    assert len(result.proposal.order_assessment.unassessed_mention_ids) == 1
    assert result.proposal.order_assessment.groups == ()
    assert result.proposal.unprocessed_count == 1 and not result.public_result.coverage.complete
    assert result.proposal.binding["external_calls"] == len(client.calls) == 2


@pytest.mark.asyncio
async def test_compact_provider_preserves_initial_order_and_fixed_public_identity():
    source = "北京。\nDay1：青溪公园、月光桥。"
    value = payload([row("青溪公园"), row("月光桥")])
    value["order_groups"] = [dict(kind="INITIAL_ORDER", activity_indices=[0, 1], scope_quote="Day1：青溪公园、月光桥。")]
    result, client = await run(source, compact(value))
    assert [c.name for c in result.public_result.days[0].activities] == ["青溪公园", "月光桥"]
    assert result.public_result.coverage.complete
    assert result.proposal.order_assessment.groups[0].kind == "INITIAL_ORDER"
    assert result.proposal.binding["compact_wire_enabled"] is True
    assert result.proposal.binding["external_calls"] == len(client.calls) == 1
    assert client.calls[0]["messages"][1]["content"] == source


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["{broken", TimeoutError()])
async def test_truncated_partial_survives_failed_second_answer_and_accounts_both_calls(second):
    source = "北京。\nDay1：青溪公园、月光桥。"
    truncated = '{"destination":"北京","day_labels":[null],"blocks":[{"day_index":1,"role":"PLANNED","category":"景点","detail_mode":"EMPTY","items":[{"name":"青溪公园"},{"name":"月'
    result, client = await run(source, (truncated, "length"), second)
    assert [c.name for c in result.public_result.days[0].activities] == ["青溪公园"]
    assert not result.public_result.coverage.complete and result.proposal.unprocessed_count > 0
    assert result.proposal.binding["external_calls"] == len(client.calls) == 2
    assert result.proposal.binding["output_tokens"] == (None if isinstance(second, TimeoutError) else 26)


@pytest.mark.parametrize("mode,details", [("EMPTY", [{"kind": "VISIT"}]), ("UNASSESSED", [])])
def test_contradictory_detail_decisions_do_not_become_complete(mode, details):
    value = compact(payload([row("青溪公园")]))
    value["blocks"][0]["detail_mode"] = mode
    value["blocks"][0]["items"][0]["source_details"] = details
    with pytest.raises(ValueError, match="COMPACT_CONTRADICTORY_DETAILS"):
        expand_compact_payload(value)


@pytest.mark.asyncio
async def test_unassessed_detail_decision_retains_parent_but_stays_incomplete():
    source = "北京。\nDay1：青溪公园。"
    value = payload([row("青溪公园")])
    del value["activities"][0]["source_details"]
    encoded = compact(value)
    result, client = await run(source, encoded, encoded)
    assert [c.name for c in result.public_result.days[0].activities] == ["青溪公园"]
    assert not result.public_result.coverage.complete and result.proposal.unprocessed_count > 0
    assert len(client.calls) == 1  # A declared unprocessed fragment is retained; no new repair workflow.


def test_unknown_fragments_never_silently_trim_the_existing_eighty_fragment_bound():
    value = payload([row(f"待评估{i}公园") for i in range(81)])
    for item in value["activities"]:
        del item["source_details"]
    expanded = expand_compact_payload(compact(value))
    assert len(expanded["activities"]) == len(expanded["unprocessed_quotes"]) == 81
    with pytest.raises(ValidationError) as caught:
        SemanticDraft.model_validate(expanded)
    assert any(e["loc"] == ("unprocessed_quotes",) and e["type"] == "too_long" for e in caught.value.errors())


@pytest.mark.asyncio
async def test_semantic_repair_keeps_wire_format_and_source_validation():
    source = "北京。\nDay1：青溪公园。"
    wrong = payload([row("青溪公园")])
    wrong["activities"][0]["source_quote"] = "不存在的引文"
    correct = payload([row("青溪公园")])
    result, client = await run(source, compact(wrong), compact(correct))
    assert [c.name for c in result.public_result.days[0].activities] == ["青溪公园"]
    assert len(client.calls) == 2
    assistant = json.loads(client.calls[1]["messages"][-2]["content"])
    assert "blocks" in assistant and "activities" not in assistant


@pytest.mark.asyncio
async def test_external_cancellation_propagates_without_third_call():
    client = Client(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await provider(client).propose("北京。Day1：青溪公园。")
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_inline_optional_parent_and_cancelled_detail_keep_existing_validator(cancelled):
    action = "不玩" if cancelled else "必玩："
    evidence = action + "云海航船"
    source = "北京。\nDay1：若有空去青岚乐园，园内" + evidence + "。"
    value = payload([row("青岚乐园", role="OPTIONAL", source_details=[
        dict(kind="VISIT", source_quote="云海航船", evidence=evidence, optional=False)])])
    encoded = compact(value)
    result, _ = await run(source, encoded, encoded, enable_source_visits=True)
    assert not result.public_result.days[0].activities
    alternative = result.public_result.days[0].alternatives[0]
    assert alternative.name == "青岚乐园"
    assert [item.name for item in alternative.source_details] == ([] if cancelled else ["云海航船"])
    if cancelled:
        assert not result.public_result.coverage.complete


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [160, 161])
@pytest.mark.parametrize("single_block", [False, True])
async def test_total_capacity_counts_flattened_source_visits_without_raising_limit(count, single_block):
    names = [f"容量测试{i:03d}公园" for i in range(count)]
    source = "北京。\nDay1：" + "、".join(names) + "。"
    encoded = compact(payload([row(name) for name in names[:160]]))
    encoded["order_groups"] = [dict(kind="INITIAL_ORDER", activity_indices=list(range(160)), scope_quote=source)]
    if count == 161:
        if single_block:
            encoded["blocks"][0]["items"].append(dict(name=names[-1]))
        else:
            encoded["blocks"].append(dict(day_index=1, role="PLANNED", category="景点", detail_mode="EMPTY", items=[dict(name=names[-1])]))
        client = Client(encoded)
        with pytest.raises(InferenceProviderUnavailableError) as caught:
            await provider(client).propose(source)
        assert caught.value.category == "INPUT_CAPACITY_EXCEEDED"
        assert caught.value.external_call_count == len(client.calls) == 1
    else:
        result, client = await run(source, encoded)
        assert [c.name for c in result.public_result.days[0].activities] == names
        assert result.public_result.coverage.complete and len(client.calls) == 1


@pytest.mark.asyncio
async def test_actual_sdk_compact_request_uses_same_strict_budget_and_full_source():
    import httpx
    from openai import AsyncOpenAI

    source = "北京。\nDay1：青溪公园。"
    answer = compact(payload([row("青溪公园")]))
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=dict(id="fixed", object="chat.completion", created=0, model="fixed",
            choices=[dict(index=0, finish_reason="stop", message=dict(role="assistant", content=json.dumps(answer, ensure_ascii=False)))],
            usage=dict(prompt_tokens=10, completion_tokens=10, total_tokens=20)))

    async with AsyncOpenAI(api_key="fixed", base_url="https://fixed.invalid/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
        result = await provider(client).propose(source)
    assert len(requests) == result.binding["external_calls"] == 1
    request = requests[0]
    assert request["messages"][1]["content"] == source
    assert request["max_tokens"] == 4096 and request["temperature"] == 0 and request["enable_thinking"] is False
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    Draft202012Validator(request["response_format"]["json_schema"]["schema"]).validate(answer)


@pytest.mark.asyncio
async def test_day_scopes_return_same_typed_plan_without_changing_existing_scope_dispatch():
    source = "北京。背景：" + "准备充分。" * 190 + "\nDay1：青溪公园。\nDay2：月光桥。"

    class Scoped:
        def __init__(self):
            self.calls = []
            self.chat = NS(completions=self)

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            prompt = kwargs["messages"][0]["content"]
            if "只分析旅行原文的全局结构" in prompt:
                assert kwargs["messages"][1]["content"] == source
                value = dict(cross_day_dependencies=False, sections=[dict(day_index=i, start_quote=f"Day{i}") for i in [1, 2]])
            else:
                day = 2 if "本次只整理原文第2天" in prompt else 1
                value = compact(payload([row("月光桥" if day == 2 else "青溪公园", day=day)], days=2))
                assert kwargs["messages"][1]["content"].startswith("北京。背景：")
                assert f"Day{day}：" in kwargs["messages"][1]["content"]
            return NS(model="fixed", usage=NS(prompt_tokens=1, completion_tokens=1), choices=[NS(
                finish_reason="stop", message=NS(content=json.dumps(value, ensure_ascii=False)))])

    client = Scoped()
    inference = provider(client)
    inference.enable_day_sections = True
    result = await inference.propose(source)
    assert [(m.atomic_place_name, m.day_index) for m in result.mentions] == [("青溪公园", 1), ("月光桥", 2)]
    assert result.binding["compact_wire_enabled"] is True
    assert result.binding["external_calls"] == len(client.calls) == 3
