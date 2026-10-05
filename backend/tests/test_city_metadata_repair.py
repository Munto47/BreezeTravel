"""City-only second answers preserve the original itinerary; no real calls."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
from openai import APIConnectionError
import pytest

from app.trip_understanding.city_metadata import repair_city_metadata
from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_city_evidence_recovery import FixedCities, row
from tests.test_experience_source_anchors import provider


SOURCE = "北京一日游。\nDay1：故宫博物院，景山公园。"
# The rejected city conflicts with the explicit source city. This isolates
# patch authorization from independently valid single-city source inference.
FIRST = {"destination": "北京", "activities": [row("故宫博物院", city="上海", evidence="上海城市核心"), row("景山公园", evidence="北京一日游")]}
PATCH = {"index": 0, "city": "北京", "city_evidence": "北京一日游"}
PRIVATE = "private-city-value-must-never-enter-diagnostics"


class RawClient:
    """Return exact JSON/string and finish reason; never transform whole drafts."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        payload, finish = response if isinstance(response, tuple) else (response, "stop")
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
            model="controlled-model", usage=SimpleNamespace(prompt_tokens=200, completion_tokens=100))


async def run_patch(response, *, source=SOURCE, first=None):
    client = RawClient(first or FIRST, response)
    places = FixedCities()
    output = await TripUnderstandingPipeline(provider(client), places).run(source)
    assert len(client.calls) == 2
    assert output.inference_binding["external_calls"] == 2
    assert output.inference_binding["repair_call_count"] == 1
    assert output.inference_binding["calls"][1]["stage"] == "CITY_METADATA_REPAIR"
    return output, places, client


def city_warnings(proposal):
    return [issue for issue in proposal.diagnostics if issue.category == "UNSUPPORTED_CITY_REMOVED"]


@pytest.mark.asyncio
async def test_explicit_city_patch_repairs_original_slot_without_resending_a_whole_itinerary():
    output, places, client = await run_patch({"activities": [PATCH]})
    assert places.calls == [("北京", "故宫博物院"), ("北京", "景山公园")]
    assert [card.name for card in output.public_result.days[0].activities] == ["故宫博物院", "景山公园"]
    assert not city_warnings(output.proposal)
    assert output.public_result.coverage.complete
    assert output.proposal.mentions[0].city_evidence == "北京一日游"
    assert client.calls[1]["max_tokens"] <= client.calls[0]["max_tokens"]


@pytest.mark.asyncio
async def test_explicit_null_pair_removes_only_invalid_evidence_and_uses_existing_soft_city():
    output, places, _client = await run_patch({"activities": [{"index": 0, "city": None, "city_evidence": None}]})
    assert places.calls == [("北京", "故宫博物院"), ("北京", "景山公园")]
    assert not city_warnings(output.proposal)
    assert output.proposal.mentions[0].city_evidence is None
    assert output.public_result.coverage.complete


@pytest.mark.asyncio
async def test_independent_source_city_can_resolve_places_without_accepting_invalid_patch():
    first = {"destination": "北京", "activities": [row("故宫博物院"), row("景山公园", evidence="北京一日游")]}
    output, places, _client = await run_patch({"activities": [{"index": 0, "city": "北京"}]}, first=first)
    assert places.calls == [("北京", "故宫博物院"), ("北京", "景山公园")]
    assert output.proposal.mentions[0].city_hint is None
    assert output.proposal.mentions[0].city_evidence == "城市核心"
    assert len(city_warnings(output.proposal)) == 1
    assert not output.public_result.coverage.complete


@pytest.mark.asyncio
async def test_valid_city_index_is_ignored_while_invalid_index_can_be_repaired():
    output, places, _client = await run_patch({"activities": [
        {"index": 1, "city": "上海", "city_evidence": "北京一日游"}, PATCH]})
    assert places.calls == [("北京", "故宫博物院"), ("北京", "景山公园")]
    assert output.proposal.mentions[1].city_evidence == "北京一日游"
    assert not city_warnings(output.proposal)


@pytest.mark.asyncio
async def test_repair_accepts_good_patch_and_keeps_each_other_original_warning():
    first = {"destination": "北京", "activities": [row(name, city="上海", evidence="上海城市核心") for name in ("故宫博物院", "景山公园")]}
    output, places, _client = await run_patch({"activities": [PATCH,
        {"index": 1, "city": "上海", "city_evidence": "北京一日游"}]}, first=first)
    assert places.calls == [("北京", "故宫博物院")]
    assert [card.name for card in output.public_result.days[0].activities] == ["故宫博物院", "景山公园"]
    assert [item.city_hint for item in output.proposal.mentions] == ["北京", None]
    assert len(city_warnings(output.proposal)) == 1
    assert output.public_result.coverage.unresolved_place_count == 1
    assert output.public_result.coverage.complete is False


