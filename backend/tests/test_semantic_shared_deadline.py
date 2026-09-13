"""A whole-trip deadline must not cancel already understood day fragments."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.pipeline import TripUnderstandingPipeline


NAMES = ["星河公园", "月光桥", "青溪寺"]


class DeadlineClient:
    def __init__(self, *, days=2, whole=False, hang_initial=False, cleanup_seconds=0):
        self.days, self.whole, self.hang_initial = days, whole, hang_initial
        self.cleanup_seconds = cleanup_seconds
        self.calls = []
        self.cancelled_calls = []
        self.patching = asyncio.Event()
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        call_number = len(self.calls)
        prompt = kwargs["messages"][0]["content"]
        if prompt.startswith("只分析旅行原文的全局结构"):
            await asyncio.sleep(0.025)
            payload = {"cross_day_dependencies": self.whole, "sections": [
                {"day_index": day, "start_quote": f"Day{day}"} for day in range(1, self.days + 1)]}
        elif prompt.startswith("只修复已提取活动的城市字段") or self.hang_initial:
            self.patching.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled_calls.append(call_number)
                if self.cleanup_seconds:
                    await asyncio.sleep(self.cleanup_seconds)
        else:
            selected = [(day, name) for day, name in enumerate(NAMES[:self.days], 1)
                        if self.whole or f"本次只整理原文第{day}天" in prompt]
            payload = {"destination": "北京", "activities": [
                {"source_quote": name, "place_name": name, "day_index": day,
                 "role": "PLANNED", "category": "景点", "city": "北京", "city_evidence": "旅行背景"}
                for day, name in selected]}
        return SimpleNamespace(model="controlled", usage=None, choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))])


def engine(client, *, deadline=0.25):
    return ExperienceQwenProvider(api_key="test", base_url="https://test.invalid", model="test",
        client=client, deadline_seconds=deadline)


def source(days=2):
    return "北京旅行背景：" + "准备充分。" * 190 + "\n" + "\n".join(
        f"Day{day}：{name}。" for day, name in enumerate(NAMES[:days], 1))


class NoPlaces:
    async def resolve(self, *_args, **_kwargs):
        assert _kwargs["city"] == "目的地待确认"
        return None  # The real adapter rejects this sentinel before HTTP.


@pytest.mark.asyncio
@pytest.mark.parametrize("whole", [False, True])
async def test_shared_deadline_keeps_first_answers_through_public_projection(whole):
    client = DeadlineClient(whole=whole)
    started = time.perf_counter()
    result = await TripUnderstandingPipeline(engine(client), NoPlaces()).run(source())
    assert time.perf_counter() - started < 0.8
    assert [(m.atomic_place_name, m.day_index) for m in result.proposal.mentions] == [
        ("星河公园", 1), ("月光桥", 2)]
    assert [[card.name for card in day.activities] for day in result.public_result.days] == [["星河公园"], ["月光桥"]]
    assert result.public_result.coverage.complete is False
    assert result.proposal.unprocessed_count == 2
    # Whole-document city failures retain the existing global count; only
    # independently processed day scopes assign these failures to a day.
    assert result.proposal.unprocessed_by_day == ({} if whole else {1: 1, 2: 1})
    assert all(card.status != "READY" for day in result.public_result.days for card in day.activities)
    assert result.inference_binding["external_calls"] == len(client.calls) == (3 if whole else 5)
    assert len(client.cancelled_calls) == (1 if whole else 2)


@pytest.mark.asyncio
async def test_queued_day_does_not_start_a_request_after_shared_deadline():
    # Include bounded transport cleanup so the waiting third day cannot win a
    # sub-clock-resolution interval just before Windows' timer deadline.
    client = DeadlineClient(days=3, cleanup_seconds=0.03)
    result = await engine(client).propose(source(3))
    assert [(m.atomic_place_name, m.day_index) for m in result.mentions] == [("星河公园", 1), ("月光桥", 2)]
    assert result.day_count == 3
    assert result.unprocessed_by_day == {1: 1, 2: 1, 3: 1}
    assert any(issue.category == "DAY_SECTION_UNPROCESSED" and issue.field == "days[3]" for issue in result.diagnostics)
    assert len(client.calls) == 5
    assert not any("本次只整理原文第3天" in call["messages"][0]["content"] for call in client.calls)
    assert result.binding["external_calls"] == 5


@pytest.mark.asyncio
async def test_expired_day_budget_does_not_start_even_its_initial_request():
    client = DeadlineClient()
    with pytest.raises(InferenceProviderUnavailableError) as failed:
        await engine(client)._propose(source(), deadline_at=time.perf_counter() - 1)
    assert failed.value.category == "DEADLINE_EXCEEDED"
    assert client.calls == []
    assert failed.value.external_call_count == 0


@pytest.mark.asyncio
async def test_explicit_user_cancellation_still_propagates_to_all_pending_calls():
    client = DeadlineClient()
    task = asyncio.create_task(engine(client, deadline=10).propose(source()))
    await asyncio.wait_for(client.patching.wait(), timeout=1)
    await asyncio.sleep(0)  # Both permitted day workers reach their pending patch.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(client.cancelled_calls) == 2
    assert len(client.calls) == 5


@pytest.mark.asyncio
async def test_no_successful_initial_answer_stays_an_explicit_unavailable_result():
    client = DeadlineClient(hang_initial=True)
    with pytest.raises(InferenceProviderUnavailableError) as failed:
        await engine(client).propose(source())
    assert failed.value.category == "DAY_SCOPES_UNAVAILABLE"
    assert len(client.calls) == 3
    assert len(client.cancelled_calls) == 2
    assert failed.value.external_call_count == 3
