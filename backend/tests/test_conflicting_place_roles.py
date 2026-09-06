"""Synthetic role conflicts must not duplicate one source visit in public views."""

from __future__ import annotations

import json

import pytest

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_experience_inference import Client, provider


def _activity(role, *, name="星河公园", day=1, occurrence=1, quote=None):
    return {"place_name": name, "source_quote": quote or name, "role": role,
            "day_index": day, "occurrence": occurrence, "category": "景点"}


def _draft(rows):
    return SemanticDraft.model_validate({"destination": "北京", "activities": rows})


class RecordingResolver:
    def __init__(self):
        self.names = []

    async def resolve(self, **values):
        self.names.append(values["atomic_place_name"])
        return None


@pytest.mark.parametrize("roles", [("PLANNED", "OPTIONAL"), ("OPTIONAL", "PLANNED")])
@pytest.mark.parametrize("wide_quote", [False, True])
def test_one_atomic_source_span_cannot_have_conflicting_visit_roles(roles, wide_quote):
    source = "北京一日游。\nDay1\n晚上去星河公园散步。"
    rows = [_activity(roles[0], quote="晚上去星河公园散步" if wide_quote else None),
            _activity(roles[1])]
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(source, _draft(rows))
    assert "SOURCE_ROLE_CONFLICT" in {issue["category"] for issue in error.value.issues}
    assert error.value.repair_draft is not None
    assert [item.role.value for item in error.value.repair_draft.activities] == list(roles)


@pytest.mark.parametrize("source", [
    "Day1\n星河公园上午去，晚上不再去。",
    "Day1\n星河公园上午去。青溪桥晚上可以再去。",
])
def test_negated_or_another_places_revisit_does_not_excuse_a_role_conflict(source):
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(source, _draft([_activity("PLANNED"), _activity("OPTIONAL")]))
    assert "SOURCE_ROLE_CONFLICT" in {issue["category"] for issue in error.value.issues}


@pytest.mark.asyncio
async def test_two_literal_occurrences_keep_the_planned_visit_and_separate_option():
    source = "北京一日游。\nDay1\n上午去星河公园。晚上有空还可以去星河公园。"
    draft = _draft([_activity("PLANNED"), _activity("OPTIONAL", occurrence=2)])

    class Inference:
        async def propose(self, text):
            return proposal_from_draft(text, draft)

    resolver = RecordingResolver()
    output = await TripUnderstandingPipeline(Inference(), resolver).run(source)
    assert [item.role.value for item in output.proposal.mentions] == ["PLANNED", "OPTIONAL"]
    assert output.proposal.mentions[0].span_start != output.proposal.mentions[1].span_start
    assert resolver.names == ["星河公园"]
    assert [card.name for card in output.public_result.days[0].activities] == ["星河公园"]
    assert [choice.name for choice in output.public_result.days[0].alternatives] == ["星河公园"]


def test_one_literal_name_can_supply_explicit_arrangements_on_different_days():
    source = "北京两日游：星河公园第一天必去，第二天可选。"
    proposal = proposal_from_draft(source, _draft([
        _activity("PLANNED", day=1), _activity("OPTIONAL", day=2),
    ]))
    assert [(item.role.value, item.day_index) for item in proposal.mentions] == [("PLANNED", 1), ("OPTIONAL", 2)]
    assert proposal.mentions[0].span_start == proposal.mentions[1].span_start


@pytest.mark.parametrize("arrangement", [
    "星河公园上午必去，晚上可以再去。",
    "星河公园上午必去，晚上可以再次游览。",
    "星河公园分别在上午和晚上安排，上午必去，晚上可选。",
    "星河公园安排两次，上午必去，晚上可选。",
])
def test_local_explicit_revisit_can_share_one_literal_name(arrangement):
    proposal = proposal_from_draft("Day1\n" + arrangement, _draft([
        _activity("PLANNED"), _activity("OPTIONAL"),
    ]))
    assert [item.role.value for item in proposal.mentions] == ["PLANNED", "OPTIONAL"]
    assert len({(item.span_start, item.span_end) for item in proposal.mentions}) == 1


@pytest.mark.parametrize("other_role", ["REFERENCE", "EXCLUDED", "PASS_THROUGH"])
def test_non_visit_roles_are_not_part_of_the_planned_optional_conflict(other_role):
    proposal = proposal_from_draft("Day1\n星河公园。", _draft([
        _activity("PLANNED"), _activity(other_role),
    ]))
    assert [item.role.value for item in proposal.mentions] == ["PLANNED", other_role]


@pytest.mark.parametrize("role", ["PLANNED", "OPTIONAL"])
def test_same_role_duplicates_still_collapse_to_one_mention(role):
    proposal = proposal_from_draft("Day1\n星河公园。", _draft([_activity(role), _activity(role)]))
    assert len(proposal.mentions) == 1
    assert proposal.mentions[0].role.value == role


@pytest.mark.parametrize("source,name,expected_role", [
    ("Day1\n星河公园可以打卡。", "星河公园", "OPTIONAL"),
    ("Day1\n枫林坊简单逛一圈即可。", "枫林坊", "PLANNED"),
])
def test_explicit_source_role_normalization_precedes_conflict_detection(source, name, expected_role):
    proposal = proposal_from_draft(source, _draft([
        _activity("PLANNED", name=name), _activity("OPTIONAL", name=name),
    ]))
    assert len(proposal.mentions) == 1
    assert proposal.mentions[0].role.value == expected_role


@pytest.mark.asyncio
async def test_one_provider_repair_resolves_conflict_before_the_only_place_query():
    source = "北京一日游。\nDay1\n晚上去星河公园散步。"
    wrong = _draft([_activity("PLANNED"), _activity("OPTIONAL")]).model_dump_json()
    correct = _draft([_activity("PLANNED")]).model_dump_json()
    client = Client(wrong, correct)
    resolver = RecordingResolver()
    output = await TripUnderstandingPipeline(provider(client), resolver).run(source)
    assert len(client.calls) == 2
    assert output.inference_binding["calls"][0]["outcome"] == "SOURCE_ROLE_CONFLICT"
    assert resolver.names == ["星河公园"]
    assert len(output.public_result.days[0].activities) == 1
    assert output.public_result.days[0].alternatives == []
    repair_data = json.loads(client.calls[1]["messages"][-1]["content"].split("\n", 1)[1])
    assert "SOURCE_ROLE_CONFLICT" in {issue["category"] for issue in repair_data["errors"]}


@pytest.mark.asyncio
async def test_two_conflicting_provider_answers_fail_without_searching_or_fake_success():
    source = "北京一日游。\nDay1\n晚上去星河公园散步。"
    wrong = _draft([_activity("PLANNED"), _activity("OPTIONAL")]).model_dump_json()
    client = Client(wrong, wrong)
    resolver = RecordingResolver()
    with pytest.raises(InferenceProviderUnavailableError) as error:
        await TripUnderstandingPipeline(provider(client), resolver).run(source)
    assert len(client.calls) == 2 and resolver.names == []
    assert error.value.external_call_count == 2
    assert [call["outcome"] for call in error.value.provider_binding["calls"]] == [
        "SOURCE_ROLE_CONFLICT", "SOURCE_ROLE_CONFLICT",
    ]
    assert source not in json.dumps(error.value.provider_binding, ensure_ascii=False)
