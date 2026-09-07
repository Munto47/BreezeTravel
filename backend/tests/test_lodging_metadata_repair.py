"""An optional hotel metadata repair cannot rewrite the extracted itinerary."""
import asyncio
import json

import httpx
import pytest
from openai import APIConnectionError

from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.repository import _persisted_proposal
from tests.test_experience_inference import Client, provider
from tests.test_semantic_partial_recovery import activity


SOURCE = "北京三日游。Day1：上午从星河酒店退房，然后到月光桥。Day2：晨光湖。Day3：晚霞公园。"
DRAFT = {"destination": "北京", "activities": [
    activity("星河酒店", category="住宿", lodging_event="CHECK_OUT"), activity("月光桥"),
    activity("晨光湖", 2), activity("晚霞公园", 3)]}
PATCH = {"index": 0, "lodging_event": "CHECK_OUT", "lodging_evidence": "上午从星河酒店退房"}


@pytest.mark.asyncio
async def test_one_short_hotel_repair_keeps_every_activity_and_projects_verified_event():
    client = Client(json.dumps(DRAFT), json.dumps({"activities": [PATCH]}))
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(SOURCE)
    assert [(item.atomic_place_name, item.day_index, item.role.value) for item in output.proposal.mentions] == [
        ("星河酒店", 1, "PLANNED"), ("月光桥", 1, "PLANNED"), ("晨光湖", 2, "PLANNED"), ("晚霞公园", 3, "PLANNED")]
    hotel = output.proposal.mentions[0]
    assert hotel.lodging_event == "CHECK_OUT" and not hotel.lodging_role_uncertain
    assert output.public_result.days[0].activities[0].lodging_event == "CHECK_OUT"
    assert output.proposal.unprocessed_count == 0
    assert len(client.calls) == output.inference_binding["external_calls"] == 2
    assert "Day2" not in client.calls[1]["messages"][1]["content"]
    assert "JSON Schema" not in client.calls[1]["messages"][0]["content"]
    assert output.inference_binding["input_tokens"] == 400
    assert output.inference_binding["calls"][1]["stage"] == "LODGING_METADATA_REPAIR"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"activities": [dict(PATCH, lodging_evidence="星河酒店")]},
    {"activities": [dict(PATCH, lodging_evidence="随后回星河酒店取行李")]},
    {"activities": [dict(PATCH, index=1)]},
    {"activities": [PATCH, PATCH]},
    {"activities": [dict(PATCH, day_index=3)]},
    {"activities": [dict(PATCH, lodging_event=None)]},
    {"activities": []},
    "{invalid-json",
])
async def test_bad_metadata_stays_uncertain_without_losing_source_hotel(response):
    payload = response if isinstance(response, str) else json.dumps(response)
    client = Client(json.dumps(DRAFT), payload)
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(SOURCE)
    hotel = output.proposal.mentions[0]
    card = output.public_result.days[0].activities[0]
    assert hotel.atomic_place_name == card.name == "星河酒店"
    assert hotel.lodging_event is card.lodging_event is None
    assert hotel.lodging_role_uncertain and card.lodging_role_uncertain
    assert output.proposal.unprocessed_count == 1 and not output.public_result.coverage.complete
    assert output.public_result.coverage.recognized_place_count == 4
    assert output.public_result.coverage.unclassified_mention_count == 0
    assert _persisted_proposal(output)["structure"][0]["lodging_role_uncertain"] is True
    retained = _persisted_proposal(output)
    assert all("lodging_evidence" not in item for item in retained["structure"])
    assert "上午从星河酒店退房" not in json.dumps(retained, ensure_ascii=False)
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_metadata_cannot_borrow_a_later_occurrence_of_the_same_hotel():
    source = "Day1：上午从星河酒店退房。Day2：下午回星河酒店取行李。"
    draft = {"activities": [activity("星河酒店", category="住宿", lodging_event="CHECK_OUT"),
        activity("星河酒店", 2, occurrence=2, category="住宿", lodging_event="LUGGAGE_PICKUP",
            lodging_evidence="下午回星河酒店取行李")]}
    client = Client(json.dumps(draft), json.dumps({"activities": [dict(PATCH, lodging_event="LUGGAGE_PICKUP",
        lodging_evidence="下午回星河酒店取行李")]}))
    result = await provider(client).propose(source)
    assert [(item.lodging_event, item.lodging_role_uncertain) for item in result.mentions] == [(None, True), ("LUGGAGE_PICKUP", False)]
    assert result.unprocessed_count == 1


@pytest.mark.asyncio
async def test_only_valid_hotel_patch_is_kept_from_a_partially_valid_response():
    source = "Day1：上午从星河酒店退房，今晚入住月光酒店。"
    draft = {"activities": [activity("星河酒店", category="住宿", lodging_event="CHECK_OUT"),
        activity("月光酒店", category="住宿", lodging_event="OVERNIGHT", lodging_scope="DAY")]}
    client = Client(json.dumps(draft), json.dumps({"activities": [PATCH,
        {"index": 1, "lodging_event": "OVERNIGHT", "lodging_scope": "DAY", "lodging_evidence": "月光酒店"}]}))
    result = await provider(client).propose(source)
    assert [(item.lodging_event, item.lodging_role_uncertain) for item in result.mentions] == [("CHECK_OUT", False), (None, True)]
    assert result.unprocessed_count == 1 and result.binding["calls"][1]["repaired_activity_count"] == 1


