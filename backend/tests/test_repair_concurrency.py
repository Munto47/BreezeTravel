from __future__ import annotations

import asyncio

import pytest

from app.repairs.errors import InvalidRepairDecisionError, RepairStaleError
from app.repairs.models import RepairStatus
from tests.test_repairs import _repair_context


@pytest.mark.asyncio
async def test_in_memory_apply_and_reject_choose_exactly_one_terminal_state(monkeypatch):
    itinerary_repository, audit_repository, repair_repository, search, _, source_report = (
        await _repair_context()
    )
    option = (await search.propose(source_report.report_id))[0]

    # Hold apply after it has entered the decision critical section.  A reject
    # started now must wait instead of observing and updating the same PROPOSED
    # option independently.
    apply_reached_postcheck = asyncio.Event()
    allow_apply_to_finish = asyncio.Event()
    original_get_report = audit_repository.get_report

    async def delayed_get_report(report_id: str):
        apply_reached_postcheck.set()
        await allow_apply_to_finish.wait()
        return await original_get_report(report_id)

    monkeypatch.setattr(audit_repository, "get_report", delayed_get_report)
    apply_task = asyncio.create_task(
        repair_repository.apply_option(
            option.repair_id,
            actor_user_id="apply-user",
            if_match_revision=1,
            idempotency_key="concurrent-apply",
        )
    )
    await asyncio.wait_for(apply_reached_postcheck.wait(), timeout=1)
    reject_task = asyncio.create_task(
        repair_repository.reject_option(
            option.repair_id,
            actor_user_id="reject-user",
            reason="concurrent reject",
        )
    )
    await asyncio.sleep(0)
    assert reject_task.done() is False

    allow_apply_to_finish.set()
    apply_result, reject_result = await asyncio.gather(
        apply_task,
        reject_task,
        return_exceptions=True,
    )

    assert apply_result.new_revision == 2
    assert isinstance(reject_result, RepairStaleError)
    stored = await repair_repository.get_option(option.repair_id)
    workspace = await itinerary_repository.get_workspace(option.result_preview.workspace_id)
    assert stored.status == RepairStatus.APPLIED
    assert workspace.current_itinerary_revision == 2
    assert workspace.current_report_id == option.postcheck_report_id


@pytest.mark.asyncio
async def test_reject_replay_is_safe_only_for_same_actor_and_normalized_reason():
    _, _, repair_repository, search, _, source_report = await _repair_context()
    option = (await search.propose(source_report.report_id))[0]

    rejected = await repair_repository.reject_option(
        option.repair_id,
        actor_user_id="decision-user",
        reason="  路线改动太大  ",
    )
    replay = await repair_repository.reject_option(
        option.repair_id,
        actor_user_id="decision-user",
        reason="路线改动太大",
    )

    assert replay == rejected
    with pytest.raises(RepairStaleError):
        await repair_repository.reject_option(
            option.repair_id,
            actor_user_id="another-user",
            reason="路线改动太大",
        )
    with pytest.raises(RepairStaleError):
        await repair_repository.reject_option(
            option.repair_id,
            actor_user_id="decision-user",
            reason="另一个理由",
        )
    assert await repair_repository.get_option(option.repair_id) == rejected


@pytest.mark.asyncio
async def test_repository_blank_reject_reason_is_a_domain_error():
    _, _, repair_repository, search, _, source_report = await _repair_context()
    option = (await search.propose(source_report.report_id))[0]

    with pytest.raises(InvalidRepairDecisionError) as invalid:
        await repair_repository.reject_option(
            option.repair_id,
            actor_user_id="decision-user",
            reason="   ",
        )

    assert invalid.value.status_code == 422


@pytest.mark.asyncio
async def test_applied_option_cannot_be_rejected_as_replay():
    _, _, repair_repository, search, _, source_report = await _repair_context()
    option = (await search.propose(source_report.report_id))[0]
    await repair_repository.apply_option(
        option.repair_id,
        actor_user_id="decision-user",
        if_match_revision=1,
        idempotency_key="apply-before-reject",
    )

    with pytest.raises(RepairStaleError):
        await repair_repository.reject_option(
            option.repair_id,
            actor_user_id="decision-user",
            reason="anything",
        )
