"""Source warning locations follow explicit sections, not inline day mentions."""
import pytest

from app.trip_understanding.experience_inference import _coverage_day_scopes
from tests.test_semantic_partial_recovery import coverage_from_literal_hints


def day_at(source, text):
    start = source.index(text)
    return next((day for day, left, right in _coverage_day_scopes(source)
                 if left <= start < start + len(text) <= right), None)


def test_markdown_day_body_keeps_multiple_paragraphs_despite_inline_reference():
    source = ("## Day1｜宫苑\n\n1. 星河公园。\n\n2. 月光桥。\n"
              "## Day2｜城中\n\n1. 晨光湖。\n如果不走原方案，可以把Day2替换为室内游。\n"
              "## Day3｜博物馆\n\n1. 落日亭。\n\n2. 清溪馆。")
    names = ["星河公园", "月光桥", "晨光湖", "落日亭", "清溪馆"]
    assert [day_at(source, name) for name in names] == [1, 1, 2, 3, 3]
    result = coverage_from_literal_hints(source, names)
    assert result.unprocessed_count == 5
    assert result.unprocessed_by_day == {1: 2, 2: 1, 3: 2}
    assert result.mentions == []  # Warning locations cannot manufacture visits.


@pytest.mark.parametrize("heading", ["## 全程补充", "# 其他建议"])
def test_same_or_higher_markdown_section_does_not_inherit_last_day(heading):
    source = "## Day1｜城中\n星河公园。\n## Day2｜湖边\n月光桥。\n" + heading + "\n晨光湖。"
    assert day_at(source, "晨光湖") is None
    result = coverage_from_literal_hints(source, ["星河公园", "月光桥", "晨光湖"])
    assert result.unprocessed_count == 3 and result.unprocessed_by_day == {1: 1, 2: 1}


def test_nested_day_subsection_remains_in_same_day_and_markdown_is_optional():
    source = "## **Day1**\n### 上午\n星河公园。\n### 下午\n月光桥。\n## **Day2**\n晨光湖。"
    assert [day_at(source, name) for name in ["星河公园", "月光桥", "晨光湖"]] == [1, 1, 2]


@pytest.mark.parametrize("source", [
    "Day1：星河公园。Day2：月光桥。",
    "## Day1\n星河公园。\n## Day2\n月光桥。\n## Day1\n总结。",
    "## Day1\n星河公园。\n## Day2\n月光桥。\n更正：两天安排对调。",
    "## Day1-2\n星河公园。",
    "## Day1.5\n星河公园。\n## Day2\n月光桥。",
    "## Day1\n星河公园。\n## Day2\n月光桥。\n把第二天提前一天，并重新预约。",
])
def test_ambiguous_or_changed_schedule_keeps_existing_global_location(source):
    assert _coverage_day_scopes(source) == []


@pytest.mark.parametrize("footer", ["其他建议：清溪馆哪天有空再去。", "全程备选：清溪馆。",
                                  "**通用提醒：** 清溪馆可参考。", "其他备选\n清溪馆哪天有空再去。",
                                  "### 其他备选：\n清溪馆哪天有空再去。", "全程备选\n清溪馆。"])
def test_unscoped_plain_footer_inside_markdown_does_not_claim_the_last_day(footer):
    source = "## Day1\n星河公园。\n## Day2\n月光桥。\n\n" + footer
    assert day_at(source, "清溪馆") is None
    result = coverage_from_literal_hints(source, ["星河公园", "月光桥", "清溪馆"])
    assert result.unprocessed_count == 3 and result.unprocessed_by_day == {1: 1, 2: 1}


def test_ticket_lead_time_and_checking_todays_schedule_do_not_move_visits():
    source = ("## Day1｜参观\n星河公园，想看升旗提前查当日升旗时间。\n"
              "月光桥（提前 7 天 20 点抢票），北门出。\n## Day2｜湖边\n晨光湖。")
    assert [day_at(source, name) for name in ["星河公园", "月光桥", "晨光湖"]] == [1, 1, 2]
    result = coverage_from_literal_hints(source, ["星河公园", "月光桥", "晨光湖"])
    assert result.unprocessed_count == 3 and result.unprocessed_by_day == {1: 2, 2: 1}
