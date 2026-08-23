from __future__ import annotations

import asyncio
import copy

import pytest

from app.itineraries.errors import RevisionConflictError
from evals.trip_check_v1.p5.concurrency_materialization_v2 import (
    build_concurrency_fault_script,
    execute_concurrency_fault,
)
from tests.test_repairs import _repair_context


async def _real_repair_harness():
    itinerary_repository, _, repair_repository, search, _, source_report = await _repair_context()
    option = (await search.propose(source_report.report_id))[0]
    workspace_id = option.result_preview.workspace_id

    async def apply_attempt(attempt: dict):
        return await repair_repository.apply_option(
            attempt["repair_id"],
            actor_user_id=attempt["actor_user_id"],
            if_match_revision=attempt["base_revision"],
            idempotency_key=attempt["idempotency_key"],
        )

    async def side_effect_probe():
        workspace = await itinerary_repository.get_workspace(workspace_id)
        return {
            "current_revision": workspace.current_itinerary_revision,
            "revision_count": sum(
                candidate_workspace_id == workspace_id
                for candidate_workspace_id, _ in itinerary_repository.revisions
            ),
            "apply_command_count": sum(
                candidate_workspace_id == workspace_id
                for candidate_workspace_id, _ in repair_repository.idempotency
            ),
        }

    return option, apply_attempt, side_effect_probe


class _DeterministicApplyState:
    def __init__(self) -> None:
        self.current_revision = 1
        self.revision_count = 1
        self.apply_command_count = 0
        self.results: dict[str, dict] = {}
        self.lock = asyncio.Lock()

    async def apply(self, attempt: dict):
        async with self.lock:
            key = attempt["idempotency_key"]
            if key in self.results:
                return {**self.results[key], "idempotent_replay": True}
            if self.current_revision != attempt["base_revision"]:
                raise RevisionConflictError("stable conflict")
            self.current_revision += 1
            self.revision_count += 1
            self.apply_command_count += 1
            result = {
                "new_revision": self.current_revision,
                "postcheck_report_id": "report-postcheck-002",
                "repair": {"status": "APPLIED"},
                "idempotent_replay": False,
            }
            self.results[key] = result
            return result

    def probe(self):
        return {
            "current_revision": self.current_revision,
            "revision_count": self.revision_count,
            "apply_command_count": self.apply_command_count,
        }


def test_p5_v2_fault_scripts_are_stable_and_enforce_key_shape() -> None:
    duplicate = build_concurrency_fault_script(
        case_id="p5.dev.bj.duplicate-001",
        workspace_id="workspace-001",
        repair_id="repair-001",
        base_revision=1,
        fault_profile_id="duplicate_apply",
    )
    assert duplicate == build_concurrency_fault_script(
        case_id="p5.dev.bj.duplicate-001",
        workspace_id="workspace-001",
        repair_id="repair-001",
        base_revision=1,
        fault_profile_id="duplicate_apply",
    )
    assert duplicate["attempts"][0]["idempotency_key"] == duplicate["attempts"][1][
        "idempotency_key"
    ]
    assert duplicate["barrier"]["mode"] == "SEQUENTIAL_REPLAY"

    concurrent = build_concurrency_fault_script(
        case_id="p5.dev.bj.concurrent-001",
        workspace_id="workspace-001",
        repair_id="repair-001",
        base_revision=1,
        fault_profile_id="concurrent_apply",
    )
    assert concurrent["attempts"][0]["idempotency_key"] != concurrent["attempts"][1][
        "idempotency_key"
    ]
    assert concurrent["barrier"] == {
        "schema_version": "trip-check-p5-apply-barrier-v2",
        "mode": "ARRIVE_ALL_THEN_ORDERED_RELEASE",
        "participant_count": 2,
        "release_order": [
            "p5.dev.bj.concurrent-001:apply-01",
            "p5.dev.bj.concurrent-001:apply-02",
        ],
    }

    with pytest.raises(ValueError, match="same idempotency key"):
        build_concurrency_fault_script(
            case_id="bad-duplicate",
            workspace_id="workspace-001",
            repair_id="repair-001",
            base_revision=1,
            fault_profile_id="duplicate_apply",
            idempotency_key="first",
            contender_idempotency_key="second",
        )
    with pytest.raises(ValueError, match="different idempotency keys"):
        build_concurrency_fault_script(
            case_id="bad-concurrent",
            workspace_id="workspace-001",
            repair_id="repair-001",
            base_revision=1,
            fault_profile_id="concurrent_apply",
            idempotency_key="same",
            contender_idempotency_key="same",
        )


