import json
from pathlib import Path

from app.repairs.strategies import (
    BoundedRepairStrategy,
    CpSatRepairStrategy,
    RoutingTsptwStrategy,
    StrategyStatus,
    execute_strategy,
    problem_from_bakeoff_case,
)


DATASET = Path(__file__).parents[1] / "evals" / "trip_check_v1" / "p4" / "solver_bakeoff_v1.jsonl"


def _case(scenario: str):
    return next(
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["scenario"] == scenario
    )


def test_all_strategies_solve_a_real_repairable_schedule_deterministically():
    problem = problem_from_bakeoff_case(_case("repairable_time_overlap"))
    bounded = BoundedRepairStrategy()
    for strategy in (bounded, RoutingTsptwStrategy(), CpSatRepairStrategy()):
        first = execute_strategy(strategy, problem, timeout_ms=2000, fallback=bounded)
        replay = execute_strategy(strategy, problem, timeout_ms=2000, fallback=bounded)
        assert first.primary.status is StrategyStatus.SUCCESS
        assert first.receipt.replay_hash == replay.receipt.replay_hash
        assert len(first.primary.schedule) == len(problem.stops)


def test_unsat_timeout_and_error_are_explicit_and_never_solver_success():
    bounded = BoundedRepairStrategy()
    unsat = execute_strategy(
        CpSatRepairStrategy(),
        problem_from_bakeoff_case(_case("locked_time_overlap")),
        timeout_ms=2000,
        fallback=bounded,
    )
    timeout = execute_strategy(
        CpSatRepairStrategy(),
        problem_from_bakeoff_case(_case("solver_deadline")),
        timeout_ms=2000,
        fallback=bounded,
    )
    error = execute_strategy(
        CpSatRepairStrategy(),
        problem_from_bakeoff_case(_case("strategy_exception")),
        timeout_ms=2000,
        fallback=bounded,
    )

    assert unsat.primary.status is StrategyStatus.UNSAT
    assert timeout.primary.status is StrategyStatus.TIMEOUT
    assert error.primary.status is StrategyStatus.ERROR
    assert timeout.receipt.fallback_status is StrategyStatus.SUCCESS
    assert error.receipt.fallback_status is StrategyStatus.SUCCESS


def test_cp_sat_25_stop_performance_case_stays_under_frozen_deadline():
    problem = problem_from_bakeoff_case(_case("dense_25_stop"))

    result = execute_strategy(
        CpSatRepairStrategy(),
        problem,
        timeout_ms=2000,
        fallback=BoundedRepairStrategy(),
    )

    assert result.primary.status is StrategyStatus.SUCCESS
    assert result.receipt.duration_ms <= 2000
    assert len(result.primary.schedule) == 25
