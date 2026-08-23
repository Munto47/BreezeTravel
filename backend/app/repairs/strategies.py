"""Internal P4 repair strategy contract and local-only solver implementations."""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.itineraries.hash_service import sha256_canonical


class StrategyStatus(str, Enum):
    SUCCESS = "SUCCESS"
    UNSAT = "UNSAT"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


class RepairProblemStop(BaseModel):
    model_config = ConfigDict(frozen=True)

    stop_id: str
    day_index: int = Field(ge=0, le=4)
    duration_minutes: int = Field(gt=0)
    earliest_start: int = Field(ge=0, lt=24 * 60)
    latest_end: int = Field(gt=0, le=24 * 60)
    original_start: int = Field(ge=0, lt=24 * 60)
    locked_start: int | None = Field(default=None, ge=0, lt=24 * 60)


class RepairProblem(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    case_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    city: str
    day_count: int = Field(ge=2, le=5)
    stops: tuple[RepairProblemStop, ...]
    travel_minutes: int = Field(default=15, ge=0)
    evidence_ready: bool = True
    fault_profile: str = "none"


class ScheduledStop(BaseModel):
    model_config = ConfigDict(frozen=True)

    stop_id: str
    day_index: int
    start_minute: int
    end_minute: int


class RepairStrategyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: StrategyStatus
    schedule: tuple[ScheduledStop, ...] = ()
    edit_cost: float | None = None
    route_cost: float | None = None
    failure_reason: str | None = None


class RepairStrategyReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy_id: str
    strategy_version: str
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: StrategyStatus
    duration_ms: float = Field(ge=0)
    edit_cost: float | None = None
    route_cost: float | None = None
    failure_reason: str | None = None
    fallback_strategy_id: str | None = None
    fallback_status: StrategyStatus | None = None
    replay_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class StrategyExecution(BaseModel):
    model_config = ConfigDict(frozen=True)

    primary: RepairStrategyResult
    effective: RepairStrategyResult
    receipt: RepairStrategyReceipt


class RepairStrategy(Protocol):
    strategy_id: str
    strategy_version: str

    def solve(self, problem: RepairProblem, *, timeout_ms: int) -> RepairStrategyResult: ...


def _schedule_result(problem: RepairProblem, starts: dict[str, int]) -> RepairStrategyResult:
    by_id = {stop.stop_id: stop for stop in problem.stops}
    schedule = tuple(
        ScheduledStop(
            stop_id=stop_id,
            day_index=by_id[stop_id].day_index,
            start_minute=start,
            end_minute=start + by_id[stop_id].duration_minutes,
        )
        for stop_id, start in sorted(
            starts.items(),
            key=lambda item: (by_id[item[0]].day_index, item[1], item[0]),
        )
    )
    edit_cost = sum(abs(item.start_minute - by_id[item.stop_id].original_start) for item in schedule)
    route_edges = sum(
        max(0, len([item for item in schedule if item.day_index == day_index]) - 1)
        for day_index in range(5)
    )
    return RepairStrategyResult(
        status=StrategyStatus.SUCCESS,
        schedule=schedule,
        edit_cost=float(edit_cost),
        route_cost=float(route_edges * problem.travel_minutes),
    )


def _fixed_order_schedule(
    problem: RepairProblem,
    ordered: Sequence[RepairProblemStop],
) -> RepairStrategyResult:
    if not problem.evidence_ready:
        return RepairStrategyResult(
            status=StrategyStatus.UNSAT,
            failure_reason="REQUIRED_EVIDENCE_UNAVAILABLE",
        )
    starts: dict[str, int] = {}
    day_end: dict[int, int] = {}
    for stop in ordered:
        earliest = max(stop.earliest_start, day_end.get(stop.day_index, stop.earliest_start))
        start = stop.locked_start if stop.locked_start is not None else max(earliest, stop.original_start)
        if start < earliest or start + stop.duration_minutes > stop.latest_end:
            return RepairStrategyResult(
                status=StrategyStatus.UNSAT,
                failure_reason="TIME_WINDOW_OR_LOCK_CONFLICT",
            )
        starts[stop.stop_id] = start
        day_end[stop.day_index] = start + stop.duration_minutes + problem.travel_minutes
    return _schedule_result(problem, starts)


class BoundedRepairStrategy:
    strategy_id = "bounded_repair_v1"
    strategy_version = "1.0.0"

    def solve(self, problem: RepairProblem, *, timeout_ms: int) -> RepairStrategyResult:
        del timeout_ms
        ordered = sorted(
            problem.stops,
            key=lambda item: (item.day_index, item.original_start, item.stop_id),
        )
        return _fixed_order_schedule(problem, ordered)


class RoutingTsptwStrategy:
    strategy_id = "routing_tsptw_v1"
    strategy_version = "1.0.0"

    def solve(self, problem: RepairProblem, *, timeout_ms: int) -> RepairStrategyResult:
        del timeout_ms
        ordered = sorted(
            problem.stops,
            key=lambda item: (
                item.day_index,
                item.latest_end,
                item.locked_start is None,
                item.original_start,
                item.stop_id,
            ),
        )
        return _fixed_order_schedule(problem, ordered)


class CpSatRepairStrategy:
    strategy_id = "cp_sat_v1"
    strategy_version = "1.0.0-ortools-9.15"

    def solve(self, problem: RepairProblem, *, timeout_ms: int) -> RepairStrategyResult:
        if not problem.evidence_ready:
            return RepairStrategyResult(
                status=StrategyStatus.UNSAT,
                failure_reason="REQUIRED_EVIDENCE_UNAVAILABLE",
            )
        backend_root = Path(__file__).resolve().parents[2]
        worker = backend_root / "scripts" / "p4_cp_sat_worker.py"
        try:
            completed = subprocess.run(
                [sys.executable, str(worker), "--solver-timeout-ms", str(int(timeout_ms * 0.65))],
                cwd=backend_root,
                input=problem.model_dump_json(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_ms / 1000,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RepairStrategyResult(
                status=StrategyStatus.TIMEOUT,
                failure_reason="CP_SAT_PROCESS_DEADLINE_EXCEEDED",
            )
        if completed.returncode != 0:
            return RepairStrategyResult(
                status=StrategyStatus.ERROR,
                failure_reason="CP_SAT_WORKER_FAILED",
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return RepairStrategyResult(
                status=StrategyStatus.ERROR,
                failure_reason="CP_SAT_WORKER_OUTPUT_INVALID",
            )
        if payload.get("status") == StrategyStatus.SUCCESS.value:
            return _schedule_result(problem, payload["starts"])
        return RepairStrategyResult.model_validate(payload)


def execute_strategy(
    strategy: RepairStrategy,
    problem: RepairProblem,
    *,
    timeout_ms: int,
    fallback: RepairStrategy | None = None,
) -> StrategyExecution:
    started = perf_counter()
    try:
        if strategy.strategy_id != BoundedRepairStrategy.strategy_id:
            if problem.fault_profile == "forced_timeout":
                primary = RepairStrategyResult(
                    status=StrategyStatus.TIMEOUT,
                    failure_reason="CONTROLLED_DEADLINE_FAULT",
                )
            elif problem.fault_profile == "forced_exception":
                raise RuntimeError("controlled strategy exception")
            else:
                primary = strategy.solve(problem, timeout_ms=timeout_ms)
        else:
            primary = strategy.solve(problem, timeout_ms=timeout_ms)
    except Exception as exc:
        primary = RepairStrategyResult(
            status=StrategyStatus.ERROR,
            failure_reason=type(exc).__name__,
        )
    duration_ms = (perf_counter() - started) * 1000
    fallback_result = None
    if primary.status is not StrategyStatus.SUCCESS and fallback is not None:
        fallback_result = fallback.solve(problem, timeout_ms=timeout_ms)
    effective = (
        fallback_result
        if fallback_result is not None and fallback_result.status is StrategyStatus.SUCCESS
        else primary
    )
    config_hash = sha256_canonical({
        "strategy_id": strategy.strategy_id,
        "strategy_version": strategy.strategy_version,
        "timeout_ms": timeout_ms,
        "fallback_strategy_id": fallback.strategy_id if fallback else None,
    })
    input_hash = sha256_canonical(problem.model_dump(mode="json"))
    output_hash = sha256_canonical(primary.model_dump(mode="json"))
    replay_hash = sha256_canonical({
        "strategy_id": strategy.strategy_id,
        "config_hash": config_hash,
        "input_hash": input_hash,
        "output_hash": output_hash,
        "fallback_output_hash": (
            sha256_canonical(fallback_result.model_dump(mode="json"))
            if fallback_result is not None
            else None
        ),
    })
    receipt = RepairStrategyReceipt(
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
        config_hash=config_hash,
        input_hash=input_hash,
        output_hash=output_hash,
        status=primary.status,
        duration_ms=duration_ms,
        edit_cost=primary.edit_cost,
        route_cost=primary.route_cost,
        failure_reason=primary.failure_reason,
        fallback_strategy_id=fallback.strategy_id if fallback_result is not None else None,
        fallback_status=fallback_result.status if fallback_result is not None else None,
        replay_hash=replay_hash,
    )
    return StrategyExecution(primary=primary, effective=effective, receipt=receipt)


def problem_from_bakeoff_case(case: dict) -> RepairProblem:
    source = case["input"]
    scenario = case["scenario"]
    day_count = int(source["days"])
    stop_count = int(source["stop_count"])
    stops = []
    for index in range(stop_count):
        day_index = index % day_count
        position = index // day_count
        original_start = 9 * 60 + position * 40
        locked_start = None
        if scenario == "locked_time_overlap" and index < 2:
            day_index = 0
            original_start = 9 * 60
            locked_start = original_start
        stops.append(RepairProblemStop(
            stop_id=f"{case['case_id']}:stop-{index:02d}",
            day_index=day_index,
            duration_minutes=60,
            earliest_start=8 * 60,
            latest_end=21 * 60,
            original_start=original_start,
            locked_start=locked_start,
        ))
    return RepairProblem(
        case_id=case["case_id"],
        case_hash=case["case_hash"],
        city=case["city"],
        day_count=day_count,
        stops=tuple(stops),
        evidence_ready=scenario not in {
            "missing_route_evidence",
            "empty_candidate_set",
            "conflicting_place_receipt",
        },
        fault_profile=(
            "forced_timeout"
            if scenario == "solver_deadline"
            else "forced_exception"
            if scenario == "strategy_exception"
            else "none"
        ),
    )
