"""Synthetic source families and controlled patches, zero external services."""
import asyncio
import copy
import json

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft, SourceAnchorValidationError, _expand_source_bound_lists, _proposal_from_live_draft,
)
from app.trip_understanding.focused_semantic_repair import repair_targets
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_semantic_supplement_budget import Client, provider


SOURCE = "北京。\nDay1：青岚乐园，园内体验云海航船、星光环线。\n逛星河公园、月光桥。\nDay2：晨光博物馆（东馆，免费）。"


def activity(name, day=1, **extra):
    return {"place_name": name, "source_quote": name, "role": "PLANNED", "day_index": day,
            "category": "景点", "source_details": [], **extra}


FIRST = {"destination": "北京", "day_labels": [None, None], "activities": [
    activity("青岚乐园"), activity("星河公园"),
    activity("晨光博物馆", 2, source_quote="晨光博物馆（东馆，免费）")], "unprocessed_quotes": [], "order_groups": []}
EMPTY = {"name_fields": [], "city_fields": [], "missing_activities": [], "source_visits": []}


def controlled_patch():
    return {**copy.deepcopy(EMPTY),
        "name_fields": [{"index": 2, "place_name": "晨光博物馆（东馆）"}],
        "missing_activities": [{"target_index": 0, "activity": activity("月光桥")}],
        "source_visits": [{"parent_index": 0, "kind": "VISIT", "source_quote": name,
            "optional": False, "evidence": "青岚乐园，园内体验云海航船、星光环线"}
            for name in ["云海航船", "星光环线"]]}


async def run(source=SOURCE, first=None, second=None, *, client=None):
    client = client or Client(first or FIRST, second if second is not None else controlled_patch())
    inference = provider(client)
    inference.enable_focused_repair = True
    inference.enable_day_sections = False
    result = await TripUnderstandingPipeline(inference, FixedReplayPlaces(), relative_only=True).run(source)
    return result, client


def cards(result):
    return [[m.name for m in day.activities] for day in result.public_result.days]


def test_first_answer_has_bounded_targets_without_using_coverage_vocabulary():
    draft = _expand_source_bound_lists(SOURCE, SemanticDraft.model_validate(FIRST))
    with pytest.raises(SourceAnchorValidationError) as raised:
        _proposal_from_live_draft(SOURCE, draft)
    names, missing = repair_targets(SOURCE, raised.value.repair_draft, raised.value)
    assert [x["place_name"] for x in names] == ["晨光博物馆（东馆）"]
    assert [(x["name"], x["day_index"]) for x in missing] == [("月光桥", 1)]


@pytest.mark.asyncio
async def test_second_patch_keeps_original_order_and_fills_name_missing_visit_and_parent_details():
    result, client = await run()
    assert len(client.calls) == result.inference_binding["external_calls"] == 2
    assert result.inference_binding["focused_repair_enabled"] is True
    assert client.calls[1]["response_format"]["json_schema"]["name"] == "BreezeTravelSemanticPatch"
    assert cards(result) == [["青岚乐园", "星河公园", "月光桥"], ["晨光博物馆（东馆）"]]
    assert [x.name for x in result.public_result.days[0].activities[0].source_details] == ["云海航船", "星光环线"]
    assert result.resolution_receipt["attempted_count"] == 4
    assert not any(x.category in {"PLACE_QUALIFIER_OMITTED", "MISSING_EXPLICIT_PARALLEL_PLACE"}
                   for x in result.proposal.diagnostics)
    # Multiple parents and unassessed order remain unfinished; the patch is
    # not evidence that all unstructured source instructions were found.
    assert not result.public_result.coverage.complete


