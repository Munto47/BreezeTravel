"""Fixed model replies through the real adapter/pipeline; no external services."""
import copy
import json

import pytest

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.experience_inference import SemanticDraft, _expand_source_bound_lists
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_semantic_supplement_budget import Client, EVIDENCE, FIRST, SOURCE, provider


def detail(name, evidence, *, kind="VISIT", optional=False, **extra):
    return dict(kind=kind, source_quote=name, optional=optional, evidence=evidence, **extra)


def activity(name, day=1, **extra):
    return dict(source_quote=name, place_name=name, role="PLANNED", day_index=day,
                category="景点", **extra)


def draft(*activities, days=1):
    return dict(destination="北京", day_labels=[None] * days, activities=list(activities))


async def run(source, *answers):
    client = Client(*answers)
    inference = provider(client)
    inference.enable_day_sections = False
    result = await TripUnderstandingPipeline(inference, FixedReplayPlaces(), relative_only=True).run(source)
    assert len(client.calls) <= 2
    return result, client


def inline_first(rows):
    first = copy.deepcopy(FIRST)
    first["activities"][0]["source_details"] = rows
    return first


def main(result):
    return [[card.name for card in day.activities] for day in result.public_result.days]


def names(result, day=0, *, alternative=False):
    items = result.public_result.days[day].alternatives if alternative else result.public_result.days[day].activities
    return [item.name for item in items[0].source_details]


@pytest.mark.asyncio
async def test_broken_first_answer_second_whole_draft_keeps_inline_details_in_public_without_third_call():
    rows = [detail("午门", EVIDENCE, kind="ENTRY"), detail("太和殿", EVIDENCE),
            detail("神武门", EVIDENCE, kind="EXIT")]
    result, client = await run(SOURCE, "{broken", inline_first(rows))
    assert len(client.calls) == 2
    assert [call["response_format"]["json_schema"]["name"] for call in client.calls] == ["BreezeTravelSemanticDraft"] * 2
    assert main(result) == [["故宫博物院"]]
    assert names(result) == ["入口：午门", "太和殿", "出口：神武门"]
    assert result.resolution_receipt["attempted_count"] == 1
    assert not any(d.category == "SOURCE_VISITS_UNPROCESSED" for d in result.proposal.diagnostics)
    assert result.public_result.coverage.complete


@pytest.mark.asyncio
async def test_partial_first_details_and_new_repair_details_survive_preserved_parent_merge():
    source = "北京。\nDay1：故宫博物院，馆内先看太和殿，再看乾清宫。然后去景山公园。"
    evidence = "故宫博物院，馆内先看太和殿，再看乾清宫"
    first = draft(activity("故宫博物院", source_details=[detail("太和殿", evidence)]),
                  activity("不存在的引文"))
    second = draft(activity("故宫博物院", source_details=[detail("乾清宫", evidence)]), activity("景山公园"))
    result, _ = await run(source, first, second)
    assert main(result) == [["故宫博物院", "景山公园"]]
    assert names(result) == ["太和殿", "乾清宫"]
    assert result.resolution_receipt["attempted_count"] == 2


@pytest.mark.asyncio
async def test_first_details_survive_failed_second_reply_and_bad_sibling_stays_unfinished():
    rows = [detail("太和殿", EVIDENCE), detail("伪造项目", EVIDENCE), {"kind": "VISIT"}, None]
    result, _ = await run(SOURCE, inline_first(rows), "{broken")
    assert main(result) == [["故宫博物院"]] and names(result) == ["太和殿"]
    assert any(d.category == "SOURCE_VISIT_UNRESOLVED" for d in result.proposal.diagnostics)
    assert result.public_result.coverage.complete is False


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[], [detail("不存在", EVIDENCE)]])
async def test_empty_or_invalid_inline_is_not_a_complete_source_visit_result(rows):
    result, _ = await run(SOURCE, "{broken", inline_first(rows))
    assert main(result) == [["故宫博物院"]] and names(result) == []
    assert any(d.category == "SOURCE_VISITS_UNPROCESSED" for d in result.proposal.diagnostics)
    assert not result.public_result.coverage.complete


