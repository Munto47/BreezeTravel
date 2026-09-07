"""Partial recovery preserves useful source facts without calling real services."""
import json
from types import SimpleNamespace

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.pipeline import EvidenceCompiler, PublicResultProjector, TripUnderstandingPipeline
from app.trip_understanding.models import ResolvedActivity, ResolutionStatus
from app.trip_understanding.semantic_recovery import complete_activities_from_truncated_json, explicit_reference_context
from tests.test_experience_inference import Client, provider


def activity(name, day=1, **fields):
    return {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": day, **fields}


@pytest.mark.asyncio
async def test_explicit_unnamed_lunch_keeps_gap_without_a_fake_place_card():
    source = "Day1：星河公园。午餐。月光桥。晚餐外婆家。"
    draft = {"activities": [activity("星河公园"), activity("午餐", place_name=None, category="餐饮"),
        activity("月光桥"), activity("外婆家", category="餐饮")]}
    output = await TripUnderstandingPipeline(provider(Client(json.dumps(draft))), ControlledSnapshotPlaceResolver()).run(source)
    day = output.public_result.days[0]
    assert [card.name for card in day.activities] == ["星河公园", "月光桥", "外婆家"]
    assert day.activities[-1].meal_role == "DINNER" and day.activities[-1].start_time is None
    assert day.meal_slots[0].meal_role == "LUNCH"
    assert day.meal_slots[0].after_activity_token == day.activities[0].activity_token
    assert day.meal_slots[0].before_activity_token == day.activities[1].activity_token
    assert output.proposal.mentions[1].atomic_place_name is None


def test_meal_name_or_wrong_model_claim_does_not_manufacture_lunch():
    source = "Day1：星河餐厅。"
    draft = SemanticDraft.model_validate({"activities": [activity("星河餐厅", category="餐饮", meal_role="LUNCH")]})
    assert proposal_from_draft(source, draft).mentions[0].meal_role is None


@pytest.mark.asyncio
@pytest.mark.parametrize("city,unprocessed,complete", [("北京", 0, True), ("上海", 1, False)])
async def test_redundant_city_metadata_is_not_a_missing_place_but_conflicting_city_stays_pending(city, unprocessed, complete):
    source = "北京 Day1：故宫博物院。"
    draft = {"destination": "北京", "activities": [activity("故宫博物院", category="景点", city=city)]}
    output = await TripUnderstandingPipeline(provider(Client(json.dumps(draft))), ControlledSnapshotPlaceResolver()).run(source)
    assert output.proposal.mentions[0].city_hint is None
    assert output.proposal.unprocessed_count == unprocessed
    assert output.public_result.coverage.complete is complete
    assert output.public_result.coverage.recognized_place_count == output.public_result.coverage.confirmed_place_count == 1
    assert output.public_result.coverage.unclassified_mention_count == 0
    category = "REDUNDANT_CITY_HINT_REMOVED" if complete else "UNSUPPORTED_CITY_REMOVED"
    assert any(issue.category == category for issue in output.proposal.diagnostics)


@pytest.mark.asyncio
async def test_redundant_city_cleanup_cannot_hide_actual_unprocessed_source():
    source = "北京 Day1：故宫博物院，然后那个地方再看。"
    draft = {"destination": "北京", "activities": [activity("故宫博物院", category="景点", city="北京")],
        "unprocessed_quotes": ["然后那个地方再看"]}
    output = await TripUnderstandingPipeline(provider(Client(json.dumps(draft))), ControlledSnapshotPlaceResolver()).run(source)
    assert output.proposal.unprocessed_count == 1
    assert output.public_result.coverage.complete is False


def test_redundant_destination_does_not_waive_a_false_explicit_city_evidence_claim():
    source = "去北京路步行街。"
    draft = SemanticDraft.model_validate({"destination": "北京", "activities": [
        activity("北京路步行街", city="北京", city_evidence="北京路步行街")]})
    result = proposal_from_draft(source, draft)
    assert result.mentions[0].city_hint is None
    assert result.unprocessed_count == 1
    assert [issue.category for issue in result.diagnostics] == ["UNSUPPORTED_CITY_REMOVED"]


@pytest.mark.parametrize("text,reason", [
    ("星河公园面积很大，留足时间。", "DESCRIPTION"),
    ("中午在星河公园附近的饭店用餐。", "LOCATION_REFERENCE"),
    ("需要注意的是，星河公园实行预约，请提前预订。", "ADVISORY"),
    ("住宿推荐选择在星河公园附近，交通便利。", "LODGING_AREA_REFERENCE"),
    ("第一天，我们将聚焦于星河公园与周边区域。", "DAY_OVERVIEW"),
    ("Day2：再去星河公园。", None),
    ("Day2：游览星河公园，附近吃饭。", None),
    ("需要注意的是，预约成功后前往星河公园。", None),
    ("Day2：星河公园。", None),
])
def test_only_explicit_reference_grammar_waives_known_noun_coverage(text, reason):
    start = text.index("星河公园")
    assert explicit_reference_context(text, start, start + 4) == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["{broken", None])