@pytest.mark.asyncio
async def test_regular_semantic_repair_already_uses_the_second_call_allowance():
    client = Client("{broken-json", json.dumps(DRAFT))
    result = await provider(client).propose(SOURCE)
    assert len(client.calls) == 2
    assert result.mentions[0].lodging_role_uncertain and result.unprocessed_count == 1
    assert not any(call.get("stage") == "LODGING_METADATA_REPAIR" for call in result.binding["calls"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "timeout"])
async def test_failed_hotel_repair_retains_the_first_result_and_never_retries(failure):
    class FailingClient(Client):
        async def create(self, **kwargs):
            if not self.calls:
                return await super().create(**kwargs)
            self.calls.append(kwargs)
            if failure == "network":
                raise APIConnectionError(request=httpx.Request("POST", "https://example.test/v1"))
            await asyncio.sleep(1)

    client = FailingClient(json.dumps(DRAFT))
    model = provider(client)
    model.deadline_seconds = 0.025
    result = await model.propose(SOURCE)
    assert len(client.calls) == 2 and len(result.mentions) == 4
    assert result.mentions[0].lodging_role_uncertain and result.unprocessed_count == 1
    assert result.binding["outcome"] == "PARTIAL_RESULT"
    assert result.binding["calls"][-1]["outcome"] == ("PROVIDER_UNAVAILABLE" if failure == "network" else "DEADLINE_EXCEEDED")
    assert result.binding["input_tokens"] is None and result.binding["estimated_cost_cny"] is None


@pytest.mark.asyncio
async def test_new_hotel_with_no_model_event_cannot_fall_back_to_legacy_overnight_assumption():
    draft = {"activities": [activity("星河酒店", category="住宿")]}
    client = Client(json.dumps(draft), json.dumps({"activities": []}))
    result = await provider(client).propose("Day1：星河酒店。")
    assert len(client.calls) == 2
    assert result.mentions[0].lodging_event is None and result.mentions[0].lodging_role_uncertain
    assert result.unprocessed_count == 1


@pytest.mark.asyncio
async def test_missing_model_event_gets_source_bound_checkout_metadata_without_moving_the_hotel():
    draft = {**DRAFT, "activities": [dict(DRAFT["activities"][0], lodging_event=None), *DRAFT["activities"][1:]]}
    client = Client(json.dumps(draft), json.dumps({"activities": [PATCH]}))
    result = await provider(client).propose(SOURCE)
    assert result.mentions[0].lodging_event == "CHECK_OUT" and not result.mentions[0].lodging_role_uncertain
    assert result.mentions[0].day_index == 1 and result.unprocessed_count == 0


@pytest.mark.asyncio
async def test_trip_header_hotel_with_missing_day_event_and_scope_gets_one_bounded_scope_repair():
    source = "全程两晚已订星河酒店。\nDay1：月光桥。\nDay2：晨光湖。\nDay3：晚霞公园。"
    draft = {"activities": [activity("星河酒店", day=None, category="住宿"),
        activity("月光桥"), activity("晨光湖", 2), activity("晚霞公园", 3)]}
    patch = {"index": 0, "lodging_event": "OVERNIGHT", "lodging_scope": "WHOLE_TRIP",
        "lodging_evidence": "全程两晚已订星河酒店"}
    client = Client(json.dumps(draft), json.dumps({"activities": [patch]}))
    result = await provider(client).propose(source)
    assert len(client.calls) == 2
    assert [item.atomic_place_name for item in result.mentions] == ["星河酒店", "月光桥", "晨光湖", "晚霞公园"]
    assert result.mentions[0].lodging_scope == "WHOLE_TRIP" and not result.mentions[0].lodging_role_uncertain
    assert result.unprocessed_count == 0 and result.binding["outcome"] == "SUCCESS"


@pytest.mark.asyncio
async def test_failed_undated_hotel_scope_does_not_fabricate_a_first_day_visit_or_extra_call():
    source = "星河酒店，哪晚住尚未确定。\nDay1：月光桥。\nDay2：晨光湖。"
    draft = {"activities": [activity("星河酒店", day=None, category="住宿"), activity("月光桥"), activity("晨光湖", 2)]}
    client = Client(json.dumps(draft), json.dumps({"activities": []}))
    result = await provider(client).propose(source)
    assert len(client.calls) == 2
    assert result.unprocessed_count > 0 and result.binding["outcome"] == "PARTIAL_RESULT"
    assert [item.atomic_place_name for item in result.mentions] == ["月光桥", "晨光湖"]
