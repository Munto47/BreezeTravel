"""Partial recovery preserves useful source facts without calling real services."""
import json
from pathlib import Path
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


@pytest.mark.asyncio
async def test_saved_truncated_whole_response_marks_late_days_without_changing_coverage():
    fixtures = Path(__file__).parent / "fixtures"
    sample = json.loads((fixtures / "live_capacity_truncated_whole.json").read_text(encoding="utf-8"))
    source = json.loads((fixtures / sample["source_fixture"]).read_text(encoding="utf-8"))["source"]
    client = Client(*[sample["model_response_content"]] * sample["identical_attempts"])
    original_create = client.create

    async def create(**kwargs):
        response = await original_create(**kwargs)
        response.choices[0].finish_reason = sample["finish_reason"]
        return response

    client.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    live = provider(client)
    # Replay the exact whole-document stage that failed before the heading fix.
    # No structure/day responses are invented. Place identities are controlled.
    live.enable_day_sections = False
    from tests.test_semantic_day_sections import RecordingPlaces

    output = await TripUnderstandingPipeline(live, RecordingPlaces()).run(source)
    assert len(client.calls) == 2
    assert len(output.proposal.mentions) == 63
    assert [m.day_index for m in output.proposal.mentions] == [day for day in range(1, 6) for _ in range(12)] + [6] * 3
    first_day_names = source.splitlines()[1].partition("：")[2].rstrip("。").split("、")
    assert [m.atomic_place_name for m in output.proposal.mentions] == first_day_names * 5 + first_day_names[:3]
    assert all(m.role.value == "PLANNED" for m in output.proposal.mentions)
    assert output.public_result.coverage.unprocessed_count == 54
    assert output.inference_binding["semantic_diagnostic_counts"] == {"KNOWN_PLACE_UNCLASSIFIED": 53, "OUTPUT_TRUNCATED": 1}
    counts = [day.unprocessed_count for day in output.public_result.days]
    assert counts[:5] == [0] * 5
    assert all(count > 0 for count in counts[5:])
    assert sum(counts) == 53  # The unlocated truncation warning stays global.
    assert all(not day.activities for day in output.public_result.days[6:])
    assert output.public_result.coverage.complete is False


def coverage_from_literal_hints(source, names, *, proposal=None):
    import re
    from app.trip_understanding.experience_inference import _with_coverage_diagnostics
    from app.trip_understanding.models import SourceSemanticPlan

    hints = [{"span_start": match.start(), "span_end": match.end(), "name": name}
             for name in names for match in re.finditer(re.escape(name), source)]
    return _with_coverage_diagnostics(source, SemanticDraft(activities=[]),
        proposal or SourceSemanticPlan(source_hash="0" * 64, destination_name="目的地待确认", mentions=[], binding={}), hints)


def test_unclassified_same_name_uses_its_own_source_position_and_counts_once():
    source = "Day1：星河公园。\nDay2：星河公园。"
    original = proposal_from_draft(source, SemanticDraft(activities=[activity("星河公园")]))
    result = coverage_from_literal_hints(source, ["星河公园"], proposal=original)
    assert result.mentions == original.mentions
    assert result.unprocessed_count == 1 and result.unprocessed_by_day == {2: 1}
    assert result.diagnostics[-1].span_start == source.rindex("星河公园")
    repeated = coverage_from_literal_hints(source, ["星河公园"], proposal=result)
    assert repeated == result  # Existing diagnostics are not assigned twice.


def test_markdown_day_heading_and_immediate_body_keep_source_day_counts():
    source = "北京两日游。\n## **第一天**\n星河公园。\n## **第二天**\n月光桥。"
    result = coverage_from_literal_hints(source, ["星河公园", "月光桥"])
    assert result.unprocessed_by_day == {1: 1, 2: 1}
    assert result.unprocessed_count == 2 and result.mentions == []


@pytest.mark.parametrize("source", [
    "Day1：星河公园。\nDay2：月光桥。\n总结\nDay1：星河公园。",
    "Day1：星河公园。\nDay2：月光桥。\n更正：两天安排对调。",
    "Day1：星河公园。\nDay2：月光桥。\n更正：前者改到第二天。",
    "Day1：星河公园。\nDay2：月光桥。\n更正：把前者挪后一天。",
    "Day2：星河公园。\nDay1：月光桥。",
    "Day1：星河公园。Day2：月光桥。",
    "Day1-2：星河公园、月光桥。",
])
def test_ambiguous_day_structure_keeps_new_coverage_warnings_global(source):
    result = coverage_from_literal_hints(source, ["星河公园", "月光桥"])
    assert result.unprocessed_count >= 2 and result.unprocessed_by_day == {}
    assert result.mentions == []


def test_cross_day_reference_in_a_day_body_is_not_assigned_by_physical_position():
    source = "Day1：星河公园。\nDay2：月光桥，昨天的晨光湖不再去；明天再考虑落日亭。"
    result = coverage_from_literal_hints(source, ["晨光湖", "落日亭"])
    assert result.unprocessed_count == 2 and result.unprocessed_by_day == {}


def test_undated_preface_and_footer_do_not_inherit_first_or_last_day():
    source = "晨光湖作为全程备选。\nDay1：星河公园。\nDay2：月光桥。\n其他建议：落日亭哪一天有空再去。"
    result = coverage_from_literal_hints(source, ["晨光湖", "星河公园", "月光桥", "落日亭"])
    assert result.unprocessed_count == 4 and result.unprocessed_by_day == {1: 1, 2: 1}


@pytest.mark.parametrize("body", ["其他建议：月光桥。", "### 全程备选月光桥。", "月光桥作为全程备选。"])
def test_standalone_last_heading_does_not_claim_an_undated_section(body):
    source = "Day1：星河公园。\nDay2\n" + body
    result = coverage_from_literal_hints(source, ["星河公园", "月光桥"])
    assert result.unprocessed_count == 2 and result.unprocessed_by_day == {1: 1}


def test_coverage_count_preserves_existing_unlocated_warning_and_complete_day():
    from app.trip_understanding.models import SemanticDiagnostic

    source = "Day1：星河公园。\nDay2：月光桥。"
    complete = proposal_from_draft(source, SemanticDraft(activities=[activity("星河公园"), activity("月光桥", 2)]))
    result = coverage_from_literal_hints(source, ["星河公园", "月光桥"], proposal=complete)
    assert result.unprocessed_count == 0 and result.unprocessed_by_day == {}
    partial = complete.model_copy(update={"unprocessed_count": 1,
        "diagnostics": [SemanticDiagnostic(category="OUTPUT_TRUNCATED")]})
    unchanged = coverage_from_literal_hints(source, ["星河公园", "月光桥"], proposal=partial)
    assert unchanged.unprocessed_count == 1 and unchanged.unprocessed_by_day == {}
    prior_day_warning = complete.model_copy(update={"mentions": complete.mentions[:1],
        "unprocessed_count": 2, "unprocessed_by_day": {1: 2}})
    with_missing = coverage_from_literal_hints(source, ["星河公园", "月光桥"], proposal=prior_day_warning)
    assert with_missing.unprocessed_count == 3 and with_missing.unprocessed_by_day == {1: 2, 2: 1}


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