@pytest.mark.asyncio
@pytest.mark.parametrize("second", [EMPTY, "{broken", ({**EMPTY}, "length"), {**EMPTY, "activities": []}])
async def test_empty_invalid_and_truncated_patch_keep_correct_first_visits_and_failure(second):
    result, client = await run(second=second)
    assert len(client.calls) == 2
    assert cards(result) == [["青岚乐园", "星河公园"], []]
    assert result.public_result.coverage.unprocessed_count > 0
    assert not result.public_result.coverage.complete


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["wrong_day", "other_name", "other_occurrence", "duplicate", "extra_field", "wrong_role", "optional_downgrade"])
async def test_bad_addition_cannot_modify_valid_first_visits_or_block_valid_name_fix(change):
    patch = controlled_patch()
    row = patch["missing_activities"][0]["activity"]
    if change == "wrong_day":
        row["day_index"] = 2
    elif change == "other_name":
        row.update(place_name="星河公园", source_quote="星河公园")
    elif change == "other_occurrence":
        row["occurrence"] = 2
    elif change == "duplicate":
        patch["missing_activities"].append(copy.deepcopy(patch["missing_activities"][0]))
    elif change == "extra_field":
        row["replace_original_index"] = 0
    elif change == "optional_downgrade":
        row["role"] = "OPTIONAL"
    else:
        row["role"] = "REFERENCE"
    result, _ = await run(second=patch)
    assert cards(result) == [["青岚乐园", "星河公园"], ["晨光博物馆（东馆）"]]
    assert any(x.category == "FOCUSED_PATCH_REJECTED" for x in result.proposal.diagnostics)
    assert not result.public_result.coverage.complete


@pytest.mark.asyncio
async def test_valid_original_name_cannot_be_renamed_and_valid_sibling_patch_survives():
    patch = controlled_patch()
    patch["name_fields"].append({"index": 0, "place_name": "星河公园"})
    result, _ = await run(second=patch)
    assert cards(result) == [["青岚乐园", "星河公园", "月光桥"], ["晨光博物馆（东馆）"]]
    assert any(x.category == "FOCUSED_PATCH_REJECTED" for x in result.proposal.diagnostics)


@pytest.mark.asyncio
async def test_same_name_parent_on_two_days_keeps_details_on_request_occurrence_after_insertion():
    source = SOURCE + "\n再访青岚乐园，园内体验月影飞车。"
    first = copy.deepcopy(FIRST)
    first["activities"].append(activity("青岚乐园", 2, occurrence=2))
    patch = controlled_patch()
    patch["source_visits"].append({"parent_index": 2, "kind": "VISIT", "source_quote": "月影飞车",
        "optional": False, "evidence": "再访青岚乐园，园内体验月影飞车"})
    result, _ = await run(source, first, patch)
    assert cards(result)[1] == ["晨光博物馆（东馆）", "青岚乐园"]
    assert [x.name for x in result.public_result.days[1].activities[-1].source_details] == ["月影飞车"]
    assert [x.name for x in result.public_result.days[0].activities[0].source_details] == ["云海航船", "星光环线"]


@pytest.mark.asyncio
async def test_wrong_parent_and_cancelled_detail_do_not_borrow_another_visits_authority():
    patch = controlled_patch()
    patch["source_visits"] = [{"parent_index": 1, "kind": "VISIT", "source_quote": "云海航船",
        "optional": False, "evidence": "青岚乐园，园内体验云海航船、星光环线"},
        {"parent_index": 0, "kind": "VISIT", "source_quote": "听雨飞车", "optional": False,
         "evidence": "青岚乐园内不玩听雨飞车"}]
    result, _ = await run(SOURCE + "\n青岚乐园内不玩听雨飞车。", second=patch)
    assert all(not x.source_details for day in result.public_result.days for x in day.activities)
    assert any(x.category == "SOURCE_VISIT_UNRESOLVED" for x in result.proposal.diagnostics)


