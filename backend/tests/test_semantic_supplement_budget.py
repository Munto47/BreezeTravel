"""The live second-answer contract preserves first-answer work and its budget."""
import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
from openai import APIConnectionError
import pytest

from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_city_evidence_recovery import FixedCities


SOURCE = "北京一日游。\nDay1：参观故宫博物院，馆内先看太和殿，午门进、神武门出。"
EVIDENCE = "参观故宫博物院，馆内先看太和殿，午门进、神武门出"
FIRST = {"destination": "北京", "day_labels": [None], "activities": [{
    "source_quote": "故宫博物院", "place_name": "故宫博物院", "role": "PLANNED",
    "category": "景点", "day_index": 1, "city": "北京", "city_evidence": "北京一日游",
}]}
DETAILS = {"city_fields": [], "source_visits": [
    {"parent_index": 0, "kind": kind, "source_quote": name, "occurrence": 1,
     "optional": False, "evidence": EVIDENCE}
    for kind, name in [("ENTRY", "午门"), ("EXIT", "神武门"), ("VISIT", "太和殿")]
]}


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        content, finish = value if isinstance(value, tuple) else (value, "stop")
        return SimpleNamespace(model="controlled", usage=None, choices=[SimpleNamespace(
            finish_reason=finish, message=SimpleNamespace(content=content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)))])


def provider(client, *, deadline=1):
    return ExperienceQwenProvider(api_key="test", base_url="https://test.invalid", model="controlled",
        client=client, deadline_seconds=deadline, enable_source_visits=True)


@pytest.mark.asyncio
async def test_source_visits_use_second_answer_and_reach_public_parent_in_user_order():
    client = Client(FIRST, DETAILS)
    output = await TripUnderstandingPipeline(provider(client), FixedCities()).run(SOURCE)
    assert len(client.calls) == output.inference_binding["external_calls"] == 2
    assert output.inference_binding["calls"][1]["stage"] == "SOURCE_VISITS_SUPPLEMENT"
    assert output.inference_binding["source_visit_supplement_enabled"] is True
    assert [card.name for card in output.public_result.days[0].activities] == ["故宫博物院"]
    assert [item.name for item in output.public_result.days[0].activities[0].source_details] == [
        "入口：午门", "太和殿", "出口：神武门"]
    assert output.resolution_receipt["attempted_count"] == 1
    assert not any(item.category == "SOURCE_VISITS_UNPROCESSED" for item in output.proposal.diagnostics)


@pytest.mark.asyncio
async def test_city_and_details_share_one_second_answer_without_rewriting_mainline():
    first = copy.deepcopy(FIRST)
    first["activities"][0]["city_evidence"] = "皇城核心"
    patch = copy.deepcopy(DETAILS)
    patch["city_fields"] = [{"index": 0, "city": None, "city_evidence": None}]
    client = Client(first, patch)
    output = await TripUnderstandingPipeline(provider(client), FixedCities()).run(SOURCE)
    assert len(client.calls) == 2
    assert output.inference_binding["calls"][1]["accepted_city_fields"] == 1
    assert not any(issue.category == "UNSUPPORTED_CITY_REMOVED" for issue in output.proposal.diagnostics)
    card = output.public_result.days[0].activities[0]
    assert card.name == "故宫博物院" and card.status == "READY"
    assert [item.name for item in card.source_details] == ["入口：午门", "太和殿", "出口：神武门"]


@pytest.mark.asyncio
@pytest.mark.parametrize("second", [
    "{broken", (DETAILS, "length"), {"city_fields": [], "source_visits": [], "activities": []},
    APIConnectionError(message="private-provider-message", request=httpx.Request("POST", "https://test.invalid")),
])
async def test_failed_supplement_preserves_first_mainline_and_reports_unfinished(second):
    client = Client(FIRST, second)
    result = await provider(client).propose(SOURCE)
    assert len(client.calls) == 2
    assert [m.atomic_place_name for m in result.mentions] == ["故宫博物院"]
    assert any(issue.category == "SOURCE_VISITS_UNPROCESSED" for issue in result.diagnostics)
    assert result.binding["outcome"] == "PARTIAL_RESULT"
    assert "private-provider-message" not in json.dumps(result.binding)