@pytest.mark.asyncio
async def test_optional_parent_retains_inline_play_experiences_without_main_card_or_poi_calls():
    source = "北京。\nDay1：若有空去青岚乐园，必玩：云海航船、星光环线。"
    item = activity("青岚乐园", source_details=[detail(name, "必玩：云海航船、星光环线")
                    for name in ["云海航船", "星光环线"]])
    item["role"] = "OPTIONAL"
    result, _ = await run(source, "{broken", draft(item))
    assert main(result) == [[]] and result.resolution_receipt["attempted_count"] == 0
    assert [a.name for a in result.public_result.days[0].alternatives] == ["青岚乐园"]
    assert names(result, alternative=True) == ["云海航船", "星光环线"]
    assert all(not row.optional for row in result.public_result.days[0].alternatives[0].source_details)


@pytest.mark.asyncio
async def test_same_parent_name_on_two_days_cannot_borrow_other_visits_details():
    source = "北京。\nDay1：青岚乐园，园内参观云海航船。\nDay2：再访青岚乐园，园内参观星光环线。"
    first = activity("青岚乐园", source_details=[detail("云海航船", "园内参观云海航船")])
    second = activity("青岚乐园", 2, occurrence=2,
        source_details=[detail("星光环线", "园内参观星光环线"), detail("云海航船", "园内参观云海航船")])
    result, _ = await run(source, "{broken", draft(first, second, days=2))
    assert main(result) == [["青岚乐园"], ["青岚乐园"]]
    assert names(result) == ["云海航船"] and names(result, 1) == ["星光环线"]
    assert any(d.category == "SOURCE_VISIT_UNRESOLVED" for d in result.proposal.diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [
    detail("听雨亭", "不参观听雨亭"),
    detail("从南门进", "不从南门进", kind="ENTRY"),
    detail("北门", "从北门出", kind="ENTRY"),
    detail("星光环线", "园内参观星光环线", parent_index=0),
    detail("星光环线", "园内参观星光环线", occurrence=1),
    detail("星光环线", "园内参观星光环线", optional="false"),
])
async def test_invalid_inline_rows_do_not_attach_or_destroy_a_valid_sibling(row):
    source = "北京。\nDay1：青岚乐园，园内参观星光环线，不参观听雨亭，不从南门进，从北门出。"
    good = detail("星光环线", "园内参观星光环线")
    result, _ = await run(source, "{broken", draft(activity("青岚乐园", source_details=[good, row])))
    assert names(result) == ["星光环线"] and main(result) == [["青岚乐园"]]
    assert any(d.category == "SOURCE_VISIT_UNRESOLVED" for d in result.proposal.diagnostics)


@pytest.mark.asyncio
async def test_inline_purpose_belongs_to_exact_parent_and_preserves_parent_days():
    source = "北京。\nDay1：青岚乐园，只看外观。\nDay2：晨光公园，仅取行李，不进园内。"
    result, _ = await run(source, "{broken", draft(
        activity("青岚乐园", source_details=[dict(kind="EXTERIOR_ONLY", optional=False, evidence="青岚乐园，只看外观")]),
        activity("晨光公园", 2, source_details=[dict(kind="PICKUP_ONLY", optional=False, evidence="晨光公园，仅取行李，不进园内")]), days=2))
    assert main(result) == [["青岚乐园"], ["晨光公园"]]
    assert names(result) == ["仅看外观，不入内部"] and names(result, 1) == ["仅取物，不参观"]


def test_bundled_parent_cannot_broadcast_details_and_retains_source_uncertainty_at_80_limit():
    source = "北京。\nDay1：青岚乐园、晨光公园，园内参观星光环线。"
    parsed = SemanticDraft.model_validate(draft(activity("青岚乐园、晨光公园",
        source_details=[detail("星光环线", "园内参观星光环线")])))
    expanded = _expand_source_bound_lists(source, parsed)
    assert [item.place_name for item in expanded.activities] == ["青岚乐园", "晨光公园"]
    assert all(item.source_details == [] for item in expanded.activities)
    assert "青岚乐园、晨光公园" in expanded.unprocessed_quotes
    saturated = parsed.model_copy(update={"unprocessed_quotes": [str(i) for i in range(80)]})
    assert len(_expand_source_bound_lists(source, saturated).unprocessed_quotes) == 80