@pytest.mark.asyncio
async def test_duplicate_index_does_not_authorize_last_write_but_unique_patch_still_survives():
    first = {"destination": "北京", "activities": [row(name, city="上海", evidence="上海城市核心") for name in ("故宫博物院", "景山公园")]}
    output, places, _client = await run_patch({"activities": [PATCH, dict(PATCH, city=None, city_evidence=None),
        {"index": 1, "city": "北京", "city_evidence": "北京一日游"}]}, first=first)
    assert places.calls == [("北京", "景山公园")]
    assert [item.city_hint for item in output.proposal.mentions] == [None, "北京"]
    assert len(city_warnings(output.proposal)) == 1


@pytest.mark.asyncio
async def test_original_index_keeps_same_name_revisits_separate():
    source = "Day1 北京：星河公园。\nDay2 上海：再次游览星河公园。"
    first = {"destination": "北京", "activities": [
        row("星河公园", evidence="Day1 北京"),
        row("星河公园", 2, "上海", occurrence=2),
    ]}
    output, places, _client = await run_patch({"activities": [{"index": 1, "city": "上海", "city_evidence": "Day2 上海"}]},
        source=source, first=first)
    assert places.calls == [("北京", "星河公园"), ("上海", "星河公园")]
    assert [(m.atomic_place_name, m.day_index, m.city_hint) for m in output.proposal.mentions] == [
        ("星河公园", 1, "北京"), ("星河公园", 2, "上海")]
    assert output.proposal.mentions[0].span_start != output.proposal.mentions[1].span_start


@pytest.mark.asyncio
async def test_patch_index_addresses_current_expanded_draft_and_never_shifts_to_a_valid_child():
    source = "北京一日游。\nDay1：故宫博物院 + 景山公园，之后北海公园。"
    first = {"destination": "北京", "activities": [
        row("故宫博物院 + 景山公园", evidence="北京一日游"), row("北海公园")]}
    output, places, _client = await run_patch({"activities": [
        {"index": 1, "city": "上海", "city_evidence": "北京一日游"},
        {"index": 2, "city": "北京", "city_evidence": "北京一日游"},
    ]}, source=source, first=first)
    assert places.calls == [("北京", "故宫博物院"), ("北京", "景山公园"), ("北京", "北海公园")]
    assert [m.city_evidence for m in output.proposal.mentions] == ["北京一日游"] * 3
    assert not city_warnings(output.proposal)


@pytest.mark.asyncio
async def test_repair_returns_a_new_draft_with_all_non_city_fields_unchanged():
    source = "北京两日游。\nDay1：09:00到星河公园。备选月光桥。晚上入住云岭酒店。\nDay2：晚霞公园。"
    first = {"destination": "北京", "day_labels": ["Day1", "Day2"], "activities": [
        row("星河公园"),
        {**row("月光桥"), "role": "OPTIONAL"},
        {**row("云岭酒店"), "category": "住宿", "lodging_event": "OVERNIGHT", "lodging_scope": "DAY",
         "lodging_evidence": "晚上入住云岭酒店"},
        row("晚霞公园", 2),
    ]}
    first["activities"][0].update(source_quote="09:00到星河公园", start_time="09:00",
        timing_source="TEXT", time_evidence="09:00到星河公园")
    draft = SemanticDraft.model_validate(first)
    before = draft.model_dump(mode="json")
    partial = _proposal_from_live_draft(source, draft, allow_partial=True)
    response = {"activities": [{"index": index, "city": "北京", "city_evidence": "北京两日游"} for index in range(4)]}
    calls = [{"attempt": 1}]
    repaired, proposal = await repair_city_metadata(provider(RawClient(response)), source, draft, partial, calls)
    assert draft.model_dump(mode="json") == before
    assert not city_warnings(proposal)
    assert len(calls) == 2
    protected_before = {**before, "activities": [{k: v for k, v in item.items() if k not in {"city", "city_evidence"}}
        for item in before["activities"]]}
    after = repaired.model_dump(mode="json")
    protected_after = {**after, "activities": [{k: v for k, v in item.items() if k not in {"city", "city_evidence"}}
        for item in after["activities"]]}
    assert protected_after == protected_before
    assert proposal.mentions[0].start_time == "09:00"
    assert proposal.mentions[2].lodging_event == "OVERNIGHT"