async def test_one_bad_source_reference_cannot_destroy_valid_days_or_become_a_place(second):
    source = "Day1：星河公园。Day2：月光桥。"
    first = {"activities": [activity("星河公园"), activity("月光桥", 2, source_quote="不存在的句子")]}
    client = Client(json.dumps(first), second or json.dumps(first))
    result = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(source)
    assert [(m.atomic_place_name, m.day_index) for m in result.proposal.mentions] == [("星河公园", 1)]
    assert result.public_result.status == "PARTIAL_RESULT"
    assert result.public_result.coverage.recognized_place_count == 1
    assert result.public_result.coverage.unclassified_mention_count >= 1
    assert result.public_result.coverage.complete is False
    assert result.resolution_receipt["attempted_count"] == 1
    assert result.inference_binding["semantic_partial_recovery"] is True
    assert "不存在" not in json.dumps(result.inference_binding, ensure_ascii=False)
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_mixed_timing_and_source_failure_only_removes_the_invalid_source_item():
    source = "Day1：星河公园、月光桥。Day2：晨光湖。"
    first = {"activities": [activity("星河公园", start_time="09:00", time_evidence="没有的时间"),
        activity("月光桥", source_quote="没有的引用"), activity("晨光湖", 2)]}
    result = await provider(Client(json.dumps(first), "{broken")).propose(source)
    assert [(m.atomic_place_name, m.day_index) for m in result.mentions] == [("星河公园", 1), ("晨光湖", 2)]
    assert result.mentions[0].start_time is None
    assert {issue.category for issue in result.diagnostics} >= {"TIME_EVIDENCE_NOT_IN_SOURCE", "SOURCE_QUOTE_NOT_FOUND"}
    assert result.binding["outcome"] == "PARTIAL_RESULT"


@pytest.mark.asyncio
async def test_repair_cannot_erase_or_reorder_previously_valid_places():
    source = "Day1：星河公园、晨光桥、月光桥。"
    first = {"activities": [activity("星河公园"), activity("晨光桥"), activity("月光桥", source_quote="坏引用")]}
    second = {"activities": [activity("晨光桥", day=2), activity("月光桥")]}
    result = await provider(Client(json.dumps(first), json.dumps(second))).propose(source)
    assert [(m.atomic_place_name, m.day_index) for m in result.mentions] == [
        ("星河公园", 1), ("晨光桥", 1), ("月光桥", 1)]


@pytest.mark.asyncio
async def test_dropped_invalid_source_item_remains_diagnostic_in_a_valid_repair():
    source = "Day1：星河公园。Day2：月光桥。"
    first = {"activities": [activity("星河公园"), activity("月光桥", day=None)]}
    second = {"activities": [activity("星河公园")]}
    result = await provider(Client(json.dumps(first), json.dumps(second))).propose(source)
    assert [m.atomic_place_name for m in result.mentions] == ["星河公园"]
    assert result.binding["outcome"] == "PARTIAL_RESULT"
    assert any(issue.category == "REPAIR_OMITTED_SOURCE_ITEM" for issue in result.diagnostics)


@pytest.mark.asyncio
async def test_cancelled_invalid_item_never_reappears_in_partial_mainline():
    source = "Day1：星河公园、月光桥。最终修改为：取消月光桥。"
    draft = {"activities": [activity("星河公园"), activity("月光桥")]}
    value = json.dumps(draft)
    result = await provider(Client(value, "{broken")).propose(source)
    assert [m.atomic_place_name for m in result.mentions] == ["星河公园"]
    assert any(issue.category == "EXPLICIT_CANCELLATION_CONFLICT" for issue in result.diagnostics)


def test_conflicting_roles_remove_both_source_interpretations_in_partial_output():
    source = "Day1：星河公园，月光桥。"
    draft = SemanticDraft.model_validate({"activities": [activity("星河公园"), activity("月光桥"),
        activity("月光桥", role="OPTIONAL")]})
    result = proposal_from_draft(source, draft, allow_partial=True)
    assert [m.atomic_place_name for m in result.mentions] == ["星河公园"]
    assert any(issue.category == "SOURCE_ROLE_CONFLICT" for issue in result.diagnostics)


def test_truncation_parser_accepts_only_completed_top_level_activity_objects():
    content = '{"destination":"北京","activities":[' + json.dumps(activity("星河公园")) + ',{"source_quote":"unfinished'
    result = complete_activities_from_truncated_json(content)
    assert result["activities"] == [activity("星河公园")]
    assert complete_activities_from_truncated_json('prose {"activities":[{}]') is None
    assert complete_activities_from_truncated_json('{"nested":{"activities":[{}]') is None
    assert complete_activities_from_truncated_json('{"activities":[{"source_quote":"unfinished') is None


