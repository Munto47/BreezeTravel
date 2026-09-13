"""A source choice reaches saved branch selection without re-extracting visits."""

import json
from pathlib import Path
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError, model_serializer

from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft, ExperienceQwenProvider
from app.trip_understanding.models import (
    ChoiceSelectCommand,
    ChoiceClearCommand,
    UndoCommand,
    UserFacingTripResult,
    ActivityDeleteCommand,
    ActivityMoveCommand,
    ActivityTextEditCommand,
    PlaceReplaceCommand,
    CreateFullRequest,
    PlaceConfirmCommand,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_semantic_supplement_budget import Client
from tests.semantic_page_replays import FixedReplayPlaces


SOURCE = "杭州一日游。\nDay1\n参观中国美术学院象山校区。随后步行至附近的【良渚文化村】或留在市区探索【小河直街】。晚上去西湖。"
SCOPE = "随后步行至附近的【良渚文化村】或留在市区探索【小河直街】"
NAMES = ["中国美术学院象山校区", "良渚文化村", "小河直街", "西湖"]


def draft(*, typed=True, source=SOURCE):
    value = {
        "destination": "杭州",
        "day_labels": [None],
        "activities": [
            {
                "source_quote": name,
                "place_name": name,
                "role": "OPTIONAL" if index in (1, 2) else "PLANNED",
                "category": "景点",
                "day_index": 1,
                "city": "杭州",
                "city_evidence": "杭州一日游",
            }
            for index, name in enumerate(NAMES)
        ],
    }
    if typed:
        value["choice_groups"] = [
            {
                "scope_quote": SCOPE,
                "occurrence": 1,
                "status": "UNSELECTED",
                "branches": [{"activity_indices": [1]}, {"activity_indices": [2]}],
            }
        ]
    return value


async def build_choice_result():
    client = Client(draft())
    provider = ExperienceQwenProvider(
        api_key="fixed",
        base_url="https://test.invalid",
        model="fixed",
        client=client,
        deadline_seconds=2,
        enable_source_visits=False,
    )
    result = await TripUnderstandingPipeline(provider, FixedReplayPlaces()).run(SOURCE)
    assert len(client.calls) == 1
    return result


async def build_unsupported_choice_result():
    saved = json.loads(
        (Path(__file__).parent / "fixtures/live_owner_shanghai_nested_choices.json").read_text(encoding="utf-8")
    )
    # Use the same saved real answer when the current validator asks its one
    # permitted repair. Never substitute a fabricated corrected model answer.
    client = Client(saved["response"], saved["response"])
    provider = ExperienceQwenProvider(
        api_key="fixed",
        base_url="https://test.invalid",
        model="fixed",
        client=client,
        deadline_seconds=10,
        enable_source_visits=False,
    )
    return await TripUnderstandingPipeline(provider, FixedReplayPlaces()).run(saved["source"])


def test_original_hangzhou_replies_remain_optional_and_source_clarifies_only_explicit_group():
    saved = json.loads(
        (Path(__file__).parent / "fixtures/live_hangzhou_unselected_choice.json").read_text(encoding="utf-8")
    )
    assert all(not item["choice_group_id"] and not item["branch_id"] for item in saved["historical_group_fields"])
    for raw in saved["responses"]:
        assert "choice_groups" not in json.loads(raw)  # never pretend the original model supplied the group
        original = SemanticDraft.model_validate_json(raw)
        result = proposal_from_draft(saved["source"], original, allow_partial=True)
        rows = [m for m in result.mentions if m.atomic_place_name in saved["target_names"]]
        assert [m.atomic_place_name for m in rows] == saved["target_names"]
        assert all(m.role.value == "OPTIONAL" and m.day_index == 3 for m in rows)
        assert rows[0].choice_group_id and rows[0].choice_group_id == rows[1].choice_group_id
        assert rows[0].branch_id != rows[1].branch_id


def test_recovery_rebinds_indices_after_an_insert_and_rejects_ambiguous_revisit():
    from app.trip_understanding.choice_groups import remap_choice_groups

    original = SemanticDraft.model_validate(draft())
    shifted = original.model_copy(update={"activities": [original.activities[3], *original.activities]})
    rebound = remap_choice_groups(SOURCE, original, shifted)
    assert [b.activity_indices for b in rebound.choice_groups[0].branches] == [[2], [3]]
    ambiguous = original.model_copy(update={"activities": [*original.activities, original.activities[1]]})
    rejected = remap_choice_groups(SOURCE, original, ambiguous)
    assert not rejected.choice_groups and SCOPE in rejected.unprocessed_quotes
    full = original.model_copy(update={"unprocessed_quotes": ["已有未处理"] * 80, "activities": ambiguous.activities})
    limited = remap_choice_groups(SOURCE, original, full)
    assert len(limited.unprocessed_quotes) == 80
    SemanticDraft.model_validate_json(limited.model_dump_json())


def test_list_expansion_before_a_group_cannot_shift_group_indices_to_wrong_visit():
    from app.trip_understanding.experience_inference import _expand_source_bound_lists

    source = SOURCE.replace("参观中国", "先后参观青溪公园、星河公园。参观中国")
    raw = draft()
    raw["activities"].insert(
        0, {"source_quote": "青溪公园、星河公园", "place_name": "青溪公园、星河公园", "role": "PLANNED", "day_index": 1}
    )
    raw["choice_groups"][0]["branches"] = [{"activity_indices": [2]}, {"activity_indices": [3]}]
    expanded = _expand_source_bound_lists(source, SemanticDraft.model_validate(raw))
    assert [branch.activity_indices for branch in expanded.choice_groups[0].branches] == [[3], [4]]
    result = proposal_from_draft(source, expanded)
    grouped = [item for item in result.mentions if item.choice_group_id]
    assert [item.atomic_place_name for item in grouped] == NAMES[1:3]


def select_command(result, branch=0):
    item = result.days[0].alternatives[branch]
    return ChoiceSelectCommand(
        command_type="CHOICE_SELECT",
        day_index=1,
        choice_group_token=item.choice_group_token,
        branch_token=item.branch_token,
        position=1,
    )


async def build_choice_states():
    from tests.test_dining_recommendations import restaurant

    before = (await build_choice_result()).public_result
    selected = apply_public_command(before, select_command(before)).result
    token = selected.days[0].choice_selections[0].activity_tokens[0]
    place = restaurant().model_copy(update={"name": NAMES[1], "category": "景点", "city": "杭州"})
    confirmed = apply_public_command(
        selected,
        PlaceConfirmCommand(
            command_type="PLACE_CONFIRM",
            activity_token=token,
            candidate_token="fixed-candidate-not-sent-to-service-00000000",
        ),
        confirmed_place=place,
    ).result
    undo = apply_public_command(selected, UndoCommand(command_type="UNDO"), undo_result=before).result
    cleared = apply_public_command(
        confirmed,
        ChoiceClearCommand(
            command_type="CHOICE_CLEAR",
            day_index=1,
            choice_group_token=confirmed.days[0].choice_selections[0].choice_group_token,
        ),
    ).result
    return {
        "before": before.model_dump(mode="json"),
        "selected": selected.model_dump(mode="json"),
        "confirmed": confirmed.model_dump(mode="json"),
        "undo": undo.model_dump(mode="json"),
        "cleared": cleared.model_dump(mode="json"),
    }


@pytest.mark.parametrize("typed", [True, False])
def test_explicit_or_is_one_unselected_group_without_changing_roles_or_order(typed):
    result = proposal_from_draft(SOURCE, SemanticDraft.model_validate(draft(typed=typed)))
    assert [m.atomic_place_name for m in result.mentions] == NAMES
    assert [m.role.value for m in result.mentions] == ["PLANNED", "OPTIONAL", "OPTIONAL", "PLANNED"]
    choices = result.mentions[1:3]
    assert choices[0].choice_group_id and choices[0].choice_group_id == choices[1].choice_group_id
    assert choices[0].branch_id != choices[1].branch_id
    assert not any(d.category == "CHOICE_GROUP_UNRESOLVED" for d in result.diagnostics)


@pytest.mark.parametrize(
    "boundary",
    [
        "cross_day",
        "wrong_occurrence",
        "outside_member",
        "duplicate",
        "reversed",
        "cancelled",
        "settled",
        "bare_advice",
        "or_maybe",
    ],
)
def test_group_cannot_override_source_or_members(boundary):
    raw, source = draft(), SOURCE
    group = raw["choice_groups"][0]
    if boundary == "cross_day":
        raw["activities"][2]["day_index"] = 2
    elif boundary == "wrong_occurrence":
        group["occurrence"] = 2
    elif boundary == "outside_member":
        group["branches"][1]["activity_indices"] = [3]
    elif boundary == "duplicate":
        group["branches"][1]["activity_indices"] = [1]
    elif boundary == "reversed":
        group["branches"].reverse()
    elif boundary == "cancelled":
        source = SOURCE.replace(SCOPE, "取消这次二选一：" + SCOPE)
    elif boundary == "settled":
        source += "最终决定去良渚文化村。"
    elif boundary == "bare_advice":
        source = SOURCE.replace(SCOPE, SCOPE.replace("或留在市区探索", "，建议前往"))
        group["scope_quote"] = SCOPE.replace("或留在市区探索", "，建议前往")
        for item in raw["activities"]:
            item["role"] = "PLANNED"
    else:
        source = SOURCE.replace("或留在", "或许留在")
        group["scope_quote"] = SCOPE.replace("或留在", "或许留在")
    result = proposal_from_draft(source, SemanticDraft.model_validate(raw), allow_partial=True)
    assert all(m.choice_group_id is None for m in result.mentions)
    assert any(d.category == "CHOICE_GROUP_UNRESOLVED" for d in result.diagnostics)


@pytest.mark.parametrize("bad", [True, "1", -1, 160])
def test_member_indices_are_strict(bad):
    raw = draft()
    raw["choice_groups"][0]["branches"][0]["activity_indices"] = [bad]
    with pytest.raises(ValidationError):
        SemanticDraft.model_validate(raw)


def test_multi_stop_branch_is_ordered_together_not_three_way_choice():
    source = "杭州一日游。\nDay1\n二选一：先去河坊街，再去南宋御街；或者去西湖。"
    raw = {
        "destination": "杭州",
        "day_labels": [None],
        "activities": [
            {"source_quote": name, "place_name": name, "role": "OPTIONAL", "day_index": 1}
            for name in ["河坊街", "南宋御街", "西湖"]
        ],
        "choice_groups": [
            {
                "scope_quote": "二选一：先去河坊街，再去南宋御街；或者去西湖",
                "branches": [{"activity_indices": [0, 1]}, {"activity_indices": [2]}],
            }
        ],
    }
    result = proposal_from_draft(source, SemanticDraft.model_validate(raw))
    assert result.mentions[0].branch_id == result.mentions[1].branch_id != result.mentions[2].branch_id
    assert len({m.choice_group_id for m in result.mentions}) == 1


@pytest.mark.parametrize(
    "quote,selectable",
    [
        ("青溪寺（外观或购票入内）+ 星河公园", False),
        ("青溪寺(外观或者入内) + 星河公园", False),
        ("青溪寺（外观）或星河公园", True),
    ],
)
def test_visit_purpose_or_cannot_become_exclusive_parent_visits(quote, selectable):
    source = "Day1\n" + quote
    raw = {
        "activities": [
            {"source_quote": name, "place_name": name, "role": "OPTIONAL", "day_index": 1}
            for name in ("青溪寺", "星河公园")
        ],
        "choice_groups": [{"scope_quote": quote, "branches": [{"activity_indices": [0]}, {"activity_indices": [1]}]}],
    }
    result = proposal_from_draft(source, SemanticDraft.model_validate(raw), allow_partial=True)
    assert all(m.choice_group_selectable == selectable for m in result.mentions)
    assert [m.role.value for m in result.mentions] == ["OPTIONAL", "OPTIONAL"]


@pytest.mark.asyncio
async def test_choose_one_marks_only_that_branch_pending_and_undo_restores_original():
    original = (await build_choice_result()).public_result
    updated = apply_public_command(original, select_command(original)).result
    assert [c.name for c in updated.days[0].activities] == [NAMES[0], NAMES[1], NAMES[3]]
    assert updated.days[0].activities[1].status == "NEEDS_CONFIRMATION"
    assert updated.days[0].choice_selections[0].activity_tokens == [updated.days[0].activities[1].activity_token]
    assert len(updated.days[0].alternatives) == 2
    assert not original.days[0].choice_selections
    assert [item.insertion_position for item in original.days[0].alternatives] == [1, 1]
    UserFacingTripResult.model_validate_json(updated.model_dump_json())
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(updated, select_command(updated, 1))
    restored = apply_public_command(updated, UndoCommand(command_type="UNDO"), undo_result=original).result
    assert not restored.days[0].choice_selections
    assert [c.name for c in restored.days[0].activities] == [NAMES[0], NAMES[3]]


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["group", "branch", "day", "position"])
async def test_command_uses_current_public_members_and_rejects_tampering(tamper):
    current = (await build_choice_result()).public_result
    command = select_command(current)
    if tamper == "day":
        command.day_index = 2
    elif tamper == "position":
        command.position = 3
    else:
        setattr(command, "choice_group_token" if tamper == "group" else "branch_token", "unknown" * 4)
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(current, command)
    assert len(current.days[0].activities) == 2 and not current.days[0].choice_selections


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["same_day", "cross_day", "delete"])
async def test_selection_survives_tokens_and_manual_change_cannot_silently_select_second_branch(action):
    original = (await build_choice_result()).public_result
    selected = apply_public_command(original, select_command(original)).result
    token = selected.days[0].choice_selections[0].activity_tokens[0]
    command = (
        ActivityDeleteCommand(command_type="ACTIVITY_DELETE", activity_token=token)
        if action == "delete"
        else ActivityMoveCommand(
            command_type="ACTIVITY_MOVE",
            activity_token=token,
            target_day_index=1 if action == "same_day" else 2,
            target_position=0,
        )
    )
    updated = apply_public_command(selected, command).result
    selection = updated.days[0].choice_selections[0]
    assert selection.status == ("SELECTED" if action == "same_day" else "MODIFIED")
    UserFacingTripResult.model_validate_json(updated.model_dump_json())
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(updated, select_command(updated, 1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["replace", "rename", "different_confirmed_identity", "first_confirmation_different_name"]
)
async def test_replacing_selected_visit_marks_choice_modified_without_enabling_other_branch(action):
    states = await build_choice_states()
    current = UserFacingTripResult.model_validate(states["confirmed"])
    token = current.days[0].choice_selections[0].activity_tokens[0]
    kwargs = {}
    if action == "replace":
        command = PlaceReplaceCommand(
            command_type="PLACE_REPLACE",
            activity_token=token,
            replacement={"name": "其他地点", "category": "景点", "area_or_address": "地点待确认"},
        )
    elif action == "rename":
        command = ActivityTextEditCommand(command_type="ACTIVITY_TEXT_EDIT", activity_token=token, name="其他地点")
    else:
        from tests.test_dining_recommendations import restaurant

        command = PlaceConfirmCommand(
            command_type="PLACE_CONFIRM",
            activity_token=token,
            candidate_token="fixed-candidate-not-sent-to-service-00000000",
        )
        kwargs = {
            "current_place_id": "old-confirmed-parent" if action == "different_confirmed_identity" else None,
            "confirmed_place": restaurant(),
        }
    changed = apply_public_command(current, command, **kwargs).result
    assert changed.days[0].choice_selections[0].status == "MODIFIED"
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(changed, select_command(changed, 1))
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(
            changed,
            ChoiceClearCommand(
                command_type="CHOICE_CLEAR",
                day_index=1,
                choice_group_token=changed.days[0].choice_selections[0].choice_group_token,
            ),
        )
    released = apply_public_command(
        changed,
        ChoiceClearCommand(
            command_type="CHOICE_CLEAR",
            day_index=1,
            choice_group_token=changed.days[0].choice_selections[0].choice_group_token,
            preserve_activities=True,
        ),
    ).result
    assert not released.days[0].choice_selections
    assert [c.name for d in released.days for c in d.activities] == [c.name for d in changed.days for c in d.activities]
    restored = apply_public_command(released, UndoCommand(command_type="UNDO"), undo_result=changed).result
    assert restored.days[0].choice_selections[0].status == "MODIFIED"


@pytest.mark.asyncio
async def test_same_confirmed_identity_retains_selection_with_canonical_alias():
    from tests.test_dining_recommendations import restaurant

    current = UserFacingTripResult.model_validate((await build_choice_states())["confirmed"])
    token = current.days[0].choice_selections[0].activity_tokens[0]
    place = restaurant().model_copy(update={"name": "良渚文化村规范名称", "city": "杭州", "category": "景点"})
    result = apply_public_command(
        current,
        PlaceConfirmCommand(
            command_type="PLACE_CONFIRM",
            activity_token=token,
            candidate_token="fixed-candidate-not-sent-to-service-00000000",
        ),
        confirmed_place=place,
        current_place_id=place.canonical_place_id,
    ).result
    assert result.days[0].choice_selections[0].status == "SELECTED"


@pytest.mark.parametrize("bad", [1, "true", None])
def test_release_intent_is_strict_and_must_not_be_coerced(bad):
    with pytest.raises(ValidationError):
        ChoiceClearCommand(
            command_type="CHOICE_CLEAR",
            day_index=1,
            choice_group_token="group-token-0000000000000000000",
            preserve_activities=bad,
        )


@pytest.mark.asyncio
async def test_intact_selection_cannot_be_released_as_modified():
    original = (await build_choice_result()).public_result
    current = apply_public_command(original, select_command(original)).result
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(
            current,
            ChoiceClearCommand(
                command_type="CHOICE_CLEAR",
                day_index=1,
                choice_group_token=current.days[0].choice_selections[0].choice_group_token,
                preserve_activities=True,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("card_count,allowed", [(159, True), (160, False)])
async def test_branch_selection_respects_actual_card_capacity_without_partial_insertion(card_count, allowed):
    current = (await build_choice_result()).public_result
    template = current.days[0].activities[0]
    current.days[0].activities = [
        template.model_copy(update={"activity_token": f"fixed-capacity-existing-{index:06d}"})
        for index in range(card_count)
    ]
    command = select_command(current)
    if not allowed:
        with pytest.raises(CommandTargetChangedError):
            apply_public_command(current, command)
        assert len(current.days[0].activities) == 160 and not current.days[0].choice_selections
    else:
        selected = apply_public_command(current, command).result
        assert len(selected.days[0].activities) == 160
        assert len(selected.days[0].choice_selections[0].activity_tokens) == 1


@pytest.mark.asyncio
async def test_other_group_survives_and_position_updates_with_hidden_cards_and_old_snapshots():
    current = (await build_choice_result()).public_result
    for item in current.days[0].alternatives:
        current.days[0].alternatives.append(
            item.model_copy(update={"choice_group_token": "other-group-current-token-00000000"})
        )
        if len(current.days[0].alternatives) == 4:
            break
    current.days[0].activities[0].status = "NEEDS_CONFIRMATION"
    assert [a.insertion_position for a in current.days[0].alternatives] == [1] * 4
    selected = apply_public_command(current, select_command(current)).result
    assert len(selected.days[0].alternatives) == 4
    assert len(selected.days[0].choice_selections) == 1
    assert all(a.insertion_position is None for a in selected.days[0].alternatives)
    moved = apply_public_command(
        selected,
        ActivityMoveCommand(
            command_type="ACTIVITY_MOVE",
            activity_token=selected.days[0].activities[-1].activity_token,
            target_day_index=1,
            target_position=0,
        ),
    ).result
    assert all(a.insertion_position is None for a in moved.days[0].alternatives)
    payload = current.model_dump(mode="json")
    for day in payload["days"]:
        day.pop("choice_selections")
        for item in day["alternatives"]:
            for field in (
                "insertion_position",
                "before_activity_token",
                "after_activity_token",
                "source_details",
                "meal_role",
            ):
                item.pop(field, None)
            item.pop("choice_group_selectable", None)
    legacy = UserFacingTripResult.model_validate(payload)
    assert not legacy.days[0].choice_selections
    assert all(a.insertion_position is None for a in legacy.days[0].alternatives)
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(legacy, select_command(legacy))
    from evals.g07_text_convergence_v1.runner import _public_payload_is_redacted

    assert _public_payload_is_redacted(selected.model_dump(mode="json"))
    assert not _public_payload_is_redacted({"choice_selections": [{"source_quote": "private source"}]})


@pytest.mark.asyncio
async def test_actual_saved_nested_choice_cannot_adopt_all_inner_options_as_required_visits():
    saved = json.loads(
        (Path(__file__).parent / "fixtures/live_owner_shanghai_nested_choices.json").read_text(encoding="utf-8")
    )
    old = UserFacingTripResult.model_validate(saved["historical_public"])
    old_option = old.days[2].alternatives[0]
    assert len([item for item in old.days[2].alternatives if item.branch_token == old_option.branch_token]) == 8
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(
            old,
            ChoiceSelectCommand(
                command_type="CHOICE_SELECT",
                day_index=3,
                position=0,
                choice_group_token=old_option.choice_group_token,
                branch_token=old_option.branch_token,
            ),
        )
    output = await build_unsupported_choice_result()
    day = output.public_result.days[2]
    assert not day.activities and day.alternatives
    assert all(not item.choice_group_selectable for item in day.alternatives)
    assert any(issue.category == "CHOICE_GROUP_UNRESOLVED" for issue in output.proposal.diagnostics)
    assert output.public_result.coverage.complete is False
    option = day.alternatives[0]
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(
            output.public_result,
            ChoiceSelectCommand(
                command_type="CHOICE_SELECT",
                day_index=3,
                position=0,
                choice_group_token=option.choice_group_token,
                branch_token=option.branch_token,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_saved_selection_confirmation_refresh_undo_authorization_and_idempotency(kind):
    from app.trip_understanding.candidates import issue_candidate
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from app.trip_understanding.errors import ResourceAccessDeniedError, RevisionConflictError
    from tests.test_experience_v3_journey import repository_for
    from tests.test_dining_recommendations import restaurant

    async with repository_for(kind) as repository:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repository)
        created = await service.create_full(
            CreateFullRequest.model_validate({"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE}}),
            owner_user_id="experience-owner",
            idempotency_key="choice-create",
            now=now,
        )
        job = await repository.claim_next(worker_id="fixed-choice", now=now, lease_seconds=60)
        await repository.complete_job(job, await build_choice_result(), now=now)

        async def read():
            resource = await repository.authorize(
                created.accepted.public_resource_id, capability_hash=None, user_id="experience-owner", now=now
            )
            return resource, await repository.get_result(resource)

        resource, initial = await read()
        with pytest.raises(ResourceAccessDeniedError):
            await repository.authorize(
                resource.public_resource_id, capability_hash=None, user_id="other-owner", now=now
            )
        command = select_command(initial.result)
        first = await service.apply_command(
            resource, command, expected_etag=initial.opaque_etag, idempotency_key="choice-select", now=now
        )
        replay = await service.apply_command(
            resource, command, expected_etag=initial.opaque_etag, idempotency_key="choice-select", now=now
        )
        assert replay.replayed and first.opaque_etag == replay.opaque_etag
        with pytest.raises(RevisionConflictError):
            await service.apply_command(
                resource, command, expected_etag=initial.opaque_etag, idempotency_key="stale-choice", now=now
            )
        resource, selected = await read()
        # Undo the branch selection itself on an actual immutable saved version.
        await service.apply_command(
            resource,
            UndoCommand(command_type="UNDO"),
            expected_etag=selected.opaque_etag,
            idempotency_key="undo-choice",
            now=now,
        )
        resource, undone_choice = await read()
        assert not undone_choice.result.days[0].choice_selections
        assert [card.name for card in undone_choice.result.days[0].activities] == [NAMES[0], NAMES[3]]
        assert len(undone_choice.result.days[0].alternatives) == 2
        await service.apply_command(
            resource,
            select_command(undone_choice.result),
            expected_etag=undone_choice.opaque_etag,
            idempotency_key="choice-select-again",
            now=now,
        )
        resource, selected = await read()
        token = selected.result.days[0].choice_selections[0].activity_tokens[0]
        place = restaurant().model_copy(update={"name": NAMES[1], "category": "景点", "city": "杭州"})
        candidate = issue_candidate(
            place,
            public_resource_id=resource.public_resource_id,
            activity_token=token,
            expected_etag=selected.opaque_etag,
            now=now,
        )
        await service.apply_command(
            resource,
            PlaceConfirmCommand(
                command_type="PLACE_CONFIRM", activity_token=token, candidate_token=candidate.candidate_token
            ),
            expected_etag=selected.opaque_etag,
            idempotency_key="choice-confirm",
            now=now,
        )
        resource, confirmed = await read()
        selected_token = confirmed.result.days[0].choice_selections[0].activity_tokens[0]
        assert (
            next(c for c in confirmed.result.days[0].activities if c.activity_token == selected_token).status == "READY"
        )
        clear_command = ChoiceClearCommand(
            command_type="CHOICE_CLEAR",
            day_index=1,
            choice_group_token=confirmed.result.days[0].choice_selections[0].choice_group_token,
        )
        clear = await service.apply_command(
            resource, clear_command, expected_etag=confirmed.opaque_etag, idempotency_key="clear-after-confirm", now=now
        )
        repeated = await service.apply_command(
            resource, clear_command, expected_etag=confirmed.opaque_etag, idempotency_key="clear-after-confirm", now=now
        )
        assert repeated.replayed and repeated.opaque_etag == clear.opaque_etag
        resource, cleared = await read()
        assert not cleared.result.days[0].choice_selections
        assert [c.name for c in cleared.result.days[0].activities] == [NAMES[0], NAMES[3]]
        await service.apply_command(
            resource,
            UndoCommand(command_type="UNDO"),
            expected_etag=cleared.opaque_etag,
            idempotency_key="undo-clear",
            now=now,
        )
        resource, confirmed = await read()
        assert confirmed.result.days[0].choice_selections[0].status == "SELECTED"
        # Restoring this choice keeps earlier edits available and also permits
        # redoing the clear; a new clear creates a new branch instead.
        assert confirmed.result.can_undo and confirmed.result.can_redo
        await service.apply_command(
            resource,
            ChoiceClearCommand(
                command_type="CHOICE_CLEAR",
                day_index=1,
                choice_group_token=confirmed.result.days[0].choice_selections[0].choice_group_token,
            ),
            expected_etag=confirmed.opaque_etag,
            idempotency_key="clear-without-undo",
            now=now,
        )
        resource, cleared = await read()
        assert not cleared.result.days[0].choice_selections
        await service.apply_command(
            resource,
            select_command(cleared.result),
            expected_etag=cleared.opaque_etag,
            idempotency_key="select-after-clear",
            now=now,
        )
        resource, confirmed = await read()
        undone = confirmed
        assert undone.result.days[0].choice_selections
        await service.apply_command(
            resource,
            ActivityDeleteCommand(
                command_type="ACTIVITY_DELETE",
                activity_token=undone.result.days[0].choice_selections[0].activity_tokens[0],
            ),
            expected_etag=undone.opaque_etag,
            idempotency_key="remove-selected",
            now=now,
        )
        resource, removed = await read()
        assert removed.result.days[0].choice_selections[0].status == "MODIFIED"
        await service.delete_source(
            resource, user_id="experience-owner", idempotency_key="choice-delete-source", now=now
        )
        resource, retained = await read()
        assert retained.result.days[0].choice_selections[0].status == "MODIFIED"
        await service.apply_command(
            resource,
            UndoCommand(command_type="UNDO"),
            expected_etag=retained.opaque_etag,
            idempotency_key="choice-undo-after-source-delete",
            now=now,
        )
        resource, restored = await read()
        assert restored.result.days[0].choice_selections
        supplementary = await repository.get_supplementary_view(resource, now=now)
        assert supplementary.status == "DELETED"
        # A user-modified selected card may be explicitly detached, never deleted.
        await service.apply_command(
            resource,
            ActivityMoveCommand(
                command_type="ACTIVITY_MOVE",
                activity_token=restored.result.days[0].choice_selections[0].activity_tokens[0],
                target_day_index=2,
                target_position=0,
            ),
            expected_etag=restored.opaque_etag,
            idempotency_key="move-selected-after-source-delete",
            now=now,
        )
        resource, modified = await read()
        release = ChoiceClearCommand(
            command_type="CHOICE_CLEAR",
            day_index=1,
            choice_group_token=modified.result.days[0].choice_selections[0].choice_group_token,
            preserve_activities=True,
        )
        outcome = await service.apply_command(
            resource, release, expected_etag=modified.opaque_etag, idempotency_key="release-modified-choice", now=now
        )
        repeat = await service.apply_command(
            resource, release, expected_etag=modified.opaque_etag, idempotency_key="release-modified-choice", now=now
        )
        assert repeat.replayed and repeat.opaque_etag == outcome.opaque_etag
        resource, released = await read()
        assert not released.result.days[0].choice_selections
        assert [c.name for d in released.result.days for c in d.activities] == [
            c.name for d in modified.result.days for c in d.activities
        ]
        await service.apply_command(
            resource,
            UndoCommand(command_type="UNDO"),
            expected_etag=released.opaque_etag,
            idempotency_key="undo-release",
            now=now,
        )
        resource, restored_modified = await read()
        assert restored_modified.result.days[0].choice_selections[0].status == "MODIFIED"

        # Write a genuinely old public shape as another immutable result. Do
        # not UPDATE an old snapshot or reconstruct it from current defaults.
        class LegacyResult(UserFacingTripResult):
            @model_serializer(mode="wrap")
            def old_shape(self, handler):
                payload = handler(self)
                for day in payload["days"]:
                    day.pop("choice_selections", None)
                    for alternative in day["alternatives"]:
                        for field in (
                            "choice_group_selectable",
                            "insertion_position",
                            "after_activity_token",
                            "before_activity_token",
                            "source_details",
                            "meal_role",
                            "start_time",
                            "end_time",
                            "visit_duration_minutes",
                            "timing_source",
                            "locked",
                            "fixed_commitment",
                        ):
                            alternative.pop(field, None)
                return payload

        legacy_created = await service.create_full(
            CreateFullRequest.model_validate({"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE}}),
            owner_user_id="experience-owner",
            idempotency_key="legacy-choice-create",
            now=now,
        )
        legacy_job = await repository.claim_next(worker_id="legacy-choice", now=now, lease_seconds=60)
        output = await build_choice_result()
        output.public_result = LegacyResult.model_validate(output.public_result.model_dump())
        await repository.complete_job(legacy_job, output, now=now)
        legacy_resource = await repository.authorize(
            legacy_created.accepted.public_resource_id, capability_hash=None, user_id="experience-owner", now=now
        )
        old = await repository.get_result(legacy_resource)
        assert not old.result.days[0].choice_selections
        if kind == "postgres":
            assert all(item.insertion_position is None for item in old.result.days[0].alternatives)
            assert all(not item.choice_group_selectable for item in old.result.days[0].alternatives)
            raw = json.loads(
                await repository._pool.fetchval(
                    "SELECT public_json FROM trip_understanding_results WHERE result_id=$1",
                    legacy_resource.current_result_id,
                )
            )
            assert all("choice_selections" not in day for day in raw["days"])
