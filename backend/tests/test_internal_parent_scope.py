"""A parent name and a literal quote alone do not establish an internal visit."""
import pytest

from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces


def activity(name, day=1, **fields):
    return dict(source_quote=name, place_name=name, day_index=day, role="PLANNED",
        city="北京", city_evidence="北京", category="景点") | fields


def plan(source, rows):
    return _proposal_from_live_draft(source, SemanticDraft.model_validate(
        dict(destination="北京", activities=rows)))


def invalid_cases():
    source = "北京。\nDay1：故宫博物院，园内参观太和殿。之后去景山公园，上万春亭俯瞰故宫全景。"
    base = [activity("故宫博物院"), activity("太和殿", role="REFERENCE", parent_source_quote="故宫博物院",
        role_evidence="园内参观太和殿"), activity("景山公园")]
    yield source, base + [activity("万春亭", role="REFERENCE", parent_source_quote="故宫博物院",
        role_evidence="上万春亭俯瞰故宫全景")], "万春亭"
    yield source, base + [activity("万春亭", parent_source_quote="故宫博物院",
        role_evidence="上万春亭俯瞰故宫全景")], "万春亭"
    yield source, base + [activity("故宫", occurrence=2, role="REFERENCE", parent_source_quote="景山公园",
        role_evidence="上万春亭俯瞰故宫全景")], "故宫"
    yield "北京。\nDay1：故宫博物院。\nDay2：景山公园，上万春亭俯瞰故宫全景。", [
        activity("故宫博物院"), activity("景山公园", 2),
        activity("万春亭", role="REFERENCE", parent_source_quote="故宫博物院",
            role_evidence="上万春亭俯瞰故宫全景")], "万春亭"
    yield "北京。\nDay1：故宫博物院，之后去太和殿。", [activity("故宫博物院"),
        activity("太和殿", parent_source_quote="故宫博物院", role_evidence="之后去太和殿")], "太和殿"
    yield "北京。\nDay1：故宫博物院。\n其他建议：园内参观太和殿。", [activity("故宫博物院"),
        activity("太和殿", parent_source_quote="故宫博物院", role_evidence="园内参观太和殿")], "太和殿"
    yield "北京。\nDay1：故宫博物院、景山公园\n园内参观太和殿。", [activity("故宫博物院"), activity("景山公园"),
        activity("太和殿", parent_source_quote="景山公园", role_evidence="园内参观太和殿")], "太和殿"
    yield "北京。\nDay1：景山公园，园内路线：万春亭，俯瞰故宫全景。", [activity("景山公园"),
        activity("万春亭"), activity("故宫")], "故宫"
    yield "北京。\nDay1—2：故宫博物院，园内参观太和殿。", [activity("故宫博物院"),
        activity("太和殿", role="REFERENCE", parent_source_quote="故宫博物院",
            role_evidence="园内参观太和殿")], "太和殿"
    yield "北京。\nDay1：故宫博物院，园内参观太和殿。随后乘车到景山公园。", [activity("故宫博物院"),
        activity("景山公园", role="REFERENCE", parent_source_quote="故宫博物院",
            role_evidence="随后乘车到景山公园")], "景山公园"
    yield "北京。\nDay1：国家博物馆、景山公园\n1. 国家博物馆，景山公园，园内参观万春亭。", [
        activity("国家博物馆"), activity("景山公园", occurrence=2), activity("万春亭", role="REFERENCE",
            parent_source_quote="国家博物馆", role_evidence="园内参观万春亭")], "万春亭"


@pytest.mark.parametrize("source,rows,name", list(invalid_cases()))
@pytest.mark.asyncio
async def test_uncertain_parent_remains_unprocessed_without_an_extra_main_stop(source, rows, name):
    proposal = plan(source, rows)
    child = next(item for item in reversed(proposal.mentions) if item.atomic_place_name == name)
    assert child.parent_mention_id is None
    assert child.role.value == "REFERENCE"
    assert any(issue.category == "PARENT_RELATION_UNRESOLVED" and issue.span_start == child.span_start
        for issue in proposal.diagnostics)
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=proposal)
    assert not output.public_result.coverage.complete
    assert output.public_result.coverage.unprocessed_count > 0
    assert all(detail.name != name for day in output.public_result.days for card in day.activities
        for detail in card.source_details)
    assert all(card.name != name for day in output.public_result.days for card in day.activities)