@pytest.mark.parametrize("response", [
    {"activities": [{"index": 0, "city": "北京"}]},
    {"activities": [{"index": 0, "city_evidence": "北京一日游"}]},
    {"activities": [dict(PATCH, city=None)]},
    {"activities": [dict(PATCH, city_evidence=None)]},
    {"activities": [dict(PATCH, city="")]},
    {"activities": [dict(PATCH, city_evidence="")]},
    {"activities": [dict(PATCH, index=True)]},
    {"activities": [dict(PATCH, index="0")]},
    {"activities": [dict(PATCH, index=0.0)]},
    {"activities": [dict(PATCH, index=-1)]},
    {"activities": [dict(PATCH, index=159)]},
    {"activities": [dict(PATCH, index=160)]},
    {"activities": [dict(PATCH, city=[PRIVATE])]},
    {"activities": [PATCH], PRIVATE: PRIVATE},
    {"activities": []},
    {"activities": None},
    '{"activities":[{"index":0,"city":"北京"',
    {"destination": "北京", "activities": [row("故宫博物院", evidence="北京一日游")]},
])
@pytest.mark.asyncio
async def test_invalid_patch_contract_cannot_promote_city_or_destroy_safe_original(response, caplog):
    output, places, _client = await run_patch(response)
    assert places.calls == [("北京", "景山公园")]
    assert output.proposal.mentions[0].city_hint is None
    assert output.proposal.mentions[0].city_evidence == FIRST["activities"][0]["city_evidence"]
    assert [card.name for card in output.public_result.days[0].activities] == ["故宫博物院", "景山公园"]
    assert output.public_result.coverage.complete is False
    assert len(city_warnings(output.proposal)) == 1
    logged = json.dumps(output.inference_binding, ensure_ascii=False) + caplog.text
    for value in (PRIVATE, SOURCE, "故宫博物院", FIRST["activities"][0]["city_evidence"]):
        assert value not in logged


@pytest.mark.parametrize("extra", [
    {"place_name": "北海公园"}, {"source_quote": "北海公园"}, {"day_index": 2}, {"role": "EXCLUDED"},
    {"occurrence": 2}, {"category": "餐饮"}, {"start_time": "12:00"}, {"timing_source": "USER"},
    {"lodging_event": "OVERNIGHT"}, {"lodging_scope": "WHOLE_TRIP"}, {"lodging_evidence": PRIVATE},
])
@pytest.mark.asyncio
async def test_city_patch_cannot_smuggle_changes_to_another_field(extra):
    output, places, _client = await run_patch({"activities": [{**PATCH, **extra}]})
    assert places.calls == [("北京", "景山公园")]
    assert [(m.atomic_place_name, m.day_index, m.role.value) for m in output.proposal.mentions] == [
        ("故宫博物院", 1, "PLANNED"), ("景山公园", 1, "PLANNED")]
    assert len(city_warnings(output.proposal)) == 1
    assert PRIVATE not in json.dumps(output.inference_binding, ensure_ascii=False)


@pytest.mark.parametrize("response", [
    ({"activities": [PATCH]}, "length"),
    TimeoutError(PRIVATE),
    APIConnectionError(message=PRIVATE, request=httpx.Request("POST", "https://example.invalid")),
])
@pytest.mark.asyncio
async def test_truncation_or_transport_failure_keeps_first_answer_and_no_third_call(response):
    output, places, _client = await run_patch(response)
    assert places.calls == [("北京", "景山公园")]
    assert len(city_warnings(output.proposal)) == 1
    assert output.public_result.coverage.complete is False
    assert PRIVATE not in json.dumps(output.inference_binding, ensure_ascii=False)


@pytest.mark.asyncio
async def test_patch_uses_original_deadline_and_preserves_first_answer_on_cancellation(monkeypatch):
    from app.trip_understanding import experience_inference as inference

    real_timeout = asyncio.timeout
    timeouts = []

    def tracked_timeout(seconds):
        timeouts.append(seconds)
        return real_timeout(seconds)

    class WaitingClient(RawClient):
        second_cancelled = False

        async def create(self, **kwargs):
            if not self.calls:
                await asyncio.sleep(0.03)
                return await super().create(**kwargs)
            self.calls.append(copy.deepcopy(kwargs))
            try:
                await asyncio.Event().wait()
            finally:
                self.second_cancelled = True

    monkeypatch.setattr(inference.asyncio, "timeout", tracked_timeout)
    client = WaitingClient(FIRST)
    engine = provider(client)
    engine.deadline_seconds = 0.08
    result = await asyncio.wait_for(engine.propose(SOURCE), timeout=0.5)
    assert timeouts[0] == 0.08
    assert len(client.calls) == 2 and client.second_cancelled
    assert result.binding["external_calls"] == 2
    assert [m.atomic_place_name for m in result.mentions] == ["故宫博物院", "景山公园"]
    assert len(city_warnings(result)) == 1
    assert result.unprocessed_count > 0


@pytest.mark.asyncio
async def test_valid_first_answer_never_spends_a_city_patch_call():
    first = copy.deepcopy(FIRST)
    first["activities"][0].update(city="北京", city_evidence="北京一日游")
    client = RawClient(first)
    result = await provider(client).propose(SOURCE)
    assert len(client.calls) == 1 and not city_warnings(result)
