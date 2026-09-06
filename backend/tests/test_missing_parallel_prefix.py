"""Missing bold-list prefixes recover only from the exact existing member."""

from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    _expand_source_bound_lists,
    proposal_from_draft,
)
from app.trip_understanding.pipeline import EvidenceCompiler


NAMES = ["星河公园", "青溪博物馆", "望星塔"]


def _activity(name, *, quote=None, day=1, role="PLANNED", occurrence=1, **values):
    return {"source_quote": quote or name, "place_name": name, "day_index": day,
            "role": role, "occurrence": occurrence, "category": "景点", **values}


def _draft(rows):
    return SemanticDraft.model_validate({"destination": "北京", "activities": rows})


@pytest.mark.parametrize("kept_index", [1, 2])
@pytest.mark.parametrize("role", ["PLANNED", "OPTIONAL"])
def test_only_the_second_or_third_member_still_preserves_every_literal_list_member(kept_index, role):
    source = "Day1：**" + "、".join(NAMES) + "**。"
    proposal = proposal_from_draft(source, _draft([_activity(NAMES[kept_index], role=role)]))
    assert [mention.atomic_place_name for mention in proposal.mentions] == NAMES
    assert [mention.role.value for mention in proposal.mentions] == [role] * 3
    assert [mention.day_index for mention in proposal.mentions] == [1] * 3
    assert [mention.span_start for mention in proposal.mentions] == [source.index(name) for name in NAMES]
    assert all(source[mention.span_start:mention.span_end] == mention.raw_text == name
               for mention, name in zip(proposal.mentions, NAMES, strict=True))
    compiled = EvidenceCompiler().compile(source, proposal)[0]
    assert [item.eligible_for_place_search for item in compiled] == [role == "PLANNED"] * 3


@pytest.mark.parametrize("count", [2, 3])
def test_a_recovered_prefix_never_takes_the_existing_last_members_time(count):
    names = NAMES[:count]
    source = "Day1：**" + "、".join(names) + "**，最后一站10:00已预约，停留60分钟。"
    draft = _draft([_activity(names[-1], start_time="10:00", visit_duration_minutes=60,
                             timing_source="TEXT", locked=True, fixed_commitment=True,
                             time_evidence=source)])
    expanded = _expand_source_bound_lists(source, draft)
    assert [item.place_name for item in expanded.activities] == names
    assert expanded.activities[-1].start_time == "10:00"
    assert expanded.activities[-1].visit_duration_minutes == 60
    assert expanded.activities[-1].locked and expanded.activities[-1].fixed_commitment
    assert expanded.activities[-1].time_evidence == source
    for added in expanded.activities[:-1]:
        assert added.start_time is None and added.end_time is None
        assert added.visit_duration_minutes is None and added.time_evidence is None
        assert not added.locked and not added.fixed_commitment
    assert len(draft.activities) == 1
    proposal = proposal_from_draft(source, draft)
    assert [mention.start_time for mention in proposal.mentions] == [None] * (count - 1) + ["10:00"]
    assert proposal.mentions[-1].visit_duration_minutes == 60
    assert proposal.mentions[-1].locked and proposal.mentions[-1].fixed_commitment


@pytest.mark.parametrize("other_role", ["REFERENCE", "EXCLUDED", "OPTIONAL"])
def test_separately_classified_members_do_not_supply_a_role_for_the_missing_member(other_role):
    source = "Day1：**" + "、".join(NAMES) + "**。"
    draft = _draft([_activity(NAMES[0], role=other_role), _activity(NAMES[1])])
    assert _expand_source_bound_lists(source, draft) == draft
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(source, draft)
    assert "MISSING_EXPLICIT_PARALLEL_PLACE" in {issue["category"] for issue in error.value.issues}


@pytest.mark.parametrize("prefix,suffix", [
    ("不去", "。"),
    ("取消", "。"),
    ("备选", "。"),
    ("二选一：", "。"),
    ("参考：", "。"),
    ("", "。\n更正：只去青溪博物馆。"),
    ("", "。\n两个只选青溪博物馆。"),
])
def test_negation_choices_references_and_amendments_do_not_borrow_a_members_role(prefix, suffix):
    source = "Day1：" + prefix + "**星河公园、青溪博物馆**" + suffix
    draft = _draft([_activity("青溪博物馆")])
    assert _expand_source_bound_lists(source, draft) == draft
    try:
        proposal = proposal_from_draft(source, draft)
    except SourceAnchorValidationError:
        return
    assert [mention.atomic_place_name for mention in proposal.mentions] == ["青溪博物馆"]


@pytest.mark.parametrize("quote", ["星河公园、青溪博物馆", "前往星河公园、青溪博物馆"])
def test_a_wide_quote_is_not_the_missing_members_exact_list_anchor(quote):
    source = "Day1：前往**星河公园、青溪博物馆**。"
    draft = _draft([_activity("青溪博物馆", quote=quote)])
    assert _expand_source_bound_lists(source, draft) == draft
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(source, draft)
    assert "MISSING_EXPLICIT_PARALLEL_PLACE" in {issue["category"] for issue in error.value.issues}


@pytest.mark.parametrize("source", [
    "Day1：介绍青溪博物馆。\nDay2：**星河公园、青溪博物馆**。",
    "Day1：介绍青溪博物馆。\n**星河公园、青溪博物馆**。",
])
def test_an_earlier_same_name_occurrence_cannot_fill_a_later_bold_group(source):
    draft = _draft([_activity("青溪博物馆", occurrence=1)])
    assert _expand_source_bound_lists(source, draft) == draft
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(source, draft)
    assert "MISSING_EXPLICIT_PARALLEL_PLACE" in {issue["category"] for issue in error.value.issues}


def test_a_correct_second_day_occurrence_does_not_borrow_the_first_days_source():
    source = "Day1：介绍青溪博物馆。\nDay2：**星河公园、青溪博物馆**。"
    proposal = proposal_from_draft(source, _draft([_activity("青溪博物馆", day=2, occurrence=2)]))
    assert [mention.atomic_place_name for mention in proposal.mentions] == NAMES[:2]
    assert [mention.day_index for mention in proposal.mentions] == [2, 2]
    assert proposal.mentions[-1].span_start == source.rindex("青溪博物馆")
