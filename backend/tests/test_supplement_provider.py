"""Supplement transport uses the existing provider and reserves before HTTP."""
import json
from types import SimpleNamespace

import pytest

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.inference_allowance import inference_allowance, InferenceAllowanceExceeded
from app.trip_understanding.supplement_provider import BoundedSupplementProvider
from tests.test_bounded_supplement import SOURCE, fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["valid", "malformed", "truncated", "denied"])
async def test_shared_configuration_usage_and_dispatch_reservation(response_kind):
    plan, result = await fixture()
    order = []
    async def reserve():
        order.append("reserve")
        if response_kind == "denied":
            raise InferenceAllowanceExceeded("MODEL_CALL_BUDGET_EXHAUSTED")
    async def create(**request):
        order.append("http")
        assert request["model"] == "kimi-for-coding" and request["max_tokens"] == 2345
        context = json.loads(request["messages"][1]["content"])
        assert context["source"] == SOURCE and context["current"]["days"] == result.model_dump(mode="json")["days"]
        assert set(context) == {"source", "current", "already_extracted"}
        schema = request["response_format"]["json_schema"]["schema"]
        def refs(item):
            if isinstance(item,dict):
                if "$ref" in item:
                    assert item["$ref"].startswith("#/$defs/") and item["$ref"].split("/")[-1] in schema["$defs"]
                for value in item.values():
                    refs(value)
            elif isinstance(item,list):
                for value in item:
                    refs(value)
        refs(schema)
        return SimpleNamespace(model="same-worker-model", usage=SimpleNamespace(prompt_tokens=123,completion_tokens=45),
            choices=[SimpleNamespace(finish_reason="length" if response_kind=="truncated" else "stop",
                message=SimpleNamespace(content='{"operations":[]}' if response_kind!="malformed" else '{'))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    configured = ExperienceQwenProvider(api_key="controlled", base_url="https://controlled.invalid", model="kimi-for-coding", client=client, max_output_tokens=2345)
    provider = BoundedSupplementProvider(configured)
    work = SimpleNamespace(source=SOURCE,plan=plan,result=result)
    with inference_allowance(reserve):
        if response_kind=="denied":
            with pytest.raises(InferenceAllowanceExceeded):
                await provider.propose(work)
            assert order==["reserve"]
        elif response_kind!="valid":
            with pytest.raises(InferenceProviderUnavailableError) as error:
                await provider.propose(work)
            assert error.value.provider_binding["input_tokens"]==123
            assert error.value.provider_binding["external_calls"]==1
            assert order==["reserve","http"]
        else:
            operations, usage = await provider.propose(work)
            assert operations==[] and usage["input_tokens"]==123 and usage["output_tokens"]==45
            assert order==["reserve","http"]
