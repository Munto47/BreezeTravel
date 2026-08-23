"""Deterministic eval-only materialization for P5 duplicate/concurrent apply faults.

The harness deliberately depends on an injected apply function and side-effect
probe.  It does not add a product runtime lock or change repair semantics.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evals.trip_check_v1.p5.data_contract import digest


FaultProfileId = Literal["duplicate_apply", "concurrent_apply"]
ApplyAttempt = Callable[[dict[str, Any]], Awaitable[Any]]
SideEffectProbe = Callable[[], Mapping[str, int] | Awaitable[Mapping[str, int]]]


class _FaultAttemptV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    repair_id: str = Field(min_length=1)
    actor_user_id: str = Field(min_length=1)
    base_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1)


class _BarrierSpecV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-apply-barrier-v2"] = (
        "trip-check-p5-apply-barrier-v2"
    )
    mode: Literal["SEQUENTIAL_REPLAY", "ARRIVE_ALL_THEN_ORDERED_RELEASE"]
    participant_count: Literal[2] = 2
    release_order: list[str] = Field(min_length=2, max_length=2)


class _FaultScriptV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-apply-fault-script-v2"] = (
        "trip-check-p5-apply-fault-script-v2"
    )
    case_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    fault_profile_id: FaultProfileId
    timeout_seconds: float = Field(gt=0)
    barrier: _BarrierSpecV2
    attempts: list[_FaultAttemptV2] = Field(min_length=2, max_length=2)
    script_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _SideEffectsV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    current_revision: int = Field(ge=0)
    revision_count: int = Field(ge=0)
    apply_command_count: int = Field(ge=0)


class _AttemptReceiptV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: str
    ordinal: int
    repair_id: str
    idempotency_key: str
    outcome: Literal[
        "APPLIED",
        "IDEMPOTENT_REPLAY",
        "STALE",
        "CONFLICT",
        "IDEMPOTENCY_KEY_REUSED",
        "TIMEOUT",
        "ERROR",
    ]
    result: dict[str, Any] | None = None
    error_category: str | None = None


def _script_payload(script: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in script.items() if key != "script_sha256"}


def build_concurrency_fault_script(
    *,
    case_id: str,
    workspace_id: str,
    repair_id: str,
    base_revision: int,
    fault_profile_id: FaultProfileId,
    actor_user_id: str = "p5-eval-runner",
    second_repair_id: str | None = None,
    idempotency_key: str | None = None,
    contender_idempotency_key: str | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Build a stable two-attempt fault script without runtime-only values."""

    first_key = idempotency_key or f"p5:{case_id}:{fault_profile_id}:01"
    if fault_profile_id == "duplicate_apply":
        second_key = contender_idempotency_key or first_key
        if second_key != first_key:
            raise ValueError("duplicate_apply requires the same idempotency key")
        second_id = second_repair_id or repair_id
        if second_id != repair_id:
            raise ValueError("duplicate_apply requires the same repair id")
        barrier_mode = "SEQUENTIAL_REPLAY"
    else:
        second_key = contender_idempotency_key or f"p5:{case_id}:{fault_profile_id}:02"
        if second_key == first_key:
            raise ValueError("concurrent_apply requires different idempotency keys")
        second_id = second_repair_id or repair_id
        barrier_mode = "ARRIVE_ALL_THEN_ORDERED_RELEASE"

    attempts = [
        {
            "attempt_id": f"{case_id}:apply-01",
            "ordinal": 0,
            "repair_id": repair_id,
            "actor_user_id": actor_user_id,
            "base_revision": base_revision,
            "idempotency_key": first_key,
        },
        {
            "attempt_id": f"{case_id}:apply-02",
            "ordinal": 1,
            "repair_id": second_id,
            "actor_user_id": actor_user_id,
            "base_revision": base_revision,
            "idempotency_key": second_key,
        },
    ]
    payload = {
        "schema_version": "trip-check-p5-apply-fault-script-v2",
        "case_id": case_id,
        "workspace_id": workspace_id,
        "fault_profile_id": fault_profile_id,
        "timeout_seconds": timeout_seconds,
        "barrier": {
            "schema_version": "trip-check-p5-apply-barrier-v2",
            "mode": barrier_mode,
            "participant_count": 2,
            "release_order": [attempt["attempt_id"] for attempt in attempts],
        },
        "attempts": attempts,
    }
    script = {**payload, "script_sha256": digest(payload)}
    return _FaultScriptV2.model_validate(script).model_dump(mode="json")