@pytest.mark.asyncio
async def test_split_parent_public_result_retains_both_roots_without_broadcast_details():
    source = "北京。\nDay1：青岚乐园、晨光公园，园内参观星光环线。"
    result, _ = await run(source, "{broken", draft(activity("青岚乐园、晨光公园",
        source_details=[detail("星光环线", "园内参观星光环线")])))
    assert main(result) == [["青岚乐园", "晨光公园"]]
    assert all(not card.source_details for card in result.public_result.days[0].activities)
    assert not result.public_result.coverage.complete


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [False, True])
async def test_inline_children_share_the_existing_total_160_capacity(overflow):
    labels = [f"青岚{index:02d}乐园" for index in range(32)]
    lines, activities, children = [], [], []
    for index, parent in enumerate(labels):
        names_for_parent = [f"星光项目{index:02d}{j}" for j in range(4 + (overflow and index == 31))]
        evidence = f"{parent}，园内参观" + "、".join(names_for_parent)
        lines.append(evidence + "。")
        activities.append(activity(parent, source_details=[detail(name, evidence) for name in names_for_parent]))
        children.extend(names_for_parent)
    source = "北京。\nDay1：" + "\n".join(lines)
    answer = draft(*activities)
    if overflow:
        with pytest.raises(InferenceProviderUnavailableError) as failure:
            await run(source, "{broken", answer)
        assert str(failure.value) == "INPUT_CAPACITY_EXCEEDED"
        assert failure.value.external_call_count == 2
    else:
        result, _ = await run(source, "{broken", answer)
        assert len(result.proposal.mentions) == 160
        assert main(result) == [labels]
        assert [d.name for card in result.public_result.days[0].activities for d in card.source_details] == children


@pytest.mark.asyncio
async def test_second_partial_keeps_new_valid_details_without_replacing_first_correct_parent():
    source = "北京。\nDay1：故宫博物院，馆内先看太和殿，再看乾清宫。"
    evidence = "故宫博物院，馆内先看太和殿，再看乾清宫"
    first = draft(activity("故宫博物院", source_details=[detail("太和殿", evidence)]), activity("错误引文一"))
    second = draft(activity("故宫博物院", source_details=[detail("乾清宫", evidence)]), activity("错误引文二"))
    result, _ = await run(source, first, second)
    assert main(result) == [["故宫博物院"]]
    assert names(result) == ["太和殿", "乾清宫"]
    assert not result.public_result.coverage.complete


def test_wire_is_typed_and_parent_index_free_while_old_draft_defaults_remain_readable():
    props = provider(Client()).schema["$defs"]["SemanticActivity"]["properties"]
    assert props["source_details"]["maxItems"] == 160
    for shape in props["source_details"]["items"]["anyOf"]:
        assert "parent_index" not in shape["properties"]
        assert "occurrence" not in shape["properties"]
        assert shape["additionalProperties"] is False
        assert {"kind", "optional", "evidence"} <= set(shape["required"])
    assert SemanticDraft.model_validate(FIRST).activities[0].source_details == []
    assert "source_details" in json.dumps(props)


@pytest.mark.asyncio
async def test_broad_evidence_and_one_correct_child_cannot_hide_an_omitted_child_or_parent():
    for source in (
        "北京。\nDay1：故宫博物院，馆内先看太和殿，再看乾清宫。",
        "北京。\nDay1：故宫博物院，馆内先看太和殿。然后去青岚乐园，必玩星光环线。",
    ):
        answer = draft(activity("故宫博物院", source_details=[detail("太和殿", source)]))
        result, _ = await run(source, "{broken", answer)
        assert names(result) == ["太和殿"] and main(result) == [["故宫博物院"]]
        assert any(d.category == "SOURCE_VISITS_UNPROCESSED" for d in result.proposal.diagnostics)
        assert not result.public_result.coverage.complete


@pytest.mark.asyncio
async def test_first_inline_plus_supplement_preserves_internal_order_and_final_reapply_is_idempotent():
    source = "北京。\nDay1：故宫博物院，馆内先看太和殿，再看乾清宫。"
    evidence = "故宫博物院，馆内先看太和殿，再看乾清宫"
    first = draft(activity("故宫博物院", source_details=[detail("太和殿", evidence)]))
    supplement = {"city_fields": [], "source_visits": [dict(parent_index=0, **detail(name, evidence))
        for name in ["太和殿", "乾清宫"]]}
    result, client = await run(source, first, supplement)
    assert len(client.calls) == 2
    assert names(result) == ["太和殿", "乾清宫"] and len(result.proposal.mentions) == 3
    assert result.proposal.unprocessed_count == 0 and result.public_result.coverage.complete