@pytest.mark.asyncio
async def test_p5_v2_duplicate_apply_replays_without_a_second_revision_or_command() -> None:
    option, apply_attempt, side_effect_probe = await _real_repair_harness()
    script = build_concurrency_fault_script(
        case_id="p5.regression.bj.duplicate-apply",
        workspace_id=option.result_preview.workspace_id,
        repair_id=option.repair_id,
        base_revision=option.base_itinerary_revision,
        fault_profile_id="duplicate_apply",
    )

    receipt = await execute_concurrency_fault(
        script,
        apply_attempt=apply_attempt,
        side_effect_probe=side_effect_probe,
    )

    assert receipt["status"] == "PASS"
    assert [attempt["outcome"] for attempt in receipt["attempts"]] == [
        "APPLIED",
        "IDEMPOTENT_REPLAY",
    ]
    assert receipt["semantic_projection"]["side_effect_delta"] == {
        "current_revision": 1,
        "revision_count": 1,
        "apply_command_count": 1,
    }
    assert receipt["semantic_projection"]["invariants"][
        "replay_added_no_side_effect"
    ] is True
    assert receipt["side_effects"]["after_first"] == receipt["side_effects"]["after"]


@pytest.mark.asyncio
async def test_p5_v2_concurrent_apply_has_one_winner_and_one_stale_or_conflict() -> None:
    option, apply_attempt, side_effect_probe = await _real_repair_harness()
    script = build_concurrency_fault_script(
        case_id="p5.regression.sh.concurrent-apply",
        workspace_id=option.result_preview.workspace_id,
        repair_id=option.repair_id,
        base_revision=option.base_itinerary_revision,
        fault_profile_id="concurrent_apply",
    )

    receipt = await execute_concurrency_fault(
        script,
        apply_attempt=apply_attempt,
        side_effect_probe=side_effect_probe,
    )

    assert receipt["status"] == "PASS"
    outcomes = [attempt["outcome"] for attempt in receipt["attempts"]]
    assert outcomes[0] == "APPLIED"
    assert outcomes[1] in {"STALE", "CONFLICT"}
    assert receipt["barrier"]["all_arrived_before_release"] is True
    assert receipt["barrier"]["release_order"] == script["barrier"]["release_order"]
    assert receipt["semantic_projection"]["side_effect_delta"] == {
        "current_revision": 1,
        "revision_count": 1,
        "apply_command_count": 1,
    }


@pytest.mark.asyncio
async def test_p5_v2_semantic_receipt_hash_is_stable_across_fresh_replays() -> None:
    script = build_concurrency_fault_script(
        case_id="p5.dev.hz.concurrent-stable",
        workspace_id="workspace-stable",
        repair_id="repair-stable",
        base_revision=1,
        fault_profile_id="concurrent_apply",
    )
    receipts = []
    for _ in range(2):
        state = _DeterministicApplyState()
        receipts.append(
            await execute_concurrency_fault(
                script,
                apply_attempt=state.apply,
                side_effect_probe=state.probe,
            )
        )

    assert receipts[0]["semantic_projection"] == receipts[1]["semantic_projection"]
    assert receipts[0]["semantic_hash"] == receipts[1]["semantic_hash"]
    assert receipts[0]["receipt_sha256"] == receipts[1]["receipt_sha256"]


@pytest.mark.asyncio
async def test_p5_v2_apply_errors_still_emit_machine_readable_receipts() -> None:
    script = build_concurrency_fault_script(
        case_id="p5.dev.bj.duplicate-error",
        workspace_id="workspace-error",
        repair_id="repair-error",
        base_revision=1,
        fault_profile_id="duplicate_apply",
    )
    state = {
        "current_revision": 1,
        "revision_count": 1,
        "apply_command_count": 0,
    }

    async def broken_apply(attempt: dict):
        del attempt
        raise RuntimeError("sensitive implementation detail")

    receipt = await execute_concurrency_fault(
        script,
        apply_attempt=broken_apply,
        side_effect_probe=lambda: state,
    )

    assert receipt["status"] == "FAIL"
    assert receipt["error_categories"] == ["RuntimeError"]
    assert [attempt["outcome"] for attempt in receipt["attempts"]] == ["ERROR", "ERROR"]
    assert all(attempt["error_category"] == "RuntimeError" for attempt in receipt["attempts"])
    assert "sensitive implementation detail" not in str(receipt)
    assert len(receipt["semantic_hash"]) == 64
    assert len(receipt["receipt_sha256"]) == 64


@pytest.mark.asyncio
async def test_p5_v2_tampered_fault_script_fails_before_apply() -> None:
    script = build_concurrency_fault_script(
        case_id="p5.dev.bj.tampered",
        workspace_id="workspace-001",
        repair_id="repair-001",
        base_revision=1,
        fault_profile_id="concurrent_apply",
    )
    tampered = copy.deepcopy(script)
    tampered["attempts"][1]["base_revision"] = 2
    invoked = False

    async def should_not_run(attempt: dict):
        nonlocal invoked
        invoked = True
        return attempt

    with pytest.raises(ValueError, match="hash mismatch"):
        await execute_concurrency_fault(
            tampered,
            apply_attempt=should_not_run,
            side_effect_probe=lambda: {
                "current_revision": 1,
                "revision_count": 1,
                "apply_command_count": 0,
            },
        )
    assert invoked is False