def _validate_script(raw_script: Mapping[str, Any]) -> _FaultScriptV2:
    script = _FaultScriptV2.model_validate(raw_script)
    serialized = script.model_dump(mode="json")
    if digest(_script_payload(serialized)) != script.script_sha256:
        raise ValueError("fault script hash mismatch")
    attempts = sorted(script.attempts, key=lambda value: value.ordinal)
    if [item.ordinal for item in attempts] != [0, 1]:
        raise ValueError("fault script ordinals must be exactly 0 and 1")
    if script.barrier.release_order != [item.attempt_id for item in attempts]:
        raise ValueError("barrier release order must match attempt ordinals")
    first, second = attempts
    if script.fault_profile_id == "duplicate_apply":
        if first.repair_id != second.repair_id or first.idempotency_key != second.idempotency_key:
            raise ValueError("duplicate_apply script identity mismatch")
        if script.barrier.mode != "SEQUENTIAL_REPLAY":
            raise ValueError("duplicate_apply requires sequential replay barrier")
    else:
        if first.idempotency_key == second.idempotency_key:
            raise ValueError("concurrent_apply script keys must differ")
        if script.barrier.mode != "ARRIVE_ALL_THEN_ORDERED_RELEASE":
            raise ValueError("concurrent_apply requires arrival barrier")
    return script


async def _probe_side_effects(probe: SideEffectProbe) -> _SideEffectsV2:
    value = probe()
    if inspect.isawaitable(value):
        value = await value
    return _SideEffectsV2.model_validate(value)


