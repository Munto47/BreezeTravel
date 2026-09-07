"""Original synthetic guides preserve separate visits to the same named street."""

from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    _retain_explicit_optional_labels,
    proposal_from_draft,
)
from app.trip_understanding.pipeline import EvidenceCompiler


def _activity(name, *, day=1, role="PLANNED", occurrence=1, quote=None, **values):
    return {"source_quote": quote or name, "place_name": name, "day_index": day,
            "role": role, "occurrence": occurrence, "category": "地点", **values}


def _draft(rows):
    return SemanticDraft.model_validate({"destination": "上海", "activities": rows})


@pytest.mark.parametrize("heading,mark,newline", [
    ("", "", "\n"),
    ("## ", "**", "\n"),
    ("  ", "", "\r\n"),
    ("  ## ", "**", "\r\n"),
])
def test_walk_then_meal_choice_retains_each_literal_occurrence_in_every_format(heading, mark, newline):
    source = newline.join([
        f"{heading}Day1：上海街区",
        f"- 09:15：{mark}枫溪路{mark}，停留30分钟。",
        f"- 中午：{mark}青禾路{mark} / {mark}枫溪路{mark}吃面。",
        "- 下午：星海公园。",
    ])
    timing = f"09:15：{mark}枫溪路{mark}，停留30分钟"
    draft = _draft([
        _activity("枫溪路", start_time="09:15", visit_duration_minutes=30,
                  timing_source="TEXT", time_evidence=timing,
                  city="上海", city_evidence="上海"),
        _activity("青禾路", role="OPTIONAL"),
        _activity("星海公园"),
    ])
    recovered = _retain_explicit_optional_labels(source, draft)
    assert [item.place_name for item in recovered.activities] == ["枫溪路", "青禾路", "枫溪路", "星海公园"]
    assert [item.role.value for item in recovered.activities] == ["PLANNED", "OPTIONAL", "OPTIONAL", "PLANNED"]
    assert recovered.activities[0] == draft.activities[0]
    assert recovered.activities[1] == draft.activities[1]
    assert recovered.activities[3] == draft.activities[2]
    assert len(draft.activities) == 3
    added = recovered.activities[2]
    assert (added.source_quote, added.occurrence, added.day_index) == ("枫溪路", 2, 1)
    assert added.start_time is None and added.end_time is None and added.visit_duration_minutes is None
    assert added.time_evidence is None and added.timing_source == "UNSPECIFIED"
    assert added.city is None and added.city_evidence is None
    assert not added.locked and not added.fixed_commitment
    assert _retain_explicit_optional_labels(source, recovered) == recovered

    proposal = proposal_from_draft(source, draft)
    assert [item.atomic_place_name for item in proposal.mentions] == ["枫溪路", "青禾路", "枫溪路", "星海公园"]
    assert [item.span_start for item in proposal.mentions] == [
        source.index("枫溪路"), source.index("青禾路"), source.rindex("枫溪路"), source.index("星海公园"),
    ]
    assert [item.start_time for item in proposal.mentions] == ["09:15", None, None, None]
    compiled = EvidenceCompiler().compile(source, proposal)[0]
    assert [item.eligible_for_place_search for item in compiled] == [True, False, False, True]


def test_missing_first_meal_choice_is_inserted_before_the_existing_second_choice():
    source = "Day1\n上午：枫溪路。\n中午：枫溪路 / 青禾路吃面。\n下午：星海公园。"
    draft = _draft([_activity("枫溪路"), _activity("青禾路", role="OPTIONAL"), _activity("星海公园")])
    proposal = proposal_from_draft(source, draft)
    assert [item.atomic_place_name for item in proposal.mentions] == ["枫溪路", "枫溪路", "青禾路", "星海公园"]
    assert [item.span_start for item in proposal.mentions] == [
        source.index("枫溪路"), source.rindex("枫溪路"), source.index("青禾路"), source.index("星海公园"),
    ]
    assert [item.role.value for item in proposal.mentions] == ["PLANNED", "OPTIONAL", "OPTIONAL", "PLANNED"]


@pytest.mark.parametrize("role", ["PLANNED", "OPTIONAL", "REFERENCE", "EXCLUDED", "PASS_THROUGH"])
def test_an_existing_claim_at_the_optional_occurrence_is_never_duplicated(role):
    source = "Day1\n上午：枫溪路。\n中午：青禾路 / 枫溪路吃面。"
    draft = _draft([
        _activity("枫溪路"), _activity("青禾路", role="OPTIONAL"),
        _activity("枫溪路", occurrence=2, role=role),
    ])
    assert _retain_explicit_optional_labels(source, draft) == draft