@pytest.mark.asyncio
async def test_regular_second_answer_leaves_no_third_supplement_allowance():
    client = Client("{broken", FIRST)
    result = await provider(client).propose(SOURCE)
    assert len(client.calls) == 2
    assert not any(call.get("stage") == "SOURCE_VISITS_SUPPLEMENT" for call in result.binding["calls"])
    assert [m.atomic_place_name for m in result.mentions] == ["故宫博物院"]
    assert any(issue.category == "SOURCE_VISITS_UNPROCESSED" for issue in result.diagnostics)


@pytest.mark.asyncio
async def test_empty_second_answer_cannot_claim_an_unknown_internal_visit_is_complete():
    source = SOURCE.replace("太和殿", "赤霞新展厅")
    client = Client(FIRST, {"city_fields": [], "source_visits": []})
    result = await TripUnderstandingPipeline(provider(client), FixedCities()).run(source)
    assert len(client.calls) == 2
    assert [card.name for card in result.public_result.days[0].activities] == ["故宫博物院"]
    assert result.public_result.days[0].activities[0].source_details == []
    assert any(issue.category == "SOURCE_VISITS_UNPROCESSED" for issue in result.proposal.diagnostics)
    assert not result.public_result.coverage.complete


class WaitingClient(Client):
    def __init__(self):
        super().__init__(FIRST)
        self.started = asyncio.Event()
        self.cancelled = False

    async def create(self, **kwargs):
        if not self.calls:
            return await super().create(**kwargs)
        self.calls.append(kwargs)
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True


@pytest.mark.asyncio
async def test_original_deadline_returns_first_answer_and_keeps_unfinished_marker():
    client = WaitingClient()
    result = await asyncio.wait_for(provider(client, deadline=0.1).propose(SOURCE), timeout=1)
    assert client.cancelled and len(client.calls) == 2
    assert [m.atomic_place_name for m in result.mentions] == ["故宫博物院"]
    assert result.binding["calls"][1]["outcome"] == "DEADLINE_EXCEEDED"
    assert any(issue.category == "SOURCE_VISITS_UNPROCESSED" for issue in result.diagnostics)


