"""Actual SDK request shape, semantic validation, and old-reader compatibility."""
from contextlib import asynccontextmanager
import copy
import json

import httpx
from jsonschema import Draft202012Validator
from openai import AsyncOpenAI
import pytest

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.experience_inference import ExperienceQwenProvider, SemanticDraft
from app.trip_understanding.models import SourceSemanticPlan


MODEL = "qwen3.7-flash-2026-07-15"
SOURCE = "北京。Day1：青溪公园。"
VALID = dict(destination="北京", day_labels=[None], unprocessed_quotes=[], activities=[
    dict(source_quote="青溪公园", place_name="青溪公园", role="PLANNED", day_index=1),
], order_groups=[dict(kind="INITIAL_ORDER", activity_indices=[0], scope_quote="Day1：青溪公园。")])


@asynccontextmanager
async def recorded_provider(*outputs):
    """No network: capture the JSON serialized by the real OpenAI SDK."""
    remaining = list(outputs)
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        value = remaining.pop(0)  # Any unexpected extra call fails the test.
        if isinstance(value, tuple):
            status, body = value
            return httpx.Response(status, json=body)
        return httpx.Response(200, json=dict(
            id="fixed-response", object="chat.completion", created=0, model=MODEL,
            choices=[dict(index=0, finish_reason="stop", message=dict(role="assistant",
                content=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)))],
            usage=dict(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ))

    async with AsyncOpenAI(api_key="test-only", base_url="https://provider.invalid/v1",
        max_retries=0, timeout=30, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
        provider = ExperienceQwenProvider(api_key="test-only", base_url="https://provider.invalid/v1",
            model=MODEL, deadline_seconds=30, max_output_tokens=4096, client=client)
        yield provider, requests


def request_schema(request):
    wire = request["response_format"]
    assert wire["type"] == "json_schema"
    assert wire["json_schema"]["strict"] is True
    assert wire["json_schema"]["name"] == "BreezeTravelSemanticDraft"
    schema = wire["json_schema"]["schema"]
    in_prompt = json.loads(request["messages"][0]["content"].split("JSON Schema:\n", 1)[1])
    assert schema == in_prompt
    Draft202012Validator.check_schema(schema)
    assert request["model"] == MODEL
    assert request["max_tokens"] == 4096
    assert request["enable_thinking"] is False
    return schema


@pytest.mark.asyncio
async def test_actual_first_and_semantic_repair_requests_share_strict_schema_and_budget():
    invalid = copy.deepcopy(VALID)
    invalid["activities"][0]["place_name"] = "原文没有的景点"
    async with recorded_provider(invalid, VALID) as (provider, requests):
        result = await provider.propose(SOURCE)
    assert len(requests) == result.binding["external_calls"] == 2
    schemas = [request_schema(request) for request in requests]
    assert schemas[0] == schemas[1]
    assert requests[0]["response_format"] == requests[1]["response_format"]
    assert requests[1]["messages"][-2]["role"] == "assistant"
    assert result.binding["calls"][0]["validation_errors"]
    assert result.unprocessed_count == 0
    assert [(item.atomic_place_name, item.day_index) for item in result.mentions] == [("青溪公园", 1)]


@pytest.mark.asyncio
async def test_actual_schema_keeps_all_business_fields_bounds_and_requires_name_day_and_day_extent():
    async with recorded_provider(VALID) as (provider, requests):
        await provider.propose(SOURCE)
    schema = request_schema(requests[0])
    activity = schema["$defs"]["SemanticActivity"]
    assert set(schema["properties"]) == {"destination", "day_labels", "activities", "unprocessed_quotes", "choice_groups", "order_groups"}
    assert set(schema["required"]) == {"activities", "day_labels", "unprocessed_quotes", "order_groups"}
    assert set(activity["required"]) == {"source_quote", "role", "place_name", "day_index"}
    assert set(activity["properties"]) == {
        "source_quote", "occurrence", "place_name", "role", "day_index", "category", "meal_role",
        "lodging_event", "lodging_scope", "lodging_evidence", "lodging_excluded_nights", "lodging_exclusion_evidence",
        "city", "city_evidence",
    }
    assert schema["additionalProperties"] is False and activity["additionalProperties"] is False
    assert schema["properties"]["activities"]["maxItems"] == 160
    assert schema["properties"]["day_labels"]["maxItems"] == 14
    choice = schema["$defs"]["SemanticChoiceGroup"]
    branch = schema["$defs"]["SemanticChoiceBranch"]
    assert choice["additionalProperties"] is False and branch["additionalProperties"] is False
    assert choice["properties"]["branches"]["minItems"] == choice["properties"]["branches"]["maxItems"] == 2
    assert choice["properties"]["status"]["const"] == "UNSELECTED"
    assert branch["properties"]["activity_indices"]["items"]["exclusiveMaximum"] == 160
    validator = Draft202012Validator(schema)
    validator.validate(VALID)
    for field in ("day_labels", "unprocessed_quotes", "order_groups"):
        missing = {key: value for key, value in VALID.items() if key != field}
        assert any(error.validator == "required" and not error.path for error in validator.iter_errors(missing))
    for field in ("place_name", "day_index"):
        missing = copy.deepcopy(VALID)
        del missing["activities"][0][field]
        assert any(error.validator == "required" and list(error.path) == ["activities", 0]
                   for error in validator.iter_errors(missing))
    for updates in ({"day_index": 15}, {"lodging_event": "GUESS"}, {"meal_role": "GUESS"},
                    {"start_time": "25:00"}, {"city": "城" * 41}, {"lodging_excluded_nights": [15]}):
        invalid = copy.deepcopy(VALID)
        invalid["activities"][0].update(updates)
        assert list(validator.iter_errors(invalid))


@pytest.mark.asyncio
async def test_explicit_null_meal_and_undated_whole_trip_hotel_are_valid_without_repair():
    source = "北京。全程住星河酒店。Day1：中午午餐待定。Day2：休息。"
    payload = dict(destination="北京", day_labels=[None, None], unprocessed_quotes=[], activities=[
        dict(source_quote="星河酒店", place_name="星河酒店", role="PLANNED", day_index=None,
             category="住宿", lodging_event="OVERNIGHT", lodging_scope="WHOLE_TRIP", lodging_evidence="全程住星河酒店"),
        dict(source_quote="午餐待定", place_name=None, role="PLANNED", day_index=1, category="餐饮", meal_role="LUNCH"),
        dict(source_quote="休息", place_name=None, role="PLANNED", day_index=2),
    ], order_groups=[])
    async with recorded_provider(payload) as (provider, requests):
        result = await provider.propose(source)
    assert len(requests) == 1
    Draft202012Validator(request_schema(requests[0])).validate(payload)
    hotel, meal, rest = result.mentions
    assert hotel.atomic_place_name == "星河酒店" and hotel.lodging_scope == "WHOLE_TRIP"
    assert hotel.lodging_event == "OVERNIGHT" and not hotel.pending_lodging_scope
    assert meal.atomic_place_name is None and meal.meal_role == "LUNCH" and meal.day_index == 1
    assert rest.atomic_place_name is None and rest.day_index == 2
    assert result.unprocessed_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [False, True])