@pytest.mark.parametrize("quote", ["枫溪路", "枫溪", "枫溪路吃面"])
def test_an_overlapping_anonymous_claim_does_not_lend_its_role_to_a_new_option(quote):
    source = "Day1\n上午：枫溪路。\n中午：青禾路 / 枫溪路吃面。"
    occurrence = 2 if quote in {"枫溪路", "枫溪"} else 1
    draft = _draft([
        _activity("枫溪路"), _activity("青禾路", role="OPTIONAL"),
        _activity(None, quote=quote, role="REFERENCE", occurrence=occurrence),
    ])
    assert _retain_explicit_optional_labels(source, draft) == draft


@pytest.mark.parametrize("tail", [
    "中午：青禾路 / 枫溪路吃面，但不要去枫溪路。",
    "中午：青禾路 / 枫溪路吃面。\n取消午餐安排。",
    "中午：青禾路 / 枫溪路吃面。\n已选方案A。",
    "中午：青禾路 / 枫溪路吃面。\n最终决定只去青禾路。",
    "中午：青禾路 / 枫溪路吃面。\n更正：午餐改到第三天。",
])
def test_negation_cancellation_and_a_decision_never_restore_the_old_meal_option(tail):
    source = "Day1\n上午：枫溪路。\n" + tail
    draft = _draft([_activity("枫溪路"), _activity("青禾路", role="OPTIONAL")])
    assert _retain_explicit_optional_labels(source, draft) == draft
    try:
        proposal = proposal_from_draft(source, draft)
    except SourceAnchorValidationError:
        return
    assert len([item for item in proposal.mentions if item.atomic_place_name == "枫溪路"]) == 1


@pytest.mark.parametrize("heading", ["Day2", "  Day2", "第二天"])
def test_an_omitted_cross_day_repeat_requests_repair_instead_of_borrowing_the_first_day(heading):
    source = f"Day1\n上午：枫溪路。\n{heading}\n上午：星海公园。\n中午：青禾路 / 枫溪路吃面。"
    draft = _draft([
        _activity("枫溪路"), _activity("星海公园", day=2),
        _activity("青禾路", day=2, role="OPTIONAL"),
    ])
    # The bounded recovery requires a single unambiguous day for a noun.
    # This cross-day case stays with semantic repair, never silent omission.
    assert _retain_explicit_optional_labels(source, draft) == draft
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(source, draft)
    assert "MISSING_EXPLICIT_OPTIONAL_PLACE" in {issue["category"] for issue in error.value.issues}


@pytest.mark.parametrize("heading", ["Day2", "  Day2", "第二天"])
def test_a_source_grounded_cross_day_repeat_keeps_the_supplied_day_and_second_occurrence(heading):
    source = f"Day1\n上午：枫溪路。\n{heading}\n上午：星海公园。\n中午：青禾路 / 枫溪路吃面。"
    draft = _draft([
        _activity("枫溪路"), _activity("星海公园", day=2),
        _activity("青禾路", day=2, role="OPTIONAL"),
        _activity("枫溪路", day=2, role="OPTIONAL", occurrence=2),
    ])
    assert _retain_explicit_optional_labels(source, draft) == draft
    proposal = proposal_from_draft(source, draft)
    assert [item.atomic_place_name for item in proposal.mentions] == ["枫溪路", "星海公园", "青禾路", "枫溪路"]
    assert [item.day_index for item in proposal.mentions] == [1, 2, 2, 2]
    assert proposal.mentions[-1].span_start == source.rindex("枫溪路")
    assert proposal.mentions[-1].role.value == "OPTIONAL"


@pytest.mark.parametrize("source,rows", [
    ("Day1\n枫溪路。\nDay2\n中午：青禾路 / 枫溪路吃面。", [_activity("枫溪路")]),
    ("Day1\n枫溪路。\n明天的安排：\n中午：青禾路 / 枫溪路吃面。",
     [_activity("枫溪路"), _activity("青禾路", role="OPTIONAL")]),
    ("Day1\n枫溪路。\n中午：青禾路 / 枫溪路吃面。\n星海公园。",
     [_activity("星海公园"), _activity("青禾路", role="OPTIONAL"), _activity("枫溪路")]),
])
def test_unclaimed_relative_or_disordered_day_cannot_supply_a_repeated_option(source, rows):
    draft = _draft(rows)
    assert _retain_explicit_optional_labels(source, draft) == draft


