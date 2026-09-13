"""Source branch labels survive grouping without becoming selection authority."""

import pytest

from app.trip_understanding.choice_groups import bind_choice_groups
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.experience_inference import (
    ExperienceQwenProvider,
    SemanticDraft,
    proposal_from_draft,
)
from app.trip_understanding.models import ChoiceClearCommand, ChoiceSelectCommand, UndoCommand
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_choice_group_selection import SOURCE, draft
from tests.test_semantic_partial_recovery import activity
from tests.test_semantic_supplement_budget import Client


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("labels,expected", [
    (("A", "B"), ["方案A", "方案B", "方案B"]),
    (("ｂ", "ａ"), ["方案B", "方案A", "方案A"]),
    (("2", "1"), ["方案2", "方案1", "方案1"]),
])
def test_verified_headings_keep_labels_and_same_name_branch_occurrences(typed, labels, expected):
    source = (f"Day1：城中/海岸二选一\n### 方案{labels[0]}：城中\n星河公园。\n"
              f"### 方案{labels[1]}：海岸\n月光桥，星河公园。")
    raw = {"activities": [activity("星河公园", role="OPTIONAL"),
        activity("月光桥", role="OPTIONAL"), activity("星河公园", role="OPTIONAL", occurrence=2)]}
    if typed:
        raw["choice_groups"] = [{"scope_quote": source,
            "branches": [{"activity_indices": [0]}, {"activity_indices": [1, 2]}]}]
    result = proposal_from_draft(source, SemanticDraft.model_validate(raw))
    assert [m.branch_label for m in result.mentions] == expected
    assert [m.atomic_place_name for m in result.mentions] == ["星河公园", "月光桥", "星河公园"]
    assert all(m.day_index == 1 and m.role.value == "OPTIONAL" and m.choice_group_selectable for m in result.mentions)
    assert len({m.mention_id for m in result.mentions}) == 3
    assert result.mentions[0].branch_id != result.mentions[2].branch_id
    assert not result.unprocessed_count


def test_unverified_labels_in_metadata_do_not_override_inline_default():
    raw = SemanticDraft.model_validate(draft())
    proposal = proposal_from_draft(SOURCE, raw)
    forged = [m.model_copy(update={"branch_label": "方案B" if i == 1 else "方案A"})
              for i, m in enumerate(proposal.mentions)]
    rebound, issues = bind_choice_groups(SOURCE, raw, forged)
    assert [m.branch_label for m in rebound[1:3]] == ["方案一", "方案二"]
    assert not issues


def test_unrecognized_source_headings_keep_safe_default_labels():
    source = "Day1：城中/海岸二选一\n方案X：城中\n星河公园。\n方案Y：海岸\n月光桥。"
    raw = SemanticDraft.model_validate({"activities": [activity(name, role="OPTIONAL")
        for name in ("星河公园", "月光桥")], "choice_groups": [{"scope_quote": source,
        "branches": [{"activity_indices": [0]}, {"activity_indices": [1]}]}]})
    result = proposal_from_draft(source, raw)
    assert [m.branch_label for m in result.mentions] == ["方案一", "方案二"]
    assert all(m.choice_group_selectable for m in result.mentions)


def test_repeated_place_and_reversed_labels_on_another_day_stay_in_that_day():
    source = ("Day1：城中/海岸二选一\n方案A：城中\n星河公园。\n方案B：海岸\n月光桥。\n"
              "Day2：城中/海岸二选一\n方案B：城中\n星河公园。\n方案A：海岸\n月光桥。")
    raw = SemanticDraft.model_validate({"activities": [
        activity(name, day=day, role="OPTIONAL", occurrence=day)
        for day in (1, 2) for name in ("星河公园", "月光桥")]})
    result = proposal_from_draft(source, raw)
    assert [m.branch_label for m in result.mentions] == ["方案A", "方案B", "方案B", "方案A"]
    assert [m.day_index for m in result.mentions] == [1, 1, 2, 2]
    assert result.mentions[0].choice_group_id != result.mentions[2].choice_group_id
    assert all(source[m.span_start:m.span_end] == m.atomic_place_name for m in result.mentions)


async def build_source_label_states():
    """Fixed provider and identity -> real pipeline/commands, used by desktop UI."""
    source = "杭州。\nDay1：城中/水岸二选一\n方案A：城中\n良渚文化村。\n方案B：水岸\n小河直街。\nDay2：西湖。"
    raw = {"destination": "杭州", "day_labels": [None, None], "activities": [
        activity(name, day=day, role=role, category="景点", city="杭州", city_evidence="杭州")
        for name, day, role in (("良渚文化村", 1, "OPTIONAL"), ("小河直街", 1, "OPTIONAL"), ("西湖", 2, "PLANNED"))]}
    client = Client(raw)
    provider = ExperienceQwenProvider(api_key="fixed", base_url="https://test.invalid", model="fixed",
        client=client, deadline_seconds=2, enable_source_visits=False)
    output = await TripUnderstandingPipeline(provider, FixedReplayPlaces()).run(source)
    assert len(client.calls) == 1
    states = {"labeled_before": output.public_result}
    edges = []

    def transition(start, end, command, **kwargs):
        states[end] = apply_public_command(states[start], command, **kwargs).result
        edges.append({"start": start, "end": end, "command": command.model_dump(mode="json", exclude_none=True)})

    option = states["labeled_before"].days[0].alternatives[0]
    transition("labeled_before", "labeled_selected", ChoiceSelectCommand(command_type="CHOICE_SELECT", day_index=1,
        choice_group_token=option.choice_group_token, branch_token=option.branch_token, position=0))
    group = states["labeled_selected"].days[0].choice_selections[0]
    transition("labeled_selected", "labeled_cleared", ChoiceClearCommand(command_type="CHOICE_CLEAR", day_index=1,
        choice_group_token=group.choice_group_token))
    transition("labeled_cleared", "labeled_restored", UndoCommand(command_type="UNDO"),
        undo_result=states["labeled_selected"])
    return {"states": {key: value.model_dump(mode="json") for key, value in states.items()}, "edges": edges}


@pytest.mark.asyncio
async def test_source_labels_survive_actual_selection_clear_and_undo():
    values = await build_source_label_states()
    states = values["states"]
    for result in states.values():
        assert [a["branch_label"] for a in result["days"][0]["alternatives"]] == ["方案A", "方案B"]
        assert result["days"][1]["activities"][0]["name"] == "西湖"
    for key in ("labeled_selected", "labeled_restored"):
        day = states[key]["days"][0]
        selected = day["choice_selections"][0]
        assert next(a["branch_label"] for a in day["alternatives"] if a["branch_token"] == selected["branch_token"]) == "方案A"
        assert [a["name"] for a in states[key]["days"][0]["activities"]] == ["良渚文化村"]
    assert not states["labeled_cleared"]["days"][0]["choice_selections"]
    assert not states["labeled_cleared"]["days"][0]["activities"]