async def test_schema_http_400_does_not_fallback_or_retry(repair):
    failure = (400, {"error": {"message": "private server detail", "type": "invalid_request_error"}})
    outputs = ("{broken", failure) if repair else (failure,)
    async with recorded_provider(*outputs) as (provider, requests):
        with pytest.raises(InferenceProviderUnavailableError) as caught:
            await provider.propose(SOURCE)
    assert len(requests) == caught.value.external_call_count == (2 if repair else 1)
    assert caught.value.category == "PROVIDER_UNAVAILABLE"
    assert "private server detail" not in str(caught.value.provider_binding)
    for request in requests:
        request_schema(request)


def test_strict_wire_does_not_change_legacy_draft_or_saved_plan_defaults():
    old = SemanticDraft.model_validate({"activities": [{"source_quote": "午餐", "role": "PLANNED"}]})
    assert old.activities[0].place_name is None and old.activities[0].day_index is None
    assert old.day_labels == [] and old.unprocessed_quotes == []
    assert old.order_groups == []  # Required only on the active model wire; old records still read.
    restored = SemanticDraft.model_validate_json(old.model_dump_json(exclude_unset=True))
    assert restored == old
    plan = SourceSemanticPlan.model_validate(dict(source_hash="0" * 64, destination_name="北京", binding={},
        mentions=[dict(mention_id="activity-1", raw_text="午餐", span_start=0, span_end=2,
                       role="PLANNED", sequence_index=0)]))
    assert plan.mentions[0].atomic_place_name is None and plan.mentions[0].day_index is None
    assert plan.day_labels == {} and plan.unprocessed_count == 0
    assert plan.order_assessment.unassessed_mention_ids == ()


@pytest.mark.asyncio
async def test_schema_does_not_make_fabricated_source_name_a_success_after_two_answers():
    invalid = copy.deepcopy(VALID)
    invalid["activities"][0]["place_name"] = "原文没有的景点"
    async with recorded_provider(invalid, invalid) as (provider, requests):
        with pytest.raises(InferenceProviderUnavailableError) as caught:
            await provider.propose(SOURCE)
    assert len(requests) == 2
    assert caught.value.category == "PLACE_NOT_IN_SOURCE_QUOTE"
    assert caught.value.provider_binding["outcome"] != "SUCCESS"