@pytest.mark.asyncio
async def test_shared_deadline_restores_first_but_external_user_cancel_propagates():
    class Slow(Client):
        async def create(self, **kwargs):
            if self.calls:
                self.calls.append(kwargs)
                await asyncio.sleep(10)
            return await super().create(**kwargs)
    client = Slow(FIRST)
    inference = provider(client, deadline=.05)
    inference.enable_focused_repair = True
    inference.enable_day_sections = False
    result = await inference.propose(SOURCE)
    assert len(client.calls) == result.binding["external_calls"] == 2
    assert result.binding["calls"][1]["outcome"] == "DEADLINE_EXCEEDED"
    assert {m.atomic_place_name for m in result.mentions} >= {"青岚乐园", "星河公园"}
    client = Client(FIRST, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run(client=client)


def test_patch_wire_preserves_required_business_activity_fields_and_forbids_replacement():
    from app.trip_understanding.focused_semantic_repair import patch_schema
    from jsonschema import Draft202012Validator
    schema = patch_schema(provider(Client()).schema)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(EMPTY)
    item = schema["$defs"]["SemanticActivity"]
    assert {"place_name", "day_index", "source_details"} <= set(item["required"])
    assert "start_time" not in item["properties"]
    assert {"meal_role", "lodging_event", "role", "city_evidence"} <= set(item["properties"])
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(controlled_patch())
    invalid = controlled_patch()
    del invalid["missing_activities"][0]["activity"]["place_name"]
    assert list(Draft202012Validator(schema).iter_errors(invalid))
    json.dumps(schema)  # No model/callable values in the actual wire contract.


@pytest.mark.asyncio
async def test_actual_second_patch_overflow_retains_two_call_failure_accounting():
    from app.trip_understanding.errors import InferenceProviderUnavailableError
    names = [f"星河{i}公园" for i in range(158)]
    source = "北京。\nDay1：青岚乐园，园内体验云海航船。\n" + "。\n".join(names) + "。\n逛北斗公园、月光桥。"
    first = {"destination": "北京", "day_labels": [None], "activities": [activity(name)
        for name in ["青岚乐园", *names, "北斗公园"]]}
    patch = {**EMPTY, "missing_activities": [{"target_index": 0, "activity": activity("月光桥")}]}
    client = Client(first, patch)
    inference = provider(client, deadline=10)
    inference.enable_focused_repair = True
    inference.enable_day_sections = False
    with pytest.raises(InferenceProviderUnavailableError, match="INPUT_CAPACITY_EXCEEDED") as raised:
        await inference.propose(source)
    assert len(client.calls) == raised.value.external_call_count == 2
    assert raised.value.provider_binding["calls"][-1]["outcome"] == "INPUT_CAPACITY_EXCEEDED"


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
async def test_only_complete_optional_area_caption_becomes_reference_and_both_real_options_remain(count):
    source = ("北京。\nDay1：白云公园。\nDay2：二选一\n### 方案 A：城中\n"
              "③青溪湾，逛青溪湾天主教堂、星河城商圈。\n### 方案 B：林间\n云岭公园。")
    first = {"destination": "北京", "day_labels": [None, None], "activities": [
        activity("白云公园"), activity("青溪湾", 2, role="OPTIONAL"), activity("云岭公园", 2, role="OPTIONAL")]}
    patch = {**EMPTY, "missing_activities": [{"target_index": index,
        "activity": activity(name, 2, role="OPTIONAL")}
        for index, name in enumerate(["青溪湾天主教堂", "星河城商圈"][:count])]}
    result, _ = await run(source, first, patch)
    assert cards(result) == [["白云公园"], []]
    expected = ["青溪湾天主教堂", "星河城商圈", "云岭公园"] if count == 2 else ["青溪湾", "青溪湾天主教堂", "云岭公园"]
    assert [m.name for m in result.public_result.days[1].alternatives] == expected
    caption = next(m for m in result.proposal.mentions if m.atomic_place_name == "青溪湾")
    assert caption.role.value == ("REFERENCE" if count == 2 else "OPTIONAL")
    assert source[caption.span_start:caption.span_end] == "青溪湾" and caption.day_index == 2
    assert bool(any(d.category == "MISSING_EXPLICIT_PARALLEL_PLACE" for d in result.proposal.diagnostics)) == (count == 1)


@pytest.mark.asyncio
async def test_new_occurrence_does_not_reorder_an_already_established_first_sequence():
    first = copy.deepcopy(FIRST)
    # This test checks the recovery boundary, not a claim that this mock order
    # is the correct interpretation. A patch has no permission to change it.
    first["activities"][:2] = list(reversed(first["activities"][:2]))
    result, _ = await run(first=first)
    assert cards(result)[0][:2] == ["星河公园", "青岚乐园"]
    assert [m.atomic_place_name for m in result.proposal.mentions if m.day_index == 1 and m.role.value == "PLANNED"][:2] == ["星河公园", "青岚乐园"]


@pytest.mark.asyncio
async def test_sdk_serializes_one_strict_patch_request_after_initial_validation_failure():
    from tests.test_experience_strict_wire_contract import recorded_provider
    async with recorded_provider(FIRST, controlled_patch()) as (inference, requests):
        inference.enable_source_visits = True
        inference.enable_focused_repair = True
        inference.enable_day_sections = False
        result = await inference.propose(SOURCE)
    assert len(requests) == result.binding["external_calls"] == 2
    assert requests[1]["response_format"]["json_schema"]["name"] == "BreezeTravelSemanticPatch"
    assert requests[1]["response_format"]["json_schema"]["strict"] is True
    assert requests[1]["max_tokens"] <= 4096 and requests[1]["temperature"] == 0


@pytest.mark.asyncio
async def test_missing_required_visit_cannot_be_downgraded_to_optional_and_marked_complete():
    # Independently found during review. The original bad result had one main,
    # one alternative, no warnings and complete=True.
    source = "北京。\nDay1：游览青岚公园、月光桥。"
    first = {"destination": "北京", "day_labels": [None], "activities": [activity("青岚公园")]}
    patch = {**EMPTY, "missing_activities": [{"target_index": 0,
        "activity": activity("月光桥", role="OPTIONAL")}]}
    result, _ = await run(source, first, patch)
    assert cards(result) == [["青岚公园"]]
    assert not result.public_result.days[0].alternatives
    assert result.public_result.coverage.unprocessed_count > 0 and not result.public_result.coverage.complete
    assert {d.category for d in result.proposal.diagnostics} >= {
        "MISSING_EXPLICIT_PARALLEL_PLACE", "FOCUSED_PATCH_REJECTED"}


@pytest.mark.asyncio
async def test_default_keeps_original_whole_second_answer_and_never_implicitly_enables_focused_repair():
    second = {"destination": "北京", "day_labels": [None, None], "activities": [
        activity("青岚乐园"), activity("星河公园"), activity("月光桥"),
        activity("晨光博物馆（东馆）", 2, source_quote="晨光博物馆（东馆，免费）")]}
    client = Client(FIRST, second)
    inference = provider(client)
    inference.enable_day_sections = False
    result = await TripUnderstandingPipeline(inference, FixedReplayPlaces(), relative_only=True).run(SOURCE)
    assert inference.enable_focused_repair is False
    assert result.inference_binding["focused_repair_enabled"] is False
    assert len(client.calls) == result.inference_binding["external_calls"] == 2
    assert [call["response_format"]["json_schema"]["name"] for call in client.calls] == ["BreezeTravelSemanticDraft"] * 2
    assert cards(result) == [["青岚乐园", "星河公园", "月光桥"], ["晨光博物馆（东馆）"]]


def test_configured_real_worker_does_not_select_development_focused_repair(monkeypatch):
    from types import SimpleNamespace
    from app.trip_understanding import worker
    from app.trip_understanding.experience_inference import ExperienceQwenProvider
    inputs = []
    def configured(**kwargs):
        inputs.append(kwargs)
        return ExperienceQwenProvider(**kwargs, client=Client())
    monkeypatch.setattr(worker, "ExperienceQwenProvider", configured)
    monkeypatch.setattr(worker, "AmapPlaceResolver", lambda **kwargs: FixedReplayPlaces())
    settings = SimpleNamespace(trip_understanding_provider_mode="live", qwen_api_key="test",
        qwen_api_url="https://test.invalid", trip_understanding_qwen_model="controlled",
        trip_understanding_qwen_deadline_seconds=1, trip_understanding_qwen_max_output_tokens=4096,
        trip_understanding_qwen_input_cny_per_million=None, trip_understanding_qwen_output_cny_per_million=None,
        amap_api_key="test", trip_understanding_amap_place_deadline_seconds=1,
        trip_understanding_amap_place_max_concurrency=1)
    pipeline = worker.build_configured_full_pipeline(settings)
    assert "enable_focused_repair" not in inputs[0]
    assert pipeline.inference_provider.enable_focused_repair is False
    assert pipeline.inference_provider.enable_source_visits is True


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_exact_duplicate_raw_row_does_not_consume_a_160th_real_visit_slot(duplicate):
    names = [f"星河{i}公园" for i in range(157)]
    source = "北京。\nDay1：青岚乐园，园内体验云海航船。\n" + "。\n".join(names) + "。\n逛北斗公园、月光桥。"
    rows = [activity(name) for name in ["青岚乐园", *names, "北斗公园"]]
    if duplicate:
        rows.append(copy.deepcopy(rows[0]))
    first = {"destination": "北京", "day_labels": [None], "activities": rows}
    original = copy.deepcopy(first)
    patch = {**EMPTY, "missing_activities": [{"target_index": 0, "activity": activity("月光桥")}]}
    result, client = await run(source, first, patch)
    assert first == original  # Stored/supplied first answer remains untouched.
    assert cards(result) == [["青岚乐园", *names, "北斗公园", "月光桥"]]
    assert len(result.public_result.days[0].activities) == 160
    assert len(client.calls) == result.inference_binding["external_calls"] == 2
    assert result.inference_binding["focused_repair_enabled"] is True


def test_duplicate_compaction_preserves_real_revisits_and_conflicting_facts_and_remaps_indices():
    from app.trip_understanding.focused_semantic_repair import _compact_identical_occurrences
    source = "Day1：青岚乐园、星河公园。再次到青岚乐园。"
    first = activity("青岚乐园")
    raw = {"activities": [first, copy.deepcopy(first), activity("星河公园"),
        activity("青岚乐园", occurrence=2), activity("青岚乐园", category="地点")],
        "order_groups": [{"kind": "REQUIRED_PRECEDENCE", "activity_indices": [0, 2, 3],
            "scope_quote": source, "required_precedence": [{"before_index": 2, "after_index": 3,
                "evidence": "星河公园。再次到青岚乐园"}]}]}
    draft = SemanticDraft.model_validate(raw)
    compact = _compact_identical_occurrences(source, draft)
    assert len(compact.activities) == 4 and len(draft.activities) == 5
    assert [(m.place_name, m.occurrence, m.category) for m in compact.activities] == [
        ("青岚乐园", 1, "景点"), ("星河公园", 1, "景点"), ("青岚乐园", 2, "景点"), ("青岚乐园", 1, "地点")]
    assert compact.order_groups[0].activity_indices == (0, 1, 2)
    assert compact.order_groups[0].required_precedence[0].before_index == 1
    assert compact.order_groups[0].required_precedence[0].after_index == 2


@pytest.mark.asyncio
async def test_conflicting_extra_raw_row_does_not_prove_source_overflow_or_destroy_partial_result():
    names = [f"星河{i}公园" for i in range(157)]
    source = "北京。\nDay1：青岚乐园，园内体验云海航船。\n" + "。\n".join(names) + "。\n逛北斗公园、月光桥。"
    rows = [activity(name) for name in ["青岚乐园", *names, "北斗公园"]]
    rows.append(activity("青岚乐园", category="地点"))
    first = {"destination": "北京", "day_labels": [None], "activities": rows}
    patch = {**EMPTY, "missing_activities": [{"target_index": 0, "activity": activity("月光桥")}]}
    result, client = await run(source, first, patch)
    assert cards(result) == [["青岚乐园", *names, "北斗公园"]]
    assert result.public_result.coverage.unprocessed_count > 0 and not result.public_result.coverage.complete
    assert len(client.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_day_section_binding_records_explicit_development_choice_without_changing_calls(enabled):
    from app.trip_understanding.experience_inference import ExperienceQwenProvider
    from tests.test_semantic_day_sections import ScopedClient, source
    client = ScopedClient()
    inference = ExperienceQwenProvider(api_key="test", base_url="https://test.invalid", model="controlled",
        client=client, enable_focused_repair=enabled)
    result = await inference.propose(source())
    assert result.binding["focused_repair_enabled"] is enabled
    assert result.binding["external_calls"] == len(client.calls) == 3
    assert [(m.atomic_place_name, m.day_index) for m in result.mentions] == [("星河公园", 1), ("月光桥", 2)]