@pytest.mark.asyncio
async def test_user_stop_cancels_the_request_instead_of_returning_success():
    client = WaitingClient()
    task = asyncio.create_task(provider(client).propose(SOURCE))
    await asyncio.wait_for(client.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.cancelled and len(client.calls) == 2


@pytest.mark.asyncio
async def test_total_capacity_failure_keeps_both_actual_calls_and_does_not_restore_smaller_first_answer(monkeypatch):
    from app.trip_understanding.errors import InferenceProviderUnavailableError
    from app.trip_understanding.failures import INPUT_CAPACITY_EXCEEDED

    # Lower only the pure adapter's bound to exercise the real overflow path
    # using one original visit and its three valid supplements.
    monkeypatch.setattr("app.trip_understanding.source_visit_supplement.MAX_TRIP_ACTIVITIES", 3)
    client = Client(FIRST, DETAILS)
    with pytest.raises(InferenceProviderUnavailableError) as caught:
        await provider(client).propose(SOURCE)
    error = caught.value
    assert str(error) == INPUT_CAPACITY_EXCEEDED
    assert len(client.calls) == error.external_call_count == error.provider_binding["external_calls"] == 2
    assert error.provider_binding["outcome"] == INPUT_CAPACITY_EXCEEDED
    assert error.provider_binding["calls"][1]["stage"] == "SOURCE_VISITS_SUPPLEMENT"
    assert error.provider_binding["calls"][1]["outcome"] == INPUT_CAPACITY_EXCEEDED


@pytest.mark.asyncio
async def test_successful_undated_hotel_repair_keeps_unprocessed_gate_requirements():
    from tests.test_semantic_partial_recovery import activity

    source = ("北京三日游。全程两晚已订星河酒店。\n"
              "Day1：星河公园，南门进、北门出。\nDay2：晨光湖。\nDay3：晚霞公园。")
    first = {"destination": "北京", "activities": [
        activity("星河酒店", day=None, category="住宿"), activity("星河公园"),
        activity("晨光湖", 2), activity("晚霞公园", 3),
    ]}
    patch = {"activities": [{"index": 0, "lodging_event": "OVERNIGHT", "lodging_scope": "WHOLE_TRIP",
                              "lodging_evidence": "全程两晚已订星河酒店"}]}
    client = Client(first, patch)
    output = await TripUnderstandingPipeline(provider(client), FixedCities()).run(source)

    assert len(client.calls) == output.inference_binding["external_calls"] == 2
    assert output.inference_binding["calls"][1]["stage"] == "LODGING_METADATA_REPAIR"
    hotel = output.proposal.mentions[0]
    assert hotel.lodging_scope == "WHOLE_TRIP" and not hotel.lodging_role_uncertain
    assert [mention.atomic_place_name for mention in output.proposal.mentions] == [
        "星河酒店", "星河公园", "晨光湖", "晚霞公园"]
    park = next(card for card in output.public_result.days[0].activities if card.name == "星河公园")
    assert park.status == "READY" and park.source_details == []
    assert any(issue.category == "SOURCE_VISITS_UNPROCESSED" for issue in output.proposal.diagnostics)
    assert output.inference_binding["outcome"] == "PARTIAL_RESULT"
    assert not output.public_result.coverage.complete


@pytest.mark.asyncio
async def test_day_scopes_keep_same_named_parent_ids_and_distinct_children_in_public_result():
    prefix = "北京两日游。\n" + "本次仅记录本日安排，不替换实际访问。" * 50 + "\n"
    source = prefix + "Day1：参观星河公园，园内先看松影亭。\nDay2：参观星河公园，园内先看映竹亭。"
    assert len(source) >= 900  # Exercise the actual day-scope entry condition.
    structure = {"cross_day_dependencies": False, "sections": [
        {"day_index": 1, "start_quote": "Day1"}, {"day_index": 2, "start_quote": "Day2"},
    ]}

    def first(day):
        return {"destination": "北京", "day_labels": [None] * day, "activities": [{
            "source_quote": "星河公园", "place_name": "星河公园", "role": "PLANNED",
            "category": "景点", "day_index": day, "city": "北京", "city_evidence": "北京两日游",
        }]}

    def detail(name):
        return {"city_fields": [], "source_visits": [{"parent_index": 0, "kind": "VISIT",
            "source_quote": name, "evidence": f"园内先看{name}", "occurrence": 1, "optional": False}]}

    client = Client(structure, first(1), detail("松影亭"), first(2), detail("映竹亭"))
    output = await TripUnderstandingPipeline(provider(client), FixedCities()).run(source)
    assert len(client.calls) == output.inference_binding["external_calls"] == 5
    assert output.inference_binding["day_scopes_completed"] == 2
    assert sum(call.get("stage") == "SOURCE_VISITS_SUPPLEMENT" for call in output.inference_binding["calls"]) == 2
    assert [[card.name for card in day.activities] for day in output.public_result.days] == [
        ["星河公园"], ["星河公园"]]
    assert [[detail.name for detail in day.activities[0].source_details] for day in output.public_result.days] == [
        ["松影亭"], ["映竹亭"]]
    parents = {mention.day_index: mention for mention in output.proposal.mentions if not mention.parent_mention_id}
    children = [mention for mention in output.proposal.mentions if mention.parent_mention_id]
    assert len(children) == 2 and parents[1].mention_id != parents[2].mention_id
    for child in children:
        assert child.parent_mention_id == parents[child.day_index].mention_id
        assert source[child.span_start:child.span_end] == child.atomic_place_name
        assert source[child.role_evidence_start:child.role_evidence_end] == child.role_evidence
    assert output.public_result.coverage.complete