@pytest.mark.parametrize("mark,newline", [("", "\n"), ("**", "\n"), ("", "\r\n"), ("**", "\r\n")])
@pytest.mark.parametrize("meal_role", ["PLANNED", "OPTIONAL"])
def test_an_unnamed_meal_covering_both_explicit_choices_keeps_the_meal_and_each_option(mark, newline, meal_role):
    pair = f"{mark}青禾路{mark} / {mark}枫溪路{mark}"
    source = newline.join([
        "  Day1", "- 上午：枫溪路。", f"- 中午：{pair}吃面。", "- 下午：星海公园。",
    ])
    draft = _draft([
        _activity("枫溪路"), _activity(None, quote=pair, category="餐饮", role=meal_role), _activity("星海公园"),
    ])
    recovered = _retain_explicit_optional_labels(source, draft)
    assert [item.place_name for item in recovered.activities] == ["枫溪路", None, "青禾路", "枫溪路", "星海公园"]
    assert recovered.activities[1] == draft.activities[1]
    assert len(draft.activities) == 3
    added = recovered.activities[2:4]
    assert [item.role.value for item in added] == ["OPTIONAL", "OPTIONAL"]
    assert [item.occurrence for item in added] == [1, 2]
    assert all(item.day_index == 1 and item.category == "地点" for item in added)
    assert all(item.start_time is None and item.end_time is None and item.visit_duration_minutes is None
               and item.time_evidence is None and item.city is None and item.city_evidence is None
               and not item.locked and not item.fixed_commitment for item in added)
    assert _retain_explicit_optional_labels(source, recovered) == recovered

    proposal = proposal_from_draft(source, draft)
    assert [item.atomic_place_name for item in proposal.mentions] == ["枫溪路", None, "青禾路", "枫溪路", "星海公园"]
    assert [item.span_start for item in proposal.mentions if item.role.value == "OPTIONAL" and item.atomic_place_name] == [
        source.index("青禾路"), source.rindex("枫溪路"),
    ]
    compiled = EvidenceCompiler().compile(source, proposal)[0]
    assert [item.eligible_for_place_search for item in compiled] == [True, False, False, False, True]


def test_meal_evidence_stays_on_the_meal_when_its_dining_areas_become_options():
    source = "Day1：上海街区\n上午：枫溪路。\n中午：青禾路 / 枫溪路吃面，12:10已经预约。\n下午：星海公园。"
    draft = _draft([
        _activity("枫溪路"),
        _activity(None, quote="青禾路 / 枫溪路", category="餐饮", start_time="12:10",
                  time_evidence="中午：青禾路 / 枫溪路吃面，12:10已经预约", timing_source="TEXT",
                  locked=True, fixed_commitment=True, city="上海", city_evidence="上海"),
        _activity("星海公园"),
    ])
    recovered = _retain_explicit_optional_labels(source, draft)
    assert len(recovered.activities) == 5
    assert recovered.activities[1] == draft.activities[1]
    for item in recovered.activities[2:4]:
        assert item.start_time is None and item.time_evidence is None
        assert item.city is None and item.city_evidence is None
        assert not item.locked and not item.fixed_commitment


@pytest.mark.parametrize("role,category,name", [
    ("REFERENCE", "餐饮", None),
    ("EXCLUDED", "餐饮", None),
    ("PASS_THROUGH", "餐饮", None),
    ("PLANNED", "地点", None),
    ("PLANNED", "景点", None),
    ("PLANNED", "住宿", None),
])
def test_other_overlapping_roles_categories_and_named_claims_do_not_supply_two_options(role, category, name):
    source = "Day1\n上午：枫溪路。\n中午：青禾路 / 枫溪路吃面。\n下午：星海公园。"
    draft = _draft([
        _activity("枫溪路"),
        _activity(name, quote="青禾路 / 枫溪路", role=role, category=category),
        _activity("星海公园"),
    ])
    assert _retain_explicit_optional_labels(source, draft) == draft


@pytest.mark.parametrize("quote", ["青禾", "青禾路", "青禾路 / 枫溪"])
def test_a_partial_meal_quote_cannot_supply_a_missing_option_from_its_overlap(quote):
    source = "Day1\n上午：枫溪路。\n中午：青禾路 / 枫溪路吃面。\n下午：星海公园。"
    draft = _draft([
        _activity("枫溪路"), _activity(None, quote=quote, category="餐饮"),
        _activity("枫溪路", occurrence=2, role="OPTIONAL"), _activity("星海公园"),
    ])
    assert _retain_explicit_optional_labels(source, draft) == draft


