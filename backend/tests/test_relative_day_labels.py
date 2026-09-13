import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft


def plan(source, labels):
    return proposal_from_draft(source, SemanticDraft(
        destination="北京", day_labels=labels,
        activities=[{"source_quote": "故宫", "place_name": "故宫", "role": "PLANNED", "day_index": 1}],
    ))


@pytest.mark.parametrize(("heading", "label"), [
    ("Day1", "Day 1"), ("Day1", "day1"), ("day 1", "DAY1"),
    ("D1", "d 1"), ("**Day1**", "Day 1"), ("Day 1", "**DAY1**"),
    ("第一天", "第 一 天"), ("第 1 天", "第一天"),
])
def test_equivalent_explicit_relative_day_labels_do_not_invent_unprocessed_content(heading, label):
    result = plan(f"北京\n{heading}：故宫。", [label])
    assert result.unprocessed_count == 0
    assert result.day_count == 1 and result.day_labels == {}
    assert [(m.atomic_place_name, m.day_index) for m in result.mentions] == [("故宫", 1)]


@pytest.mark.parametrize(("source", "label"), [
    ("北京\nDay1：故宫。", "Day2"),
    ("北京\nDay10：故宫。", "Day1"),
    ("北京\nHoliday1：故宫。", "Day1"),
    ("北京\nDay1：故宫。", "**Day1"),
    ("北京\nDay1：故宫。", "2026年9月8日"),
    ("北京一日游：故宫。", "Day1"),
])
def test_label_normalization_cannot_invent_a_day_or_calendar_date(source, label):
    result = plan(source, [label])
    assert result.unprocessed_count >= 1
    assert any(d.category == "UNSUPPORTED_DAY_LABEL_REMOVED" for d in result.diagnostics)
    assert result.day_labels == {}


def test_label_for_a_different_index_is_not_accepted_even_when_present_in_source():
    result = plan("北京\nDay1：故宫。\nDay2：休息。", ["Day 2", "Day 1"])
    assert result.unprocessed_count == 2
    assert result.day_count == 2
