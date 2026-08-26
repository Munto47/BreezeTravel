"""Isolated OR-Tools worker used only by the P4 local bake-off."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.repairs.strategies import RepairProblem, RepairStrategyResult, StrategyStatus  # noqa: E402


def solve(problem: RepairProblem, *, timeout_ms: int) -> dict:
    # OR-Tools imports pandas only for optional dataframe helpers.  Masking a
    # broken optional pyarrow install keeps native solver loading isolated from
    # the main backend test/runtime process.
    sys.modules["pyarrow"] = None
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    starts = {
        stop.stop_id: model.new_int_var(
            stop.earliest_start,
            stop.latest_end - stop.duration_minutes,
            f"start:{stop.stop_id}",
        )
        for stop in problem.stops
    }
    deviations = []
    for stop in problem.stops:
        if stop.locked_start is not None:
            model.add(starts[stop.stop_id] == stop.locked_start)
        deviation = model.new_int_var(0, 24 * 60, f"deviation:{stop.stop_id}")
        model.add_abs_equality(deviation, starts[stop.stop_id] - stop.original_start)
        deviations.append(deviation)
    for day in range(5):
        day_stops = [stop for stop in problem.stops if stop.day_index == day]
        for left_index, left in enumerate(day_stops):
            for right in day_stops[left_index + 1:]:
                left_before = model.new_bool_var(f"before:{left.stop_id}:{right.stop_id}")
                model.add(
                    starts[right.stop_id]
                    >= starts[left.stop_id] + left.duration_minutes + problem.travel_minutes
                ).only_enforce_if(left_before)
                model.add(
                    starts[left.stop_id]
                    >= starts[right.stop_id] + right.duration_minutes + problem.travel_minutes
                ).only_enforce_if(left_before.negated())
    model.minimize(sum(deviations))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.001, timeout_ms / 1000)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 20260823
    solver.parameters.stop_after_first_solution = True
    status = solver.solve(model)
    if status in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return {
            "status": StrategyStatus.SUCCESS.value,
            "starts": {stop.stop_id: solver.value(starts[stop.stop_id]) for stop in problem.stops},
        }
    if status == cp_model.INFEASIBLE:
        return RepairStrategyResult(
            status=StrategyStatus.UNSAT,
            failure_reason="CP_SAT_INFEASIBLE",
        ).model_dump(mode="json")
    return RepairStrategyResult(
        status=StrategyStatus.TIMEOUT,
        failure_reason="CP_SAT_DEADLINE_EXCEEDED",
    ).model_dump(mode="json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--solver-timeout-ms", type=int, required=True)
    args = parser.parse_args()
    problem = RepairProblem.model_validate_json(sys.stdin.read())
    print(json.dumps(solve(problem, timeout_ms=args.solver_timeout_ms), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