@pytest.mark.asyncio
async def test_empty_supplement_does_not_erase_a_fully_source_covered_inline_answer():
    first = inline_first([detail("午门", EVIDENCE, kind="ENTRY"), detail("太和殿", EVIDENCE),
                          detail("神武门", EVIDENCE, kind="EXIT")])
    result, _ = await run(SOURCE, first, {"city_fields": [], "source_visits": []})
    assert names(result) == ["入口：午门", "太和殿", "出口：神武门"]
    assert not any(d.category == "SOURCE_VISITS_UNPROCESSED" for d in result.proposal.diagnostics)
    assert result.public_result.coverage.complete


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["。", "；", "\n"])
async def test_an_uncovered_same_parent_continuation_never_clears_global_pending(separator):
    source = "北京。\nDay1：青岚乐园，园内参观星光环线" + separator + "随后体验云海航船。"
    first = draft(activity("青岚乐园", source_details=[detail("星光环线", "园内参观星光环线")]))
    result, _ = await run(source, "{broken", first)
    assert main(result) == [["青岚乐园"]] and names(result) == ["星光环线"]
    assert any(d.category == "SOURCE_VISITS_UNPROCESSED" for d in result.proposal.diagnostics)
    assert not result.public_result.coverage.complete


@pytest.mark.asyncio
async def test_uncovered_next_day_without_a_repeated_internal_cue_stays_pending():
    source = "北京。\nDay1：青岚乐园，园内参观星光环线。\nDay2：晨光公园，体验云海航船。"
    first = draft(activity("青岚乐园", source_details=[detail("星光环线", "园内参观星光环线")]),
                  activity("晨光公园", 2), days=2)
    result, _ = await run(source, "{broken", first)
    assert main(result) == [["青岚乐园"], ["晨光公园"]]
    assert names(result) == ["星光环线"] and names(result, 1) == []
    assert any(d.category == "SOURCE_VISITS_UNPROCESSED" for d in result.proposal.diagnostics)
    assert not result.public_result.coverage.complete


@pytest.mark.asyncio
async def test_an_internal_name_misclassified_as_root_is_not_proof_of_internal_coverage():
    source = "北京。\nDay1：青岚乐园，园内参观星光环线、云海航船。"
    first = draft(activity("青岚乐园", source_details=[detail("星光环线", "园内参观星光环线、云海航船")]),
                  activity("云海航船"))
    result, _ = await run(source, "{broken", first)
    # The inline connection must neither rewrite the model's root role nor
    # count that wrong independent card as a validated internal arrangement.
    assert main(result) == [["青岚乐园", "云海航船"]] and names(result) == ["星光环线"]
    assert any(d.category == "SOURCE_VISITS_UNPROCESSED" for d in result.proposal.diagnostics)
    assert not result.public_result.coverage.complete


@pytest.mark.asyncio
async def test_inline_optional_reconciliation_is_reflected_in_the_second_parent_table():
    source = "北京。\nDay1：青岚乐园，若有空园内参观云海航船。"
    row = detail("云海航船", "若有空园内参观云海航船", optional=True)
    child = activity("云海航船")
    child["role"] = "OPTIONAL"
    first = draft(activity("青岚乐园", source_details=[row]), child)
    result, client = await run(source, first, {"city_fields": [], "source_visits": [dict(parent_index=0, **row)]})
    table = json.loads(client.calls[1]["messages"][-1]["content"])["parents"]
    assert [parent["name"] for parent in table] == ["青岚乐园"]
    assert main(result) == [["青岚乐园"]] and names(result) == ["云海航船"]
    assert not result.public_result.days[0].alternatives


@pytest.mark.asyncio
async def test_unknown_instruction_before_the_first_internal_cue_stays_unfinished():
    source = "北京。\nDay1：青岚乐园，先体验云海航船。\n园内参观星光环线。"
    first = draft(activity("青岚乐园", source_details=[detail("星光环线", "园内参观星光环线")]))
    result, _ = await run(source, "{broken", first)
    assert main(result) == [["青岚乐园"]] and names(result) == ["星光环线"]
    assert any(d.category == "SOURCE_VISITS_UNPROCESSED" for d in result.proposal.diagnostics)
    assert not result.public_result.coverage.complete