def test_a_meal_without_an_explicit_slash_choice_cannot_create_two_options():
    source = "Day1\n上午：枫溪路。\n中午：青禾路与枫溪路附近吃面。\n下午：星海公园。"
    draft = _draft([
        _activity("枫溪路"), _activity(None, quote="青禾路与枫溪路", category="餐饮"), _activity("星海公园"),
    ])
    assert _retain_explicit_optional_labels(source, draft) == draft


def test_the_meal_and_dining_areas_in_an_unselected_day_branch_all_stay_optional():
    source = ("Day1：二选一（方案 A 街区｜方案 B 郊外）\n方案 A：街区\n"
              "上午：枫溪路。\n中午：青禾路 / 枫溪路吃面。\n下午：星海公园。\n"
              "方案 B：郊外\n上午：云桥公园。")
    draft = _draft([
        _activity("枫溪路", role="OPTIONAL"),
        _activity(None, quote="青禾路 / 枫溪路", category="餐饮", role="OPTIONAL"),
        _activity("星海公园", role="OPTIONAL"), _activity("云桥公园", role="OPTIONAL"),
    ])
    recovered = _retain_explicit_optional_labels(source, draft)
    assert len(recovered.activities) == 6
    assert recovered.activities[1] == draft.activities[1]
    proposal = proposal_from_draft(source, draft)
    assert [item.atomic_place_name for item in proposal.mentions] == ["枫溪路", None, "青禾路", "枫溪路", "星海公园", "云桥公园"]
    assert all(item.role.value == "OPTIONAL" and item.day_index == 1 for item in proposal.mentions)
    assert all(not item.eligible_for_place_search for item in EvidenceCompiler().compile(source, proposal)[0])


@pytest.mark.parametrize("pair,expected_options", [
    ("青禾路 / 枫溪路", ["青禾路", "枫溪路"]),
    ("枫溪路 / 青禾路", ["枫溪路", "青禾路"]),
])
def test_main_visits_followed_by_an_ordered_option_block_can_recover_the_missing_meal_option(pair, expected_options):
    source = f"Day1\n上午：枫溪路。\n中午：{pair}吃面。\n下午：星海公园。"
    draft = _draft([
        _activity("枫溪路"), _activity("星海公园"), _activity("青禾路", role="OPTIONAL"),
    ])
    recovered = _retain_explicit_optional_labels(source, draft)
    assert recovered.activities[:2] == draft.activities[:2]
    assert [item.place_name for item in recovered.activities[2:]] == expected_options
    assert all(item.role.value == "OPTIONAL" for item in recovered.activities[2:])
    assert next(item for item in recovered.activities[2:] if item.place_name == "青禾路") == draft.activities[2]
    assert next(item for item in recovered.activities[2:] if item.place_name == "枫溪路").occurrence == 2
    assert _retain_explicit_optional_labels(source, recovered) == recovered
    proposal = proposal_from_draft(source, draft)
    assert [item.atomic_place_name for item in proposal.mentions if item.role.value == "PLANNED"] == ["枫溪路", "星海公园"]
    assert [item.atomic_place_name for item in proposal.mentions if item.role.value == "OPTIONAL"] == expected_options
    option_spans = [item.span_start for item in proposal.mentions if item.role.value == "OPTIONAL"]
    assert option_spans == sorted(option_spans)


@pytest.mark.parametrize("reversed_role", ["PLANNED", "OPTIONAL"])
def test_a_reversed_order_within_a_role_still_prevents_automatic_recovery(reversed_role):
    source = "Day1\n上午：枫溪路。\n中午：青禾路 / 枫溪路吃面。\n下午：星海公园。\nIris咖啡可以打卡。"
    planned = [_activity("枫溪路"), _activity("星海公园")]
    optional = [_activity("青禾路", role="OPTIONAL"), _activity("Iris咖啡", role="OPTIONAL")]
    if reversed_role == "PLANNED":
        planned.reverse()
    else:
        optional.reverse()
    draft = _draft(planned + optional)
    assert _retain_explicit_optional_labels(source, draft) == draft
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(source, draft)
    assert "MISSING_EXPLICIT_OPTIONAL_PLACE" in {issue["category"] for issue in error.value.issues}


