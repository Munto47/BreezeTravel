"""A later optional clause cannot cancel an earlier mandatory visit or gate."""
import pytest

from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_source_visit_local_evidence_contract import apply, location
from tests.test_source_visit_supplement import plan


# Literal source paragraph and the new answer's broad evidence from
# beijing-local-evidence-second-live-v1.json; only the surrounding day heading
# is reduced here. This is a fixed adapter regression, not another model run.
PALACE = "**故宫博物院**（旺季 60 元，提前 7 天 20 点抢票，午门进、神武门出），重点：太和殿、乾清宫、御花园；时间充裕加珍宝馆、钟表馆（另付费）携程。"
LONG_EVIDENCE = "故宫博物院（旺季 60 元，提前 7 天 20 点抢票，午门进、神武门出），重点：太和殿、乾清宫、御花园；时间充裕加珍宝馆、钟表馆（另付费）携程。"
VISIT_EVIDENCE = "重点：太和殿、乾清宫、御花园；时间充裕加珍宝馆、钟表馆（另付费）携程。"


@pytest.mark.asyncio
async def test_recorded_broad_evidence_keeps_five_mandatory_details_and_two_conditional_siblings():
    source = "北京。\nDay1：" + PALACE
    before = plan(source, [("故宫博物院", 1, 1)])
    rows = [location(name, "VISIT", VISIT_EVIDENCE, optional=optional) for name, optional in (
        ("太和殿", False), ("乾清宫", False), ("御花园", False), ("珍宝馆", True), ("钟表馆", True))]
    rows += [location("午门", "ENTRY", LONG_EVIDENCE), location("神武门", "EXIT", LONG_EVIDENCE)]
    result = apply(source, before, rows)
    assert result.mentions[0] == before.mentions[0] and result.unprocessed_count == 0
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=result)
    assert len(output.public_result.days[0].activities) == 1
    assert output.resolution_receipt["attempted_count"] == 1
    assert [d.model_dump() for d in output.public_result.days[0].activities[0].source_details] == [
        dict(name="入口：午门", optional=False), dict(name="太和殿", optional=False),
        dict(name="乾清宫", optional=False), dict(name="御花园", optional=False),
        dict(name="珍宝馆", optional=True), dict(name="钟表馆", optional=True), dict(name="出口：神武门", optional=False)]
    for name in ("珍宝馆", "钟表馆"):
        wrong = apply(source, before, [location(name, "VISIT", VISIT_EVIDENCE)])
        assert wrong.mentions == before.mentions and wrong.unprocessed_count > 0


@pytest.mark.parametrize("text,quote,evidence,kind", [
    ("馆内若有时间，先看晨光亭、晚晴亭。", "晚晴亭", "先看晨光亭、晚晴亭", "VISIT"),
    ("馆内若有时间：先看晨光亭；再看晚晴亭。", "晚晴亭", "再看晚晴亭", "VISIT"),
    ("若有时间，园内先看晨光亭；再看晚晴亭。", "晚晴亭", "再看晚晴亭", "VISIT"),
    ("馆内如果下雨：先看晨光亭；再看晚晴亭。", "晚晴亭", "再看晚晴亭", "VISIT"),
    ("馆内如果展厅开放：先看晨光亭；再看晚晴亭。", "晚晴亭", "再看晚晴亭", "VISIT"),
    ("馆内如果人少，先看晨光亭；再看晚晴亭。", "晚晴亭", "再看晚晴亭", "VISIT"),
    ("若体力允许：从南门进，最后从北门出。", "南门", "从南门进", "ENTRY"),
    ("若体力允许：从南门进，最后从北门出。", "北门", "最后从北门出", "EXIT"),
])
def test_preposed_condition_cannot_be_hidden_by_short_evidence_or_comma(text, quote, evidence, kind):
    source = "北京。\nDay1：青溪公园，" + text
    before = plan(source, [("青溪公园", 1, 1)])
    after = apply(source, before, [location(quote, kind, evidence)])
    assert after.mentions == before.mentions and after.unprocessed_count > 0
    accepted = apply(source, before, [location(quote, kind, evidence, optional=True)])
    assert len(accepted.mentions) == len(before.mentions) + 1


def test_long_preposed_condition_has_no_extra_arbitrary_length_limit():
    condition = "如果当天实际下雨并且场馆已发布临时开放通知且现场工作人员确认当前可以进入且已经完成预约"
    assert len(condition) > 40
    source = "北京。\nDay1：青溪公园，馆内" + condition + "：先看晨光亭；再看晚晴亭。"
    before = plan(source, [("青溪公园", 1, 1)])
    after = apply(source, before, [location("晚晴亭", "VISIT", "再看晚晴亭")])
    assert after.mentions == before.mentions and after.unprocessed_count > 0


@pytest.mark.parametrize("condition", ["若有时间再看", "时间充裕再参观"])
def test_objectless_postposed_condition_still_governs_the_previous_location(condition):
    source = "北京。\nDay1：青溪公园，园内晨光亭，" + condition + "。"
    before = plan(source, [("青溪公园", 1, 1)])
    evidence = "园内晨光亭，" + condition
    wrong = apply(source, before, [location("晨光亭", "VISIT", evidence)])
    assert wrong.mentions == before.mentions and wrong.unprocessed_count > 0
    correct = apply(source, before, [location("晨光亭", "VISIT", evidence, optional=True)])
    assert len(correct.mentions) == len(before.mentions) + 1


@pytest.mark.parametrize("text,quote,evidence,kind", [
    ("馆内先看晨光亭，若有时间再看晚晴亭。", "晨光亭", "馆内先看晨光亭，若有时间再看晚晴亭", "VISIT"),
    ("馆内若有时间看晚晴亭；之后必须看晨光亭。", "晨光亭", "馆内若有时间看晚晴亭；之后必须看晨光亭", "VISIT"),
    ("从南门进、北门出；若有时间看晨光亭。", "南门", "从南门进、北门出；若有时间看晨光亭", "ENTRY"),
    ("从南门进、北门出；若有时间看晨光亭。", "北门", "从南门进、北门出；若有时间看晨光亭", "EXIT"),
])
def test_another_conditional_clause_does_not_change_a_mandatory_location(text, quote, evidence, kind):
    source = "北京。\nDay1：青溪公园，" + text
    before = plan(source, [("青溪公园", 1, 1)])
    after = apply(source, before, [location(quote, kind, evidence)])
    assert len(after.mentions) == len(before.mentions) + 1 and after.unprocessed_count == 0


@pytest.mark.parametrize("text,quote,evidence,kind", [
    ("园内取消参观晨光亭；若有时间看晚晴亭。", "晨光亭", "晨光亭", "VISIT"),
    ("不**从**南门进，从北门进；时间充裕看晨光亭。", "南门", "南门进", "ENTRY"),
    ("从南门进，不从北门出；时间充裕看晨光亭。", "北门", "北门出", "EXIT"),
    ("从南门出；时间充裕看晨光亭。", "南门", "从南门出", "ENTRY"),
])
def test_local_cancellation_negation_and_direction_remain_strict(text, quote, evidence, kind):
    source = "北京。\nDay1：青溪公园，" + text
    before = plan(source, [("青溪公园", 1, 1)])
    after = apply(source, before, [location(quote, kind, evidence, optional=True)])
    assert after.mentions == before.mentions and after.unprocessed_count > 0