@pytest.mark.parametrize("source,evidence", [
    ("北京。\nDay1：故宫博物院，园内参观太和殿。", "园内参观太和殿"),
    ("北京。\nDay1：故宫博物院\n园内路线：参观太和殿。", "园内路线：参观太和殿"),
    ("北京。\nDay1：故宫博物院，园内先看乾清宫；接着看太和殿。", "接着看太和殿"),
    ("北京。\nDay1：故宫博物院内参观太和殿。", "参观太和殿"),
    ("北京。\nDay1：故宫博物院，园内路线：乾清宫和太和殿。", "和太和殿"),
    ("北京。\nDay1：故宫博物院（买联票），打卡太和殿。", "打卡太和殿"),
])
def test_explicit_internal_source_scope_keeps_the_parent(source, evidence):
    proposal = plan(source, [activity("故宫博物院"), activity("太和殿", role="REFERENCE",
        parent_source_quote="故宫博物院", role_evidence=evidence)])
    assert proposal.mentions[1].parent_mention_id == proposal.mentions[0].mention_id
    assert proposal.unprocessed_count == 0


@pytest.mark.asyncio
async def test_optional_child_uses_parent_scope_across_local_sentences():
    source = "北京。\nDay1：故宫博物院，园内先参观太和殿；时间足够可看珍宝馆。"
    proposal = plan(source, [activity("故宫博物院"), activity("太和殿", role="REFERENCE",
        parent_source_quote="故宫博物院", role_evidence="园内先参观太和殿"),
        activity("珍宝馆", role="OPTIONAL", parent_source_quote="故宫博物院", role_evidence="时间足够可看珍宝馆")])
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=proposal)
    assert [card.name for card in output.public_result.days[0].activities] == ["故宫博物院"]
    assert [detail.model_dump() for detail in output.public_result.days[0].activities[0].source_details] == [
        {"name": "太和殿", "optional": False}, {"name": "珍宝馆", "optional": True}]
    assert output.public_result.coverage.complete


def test_repeated_parent_visits_bind_each_child_to_its_actual_day():
    source = "北京。\nDay1：故宫博物院，园内参观太和殿。\nDay2：再访故宫博物院，园内参观乾清宫。"
    proposal = plan(source, [activity("故宫博物院"), activity("太和殿", role="REFERENCE",
        parent_source_quote="故宫博物院", role_evidence="园内参观太和殿"),
        activity("故宫博物院", 2, occurrence=2), activity("乾清宫", 2, role="REFERENCE",
        parent_source_quote="故宫博物院", role_evidence="园内参观乾清宫")])
    assert proposal.mentions[1].parent_mention_id == proposal.mentions[0].mention_id
    assert proposal.mentions[3].parent_mention_id == proposal.mentions[2].mention_id
    assert proposal.unprocessed_count == 0


@pytest.mark.parametrize("parent,child,body,evidence", [
    ("景山公园", "万春亭", "上万春亭俯瞰故宫全景", "上万春亭俯瞰故宫全景"),
    ("国家博物馆", "古代中国展厅", "建议2-3小时，看古代中国展厅", "看古代中国展厅"),
    ("天坛公园", "回音壁", "打卡祈年殿、回音壁", "打卡祈年殿、回音壁"),
    ("国家博物馆", "书画馆", "馆内先去青铜馆，再去书画馆", "再去书画馆"),
])
def test_local_visit_action_does_not_require_a_place_suffix_or_internal_keyword(parent, child, body, evidence):
    source = f"北京。\nDay1：{parent}，{body}。"
    proposal = plan(source, [activity(parent), activity(child, role="REFERENCE",
        parent_source_quote=parent, role_evidence=evidence)])
    assert proposal.mentions[1].parent_mention_id == proposal.mentions[0].mention_id
    assert proposal.unprocessed_count == 0


def test_parent_in_day_overview_uses_its_unique_numbered_body_for_details():
    source = "北京。\nDay1：天坛公园—国家博物馆\n1. 天坛公园，游览公园。\n2. 国家博物馆，建议2-3小时，看古代中国展厅。"
    proposal = plan(source, [activity("天坛公园", occurrence=2), activity("国家博物馆"),
        activity("古代中国展厅", role="REFERENCE", parent_source_quote="国家博物馆", role_evidence="看古代中国展厅")])
    parent = next(item for item in proposal.mentions if item.atomic_place_name == "国家博物馆")
    child = next(item for item in proposal.mentions if item.atomic_place_name == "古代中国展厅")
    assert parent.span_start == source.index("国家博物馆")
    assert child.parent_mention_id == parent.mention_id
    assert proposal.unprocessed_count == 0


def test_rejected_parent_does_not_erase_an_exclusion():
    source = "北京。\nDay1：故宫博物院，取消珍宝馆。"
    proposal = plan(source, [activity("故宫博物院"), activity("珍宝馆", role="EXCLUDED",
        parent_source_quote="景山公园", role_evidence="取消珍宝馆")])
    child = proposal.mentions[1]
    assert child.parent_mention_id is None
    assert child.role.value == "EXCLUDED"
    assert proposal.unprocessed_count == 1