@pytest.mark.parametrize("kept,category,role", [
    ("青禾路", "餐饮", "PLANNED"),
    ("枫溪路", "餐饮", "PLANNED"),
    ("青禾路", "地点", "OPTIONAL"),
    ("枫溪路", "地点", "OPTIONAL"),
])
def test_an_exact_named_member_with_the_whole_meal_pair_quote_is_narrowed_before_recovery(kept, category, role):
    source = "Day1\n上午：枫溪路。\n中午：青禾路 / 枫溪路吃面。\n下午：星海公园。"
    draft = _draft([
        _activity("枫溪路"),
        _activity(kept, quote="青禾路 / 枫溪路", category=category, role=role),
        _activity("星海公园"),
    ])
    proposal = proposal_from_draft(source, draft)
    assert [item.atomic_place_name for item in proposal.mentions if item.role.value == "PLANNED"] == ["枫溪路", "星海公园"]
    options = [item for item in proposal.mentions if item.role.value == "OPTIONAL"]
    assert [item.atomic_place_name for item in options] == ["青禾路", "枫溪路"]
    assert [item.span_start for item in options] == [source.index("青禾路"), source.rindex("枫溪路")]
    assert [source[item.span_start:item.span_end] for item in options] == ["青禾路", "枫溪路"]
    assert [item.day_index for item in options] == [1, 1]
    assert all(not item.eligible_for_place_search
               for item in EvidenceCompiler().compile(source, proposal)[0]
               if item.mention.role.value == "OPTIONAL")
    assert draft.activities[1].source_quote == "青禾路 / 枫溪路"
    assert draft.activities[1].occurrence == 1


@pytest.mark.parametrize("kept", ["青禾路", "枫溪路"])
def test_narrowing_a_meal_member_keeps_its_own_timing_and_never_moves_it_to_its_sibling(kept):
    source = f"Day1：上海街区\n上午：枫溪路。\n中午：青禾路 / 枫溪路吃面，12:10在{kept}已预约。\n下午：星海公园。"
    original = _activity(kept, quote="青禾路 / 枫溪路", category="餐饮", role="OPTIONAL",
                         start_time="12:10", timing_source="TEXT", locked=True, fixed_commitment=True,
                         time_evidence=f"12:10在{kept}已预约", city="上海", city_evidence="上海")
    draft = _draft([_activity("枫溪路"), original, _activity("星海公园")])
    recovered = _retain_explicit_optional_labels(source, draft)
    assert len(recovered.activities) == 4
    retained = next(item for item in recovered.activities if item.place_name == kept and item.role.value == "OPTIONAL")
    assert retained.source_quote == kept
    assert retained.occurrence == (2 if kept == "枫溪路" else 1)
    assert retained.start_time == "12:10" and retained.time_evidence == original["time_evidence"]
    assert retained.locked and retained.fixed_commitment and retained.city == "上海"
    added = next(item for item in recovered.activities if item.place_name != kept and item.role.value == "OPTIONAL")
    assert added.start_time is None and added.end_time is None and added.visit_duration_minutes is None
    assert added.time_evidence is None and added.city is None and added.city_evidence is None
    assert not added.locked and not added.fixed_commitment


@pytest.mark.parametrize("name,role,day,quote", [
    ("青禾路步行街", "PLANNED", 1, "青禾路 / 枫溪路"),
    ("其他路", "PLANNED", 1, "青禾路 / 枫溪路"),
    ("青禾路", "REFERENCE", 1, "青禾路 / 枫溪路"),
    ("青禾路", "EXCLUDED", 1, "青禾路 / 枫溪路"),
    ("青禾路", "PASS_THROUGH", 1, "青禾路 / 枫溪路"),
    ("青禾路", "PLANNED", 2, "青禾路 / 枫溪路"),
    ("青禾路", "PLANNED", 1, "青禾路 / 枫溪"),
    ("青禾路", "PLANNED", 1, "青禾路 / 枫溪路吃面。\n下午：星海公园"),
])
def test_an_unsafe_named_pair_claim_is_not_narrowed_to_authorize_missing_options(name, role, day, quote):
    source = "Day1\n上午：枫溪路。\n中午：青禾路 / 枫溪路吃面。\n下午：星海公园。"
    draft = _draft([
        _activity("枫溪路"), _activity(name, quote=quote, role=role, day=day, category="餐饮"),
        _activity("星海公园"),
    ])
    assert _retain_explicit_optional_labels(source, draft) == draft


def test_a_named_pair_cannot_take_the_source_occurrence_already_claimed_as_a_reference():
    source = "Day1\n上午：枫溪路。\n中午：青禾路 / 枫溪路吃面。\n下午：星海公园。"
    draft = _draft([
        _activity("枫溪路"), _activity("青禾路", quote="青禾路 / 枫溪路", category="餐饮"),
        _activity(None, quote="枫溪路", occurrence=2, role="REFERENCE"), _activity("星海公园"),
    ])
    assert _retain_explicit_optional_labels(source, draft) == draft