@pytest.mark.asyncio
async def test_truncated_long_output_preserves_completed_days_and_cannot_claim_completeness():
    source = "\n".join(f"Day{day}：星河{day}公园。" for day in range(1, 15))
    complete = [activity(f"星河{day}公园", day) for day in range(1, 8)]
    content = '{"activities":' + json.dumps(complete)[:-1] + ',{"source_quote":"星河8'
    client = Client(content, "{bad")
    original_create = client.create

    async def create(**kwargs):
        response = await original_create(**kwargs)
        response.choices[0].finish_reason = "length" if len(client.calls) == 1 else "stop"
        return response

    client.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    result = await provider(client).propose(source)
    assert [m.day_index for m in result.mentions] == list(range(1, 8))
    assert result.day_count == 14
    assert result.binding["outcome"] == "PARTIAL_RESULT"
    assert any(issue.category == "OUTPUT_TRUNCATED" for issue in result.diagnostics)
    assert len(client.calls) == 2


@pytest.mark.parametrize("newline,heading", [("\n", "### "), ("\r\n", ""), ("\n", "")])
def test_same_name_in_two_branches_survives_public_projection(newline, heading):
    source = newline.join(["Day1：城中/海岸二选一", f"{heading}方案A：城中", "星河公园。",
                           f"{heading}方案B：海岸", "月光桥，星河公园。"])
    draft = SemanticDraft.model_validate({"activities": [activity("星河公园", role="OPTIONAL"),
        activity("月光桥", role="OPTIONAL"), activity("星河公园", role="OPTIONAL", occurrence=2)]})
    proposal = proposal_from_draft(source, draft)
    compiled = EvidenceCompiler().compile(source, proposal)[0]
    resolved = [ResolvedActivity(compiled=item, resolution_status=ResolutionStatus.NOT_ELIGIBLE) for item in compiled]
    result = PublicResultProjector().project(proposal.destination_name, proposal.destination_basis, resolved, include_alternatives=True)
    choices = result.days[0].alternatives
    assert [choice.name for choice in choices] == ["星河公园", "月光桥", "星河公园"]
    assert [choice.branch_label for choice in choices] == ["方案A", "方案B", "方案B"]
    assert choices[0].branch_token != choices[2].branch_token
    assert len({choice.activity_token for choice in choices}) == 3
    assert choices[0].choice_group_token == choices[2].choice_group_token
    assert "span_start" not in result.model_dump_json()
    assert "choice-" not in result.model_dump_json()


def test_stated_internal_detail_retains_its_parent_without_extra_main_stop():
    source = "Day1：星河公园，园内路线：月光亭。"
    draft = SemanticDraft.model_validate({"activities": [activity("星河公园"), activity("月光亭")]})
    proposal = proposal_from_draft(source, draft)
    parent, child = proposal.mentions
    assert child.role.value == "REFERENCE"
    assert child.parent_mention_id == parent.mention_id
    assert child.relation_type == "INTERNAL_DETAIL"
    assert not EvidenceCompiler().compile(source, proposal)[0][1].eligible_for_place_search


@pytest.mark.asyncio
async def test_known_name_coverage_is_a_question_not_an_automatic_visit(monkeypatch):
    source = "Day1：星河公园。资料提到月光桥。"
    start = source.index("月光桥")
    monkeypatch.setattr("app.trip_understanding.experience_inference._known_source_places",
        lambda _source: [{"name": "月光桥", "span_start": start, "span_end": start + 3}])
    client = Client(json.dumps({"activities": [activity("星河公园")]}))
    result = await provider(client).propose(source)
    assert [m.atomic_place_name for m in result.mentions] == ["星河公园"]
    assert result.diagnostics[0].category == "KNOWN_PLACE_UNCLASSIFIED"
    assert result.binding["outcome"] == "PARTIAL_RESULT"
    assert len(client.calls) == 1
    assert "仅供核对遗漏" in client.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_explicit_reference_accounts_for_known_name_without_promoting_it(monkeypatch):
    source = "Day1：星河公园。资料提到月光桥。"
    start = source.index("月光桥")
    monkeypatch.setattr("app.trip_understanding.experience_inference._known_source_places",
        lambda _source: [{"name": "月光桥", "span_start": start, "span_end": start + 3}])
    client = Client(json.dumps({"activities": [activity("星河公园"), activity("月光桥", role="REFERENCE")]}))
    result = await provider(client).propose(source)
    assert result.diagnostics == []
    assert result.mentions[1].role.value == "REFERENCE"


@pytest.mark.asyncio
async def test_immutable_persisted_semantics_never_contains_source_offsets_or_literal_diagnostics():
    from app.trip_understanding.repository import _persisted_proposal

    source = "Day1：星河公园。Day2：月光桥。"
    first = {"activities": [activity("星河公园"), activity("月光桥", day=None)]}
    encoded = json.dumps(first)
    output = await TripUnderstandingPipeline(provider(Client(encoded, "{broken")), ControlledSnapshotPlaceResolver()).run(source)
    retained = _persisted_proposal(output)
    serialized = json.dumps(retained, ensure_ascii=False)
    assert retained["structure"][0]["mention_id"] == output.proposal.mentions[0].mention_id
    assert retained["diagnostics"]
    assert all(forbidden not in serialized for forbidden in ("span_start", "span_end", "source_quote", "星河公园", "月光桥"))
    assert retained["verbatim_quotes"] == "ENCRYPTED_IN_SOURCE_CLAIMS"