def _stable_result_projection(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        payload = {}
    repair = payload.get("repair") if isinstance(payload.get("repair"), Mapping) else {}
    return {
        "new_revision": payload.get("new_revision"),
        "postcheck_report_id": payload.get("postcheck_report_id"),
        "repair_status": repair.get("status"),
        "idempotent_replay": bool(payload.get("idempotent_replay", False)),
    }


def _classify_error(exc: Exception) -> str:
    # Importing the product exception classes is unnecessary here: eval adapters
    # may surface equivalent errors from HTTP or an isolated process.  Stable
    # categories intentionally key off the public domain class names.
    name = type(exc).__name__
    if name == "RepairStaleError":
        return "STALE"
    if name == "RevisionConflictError":
        return "CONFLICT"
    if name == "IdempotencyKeyReusedError":
        return "IDEMPOTENCY_KEY_REUSED"
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    return "ERROR"


async def _execute_attempt(
    attempt: _FaultAttemptV2,
    *,
    apply_attempt: ApplyAttempt,
    timeout_seconds: float,
    on_enter: Callable[[], None] | None = None,
) -> _AttemptReceiptV2:
    async def invoke() -> Any:
        if on_enter is not None:
            on_enter()
        return await apply_attempt(attempt.model_dump(mode="json"))

    try:
        result = await asyncio.wait_for(
            invoke(),
            timeout=timeout_seconds,
        )
        projection = _stable_result_projection(result)
        outcome = "IDEMPOTENT_REPLAY" if projection["idempotent_replay"] else "APPLIED"
        return _AttemptReceiptV2(
            attempt_id=attempt.attempt_id,
            ordinal=attempt.ordinal,
            repair_id=attempt.repair_id,
            idempotency_key=attempt.idempotency_key,
            outcome=outcome,
            result=projection,
        )
    except Exception as exc:  # every attempted apply must have a receipt
        return _AttemptReceiptV2(
            attempt_id=attempt.attempt_id,
            ordinal=attempt.ordinal,
            repair_id=attempt.repair_id,
            idempotency_key=attempt.idempotency_key,
            outcome=_classify_error(exc),
            error_category=type(exc).__name__,
        )


class _OrderedArrivalBarrier:
    def __init__(self, release_order: Sequence[str]) -> None:
        self._arrived: set[str] = set()
        self._all_arrived = asyncio.Event()
        self._release = {attempt_id: asyncio.Event() for attempt_id in release_order}
        self._entered_apply = {attempt_id: asyncio.Event() for attempt_id in release_order}
        self._release_order = list(release_order)

    async def arrive_and_wait(self, attempt_id: str) -> None:
        self._arrived.add(attempt_id)
        if len(self._arrived) == len(self._release):
            self._all_arrived.set()
        await self._release[attempt_id].wait()

    def mark_apply_entered(self, attempt_id: str) -> None:
        self._entered_apply[attempt_id].set()

    async def release_all(self, timeout_seconds: float) -> None:
        await asyncio.wait_for(self._all_arrived.wait(), timeout=timeout_seconds)
        for attempt_id in self._release_order:
            self._release[attempt_id].set()
            # The next contender is not released until this contender has
            # actually entered the injected apply coroutine. Product locking
            # still decides the mutation; the eval scheduler decides only the
            # stable arrival/release order.
            await asyncio.wait_for(
                self._entered_apply[attempt_id].wait(),
                timeout=timeout_seconds,
            )

    @property
    def arrived_count(self) -> int:
        return len(self._arrived)

    @property
    def entered_apply_count(self) -> int:
        return sum(event.is_set() for event in self._entered_apply.values())


def _counter_delta(before: _SideEffectsV2, after: _SideEffectsV2) -> dict[str, int]:
    return {
        "current_revision": after.current_revision - before.current_revision,
        "revision_count": after.revision_count - before.revision_count,
        "apply_command_count": after.apply_command_count - before.apply_command_count,
    }


def _semantic_projection(
    *,
    script: _FaultScriptV2,
    attempts: Sequence[_AttemptReceiptV2],
    before: _SideEffectsV2,
    after_first: _SideEffectsV2 | None,
    after: _SideEffectsV2,
) -> dict[str, Any]:
    outcomes = [item.outcome for item in sorted(attempts, key=lambda value: value.ordinal)]
    outcome_counts = dict(sorted(Counter(outcomes).items()))
    total_delta = _counter_delta(before, after)
    common = {
        "base_revision_advanced_once": (
            before.current_revision == script.attempts[0].base_revision
            and after.current_revision == script.attempts[0].base_revision + 1
        ),
        "one_revision_created": total_delta["revision_count"] == 1,
        "one_apply_command_created": total_delta["apply_command_count"] == 1,
    }
    if script.fault_profile_id == "duplicate_apply":
        profile_invariants = {
            "one_apply_one_replay": outcomes == ["APPLIED", "IDEMPOTENT_REPLAY"],
            "replay_added_no_side_effect": after_first == after,
        }
    else:
        profile_invariants = {
            "one_apply_one_stale_or_conflict": (
                outcome_counts.get("APPLIED") == 1
                and outcome_counts.get("STALE", 0) + outcome_counts.get("CONFLICT", 0) == 1
                and sum(outcome_counts.values()) == 2
            )
        }
    invariants = {**common, **profile_invariants}
    return {
        "schema_version": "trip-check-p5-apply-fault-semantic-projection-v2",
        "case_id": script.case_id,
        "fault_profile_id": script.fault_profile_id,
        "script_sha256": script.script_sha256,
        "outcome_counts": outcome_counts,
        "side_effect_delta": total_delta,
        "invariants": invariants,
        "all_invariants_passed": all(invariants.values()),
    }


async def execute_concurrency_fault(
    script: Mapping[str, Any],
    *,
    apply_attempt: ApplyAttempt,
    side_effect_probe: SideEffectProbe,
) -> dict[str, Any]:
    """Execute a duplicate/concurrent fault and always return attempt receipts."""

    validated = _validate_script(script)
    attempts = sorted(validated.attempts, key=lambda value: value.ordinal)
    before = await _probe_side_effects(side_effect_probe)
    after_first: _SideEffectsV2 | None = None

    if validated.fault_profile_id == "duplicate_apply":
        receipts = []
        for attempt in attempts:
            receipts.append(
                await _execute_attempt(
                    attempt,
                    apply_attempt=apply_attempt,
                    timeout_seconds=validated.timeout_seconds,
                )
            )
            snapshot = await _probe_side_effects(side_effect_probe)
            if attempt.ordinal == 0:
                after_first = snapshot
        barrier_receipt = {
            "mode": validated.barrier.mode,
            "participant_count": 2,
            "arrived_count": 2,
            "entered_apply_count": 2,
            "all_arrived_before_release": False,
            "release_order": validated.barrier.release_order,
        }
    else:
        barrier = _OrderedArrivalBarrier(validated.barrier.release_order)

        async def contender(attempt: _FaultAttemptV2) -> _AttemptReceiptV2:
            await barrier.arrive_and_wait(attempt.attempt_id)
            return await _execute_attempt(
                attempt,
                apply_attempt=apply_attempt,
                timeout_seconds=validated.timeout_seconds,
                on_enter=lambda: barrier.mark_apply_entered(attempt.attempt_id),
            )

        tasks = [asyncio.create_task(contender(attempt)) for attempt in attempts]
        try:
            await barrier.release_all(validated.timeout_seconds)
            receipts = list(await asyncio.gather(*tasks))
        except Exception:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        barrier_receipt = {
            "mode": validated.barrier.mode,
            "participant_count": 2,
            "arrived_count": barrier.arrived_count,
            "entered_apply_count": barrier.entered_apply_count,
            "all_arrived_before_release": barrier.arrived_count == 2,
            "release_order": validated.barrier.release_order,
        }

    after = await _probe_side_effects(side_effect_probe)
    projection = _semantic_projection(
        script=validated,
        attempts=receipts,
        before=before,
        after_first=after_first,
        after=after,
    )
    semantic_hash = digest(projection)
    error_categories = sorted(
        {item.error_category for item in receipts if item.error_category is not None}
    )
    receipt = {
        "schema_version": "trip-check-p5-apply-fault-receipt-v2",
        "status": "PASS" if projection["all_invariants_passed"] else "FAIL",
        "case_id": validated.case_id,
        "workspace_id": validated.workspace_id,
        "fault_profile_id": validated.fault_profile_id,
        "script_sha256": validated.script_sha256,
        "barrier": barrier_receipt,
        "attempts": [
            item.model_dump(mode="json")
            for item in sorted(receipts, key=lambda value: value.ordinal)
        ],
        "side_effects": {
            "before": before.model_dump(mode="json"),
            "after_first": (
                after_first.model_dump(mode="json") if after_first is not None else None
            ),
            "after": after.model_dump(mode="json"),
        },
        "error_categories": error_categories,
        "semantic_projection": projection,
        "semantic_hash": semantic_hash,
    }
    receipt["receipt_sha256"] = digest(receipt)
    return receipt
