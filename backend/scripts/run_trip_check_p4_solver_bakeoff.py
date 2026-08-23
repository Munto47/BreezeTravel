"""Run the frozen P4 strategy comparison and emit hash-bound receipts."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.audit.engine import AuditEngine
from app.audit.evidence_service import EvidenceObservation, EvidenceService
from app.audit.models import AuditReport, AuditRunInput
from app.audit.system_constraints import with_system_constraints
from app.itineraries.hash_service import sha256_canonical, with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    ResolutionStatus,
    RevisionSource,
    RevisionTransport,
    TripDateRange,
)
from app.repairs.errors import RepairUnsafePostcheckError
from app.repairs.objective import assert_repair_postcheck_safe
from app.repairs.strategies import (
    BoundedRepairStrategy,
    CpSatRepairStrategy,
    RepairProblem,
    RepairStrategyResult,
    RoutingTsptwStrategy,
    ScheduledStop,
    StrategyStatus,
    execute_strategy,
    problem_from_bakeoff_case,
)
from app.schemas.task_spec import DateRange, TripTaskSpec


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DATASET = BACKEND_ROOT / "evals" / "trip_check_v1" / "p4" / "solver_bakeoff_v1.jsonl"
NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)


def _clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def _schedule_revision(
    problem: RepairProblem,
    schedule: tuple[ScheduledStop, ...],
    *,
    revision: int,
) -> Any:
    trip_range = TripDateRange(
        start=date(2026, 10, 1),
        end=date(2026, 10, 1) + timedelta(days=problem.day_count - 1),
    )
    by_day = {
        day_index: sorted(
            [item for item in schedule if item.day_index == day_index],
            key=lambda item: (item.start_minute, item.stop_id),
        )
        for day_index in range(problem.day_count)
    }
    days = []
    for day_index, scheduled in by_day.items():
        stops = []
        for order_index, item in enumerate(scheduled):
            category = (
                "hotel"
                if order_index == len(scheduled) - 1
                else "food"
                if 11 * 60 + 30 <= item.start_minute < 13 * 60 + 30
                or 18 * 60 <= item.start_minute < 20 * 60
                else "attraction"
            )
            stops.append(ItineraryStop(
                stop_id=item.stop_id,
                place_id=f"place:{item.stop_id}",
                day_index=day_index,
                order_index=order_index,
                raw_name=f"P4 fixture {item.stop_id}",
                category=category,
                start_time=_clock(item.start_minute),
                end_time=_clock(item.end_minute),
                visit_duration_minutes=item.end_minute - item.start_minute,
                transport_to_next=(
                    RevisionTransport(mode="driving", duration_minutes=problem.travel_minutes)
                    if order_index < len(scheduled) - 1
                    else None
                ),
                resolution_status=ResolutionStatus.USER_CONFIRMED,
            ))
        days.append(ItineraryDay(
            day_index=day_index,
            date=trip_range.start + timedelta(days=day_index),
            stops=stops,
        ))
    return with_content_hash(ItineraryRevisionContent(
        itinerary_id=f"itinerary:{problem.case_id}",
        workspace_id=f"workspace:{problem.case_id}",
        revision=revision,
        parent_revision=revision - 1 if revision > 1 else None,
        source_type=RevisionSource.REPAIR if revision > 1 else RevisionSource.IMPORT,
        city=problem.city,
        date_range=trip_range,
        days=days,
        created_by="p4-solver-bakeoff",
        created_at=NOW,
    ))


def _snapshot(revision) -> Any:
    service = EvidenceService()
    places = {
        stop.place_id: {
            "place_id": stop.place_id,
            "name": stop.raw_name,
            "city": revision.city,
            "category": stop.category,
            "opening_hours": "08:00-23:00",
            "retrieval_observed_at": NOW,
        }
        for day in revision.days
        for stop in day.stops
    }
    observations = service.observations_from_revision(revision, places, now=NOW)
    observations.extend(
        EvidenceObservation(
            subject_type="ROUTE_EDGE",
            subject_id=f"{left.stop_id}->{right.stop_id}",
            fact_type="ROUTE_TIME",
            value={"mode": "driving", "duration_minutes": left.transport_to_next.duration_minutes},
            provider="controlled_p4_route_fixture",
            observed_at=NOW,
        )
        for day in revision.days
        for left, right in zip(day.stops, day.stops[1:])
    )
    return service.create_snapshot(
        workspace_id=revision.workspace_id,
        itinerary_revision=revision.revision,
        observations=observations,
        now=NOW,
    )


def _semantic_report_hash(report: AuditReport) -> str:
    return sha256_canonical({
        "overall_status": report.overall_status.value,
        "findings": sorted(
            (
                item.rule_id,
                item.status.value,
                item.severity.value,
                item.reason_code,
                tuple(item.affected_days),
                tuple(item.affected_stop_ids),
            )
            for item in report.findings
        ),
    })


def _full_postcheck(
    problem: RepairProblem,
    result: RepairStrategyResult,
) -> dict[str, Any]:
    source_schedule = tuple(
        ScheduledStop(
            stop_id=stop.stop_id,
            day_index=stop.day_index,
            start_minute=stop.original_start,
            end_minute=stop.original_start + stop.duration_minutes,
        )
        for stop in problem.stops
    )
    source_revision = _schedule_revision(problem, source_schedule, revision=1)
    result_revision = _schedule_revision(problem, result.schedule, revision=2)
    source_snapshot = _snapshot(source_revision)
    result_snapshot = _snapshot(result_revision)
    task = with_system_constraints(TripTaskSpec(
        task_id=f"task:{problem.case_id}",
        room_id=f"room:{problem.case_id}",
        task_revision=1,
        city=problem.city,
        date_range=DateRange(start=source_revision.date_range.start, days=problem.day_count),
    ))
    engine = AuditEngine()
    source_report = engine.run(
        run_input=AuditRunInput(
            workspace_id=source_revision.workspace_id,
            itinerary_revision=1,
            task_id=task.task_id,
            task_revision=1,
            place_resolution_versions={stop.place_id: 1 for day in source_revision.days for stop in day.stops},
        ),
        revision=source_revision,
        task_spec=task,
        evidence_snapshot=source_snapshot,
        now=NOW,
    )
    postcheck = engine.run(
        run_input=AuditRunInput(
            workspace_id=result_revision.workspace_id,
            itinerary_revision=2,
            task_id=task.task_id,
            task_revision=1,
            place_resolution_versions={stop.place_id: 1 for day in result_revision.days for stop in day.stops},
        ),
        revision=result_revision,
        task_spec=task,
        evidence_snapshot=result_snapshot,
        supersedes_report_id=source_report.report_id,
        now=NOW,
    )
    try:
        assert_repair_postcheck_safe(source_report, postcheck)
        safety_status = "PASS"
        safety_reason = None
    except RepairUnsafePostcheckError as exc:
        safety_status = "FAIL"
        safety_reason = exc.context
    return {
        "status": safety_status,
        "reason": safety_reason,
        "source_overall_status": source_report.overall_status.value,
        "postcheck_overall_status": postcheck.overall_status.value,
        "semantic_report_hash": _semantic_report_hash(postcheck),
        "audit_rule_set_version": postcheck.audit_rule_set_version,
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _oracle_pass(case: dict, strategy_id: str, status: StrategyStatus) -> bool:
    expected = case["oracle"]["expected_outcome"]
    if expected in {"FEASIBLE", "PERFORMANCE"}:
        return status is StrategyStatus.SUCCESS
    if expected == "UNSAT":
        return status is StrategyStatus.UNSAT
    if expected == "TIMEOUT":
        return (
            status is StrategyStatus.SUCCESS
            if strategy_id == BoundedRepairStrategy.strategy_id
            else status is StrategyStatus.TIMEOUT
        )
    if expected == "FALLBACK":
        return (
            status is StrategyStatus.SUCCESS
            if strategy_id == BoundedRepairStrategy.strategy_id
            else status is StrategyStatus.ERROR
        )
    return False


def run_bakeoff(*, commit_sha: str, output_dir: Path) -> dict[str, Any]:
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line]
    bounded = BoundedRepairStrategy()
    strategies = (bounded, RoutingTsptwStrategy(), CpSatRepairStrategy())
    records: list[dict[str, Any]] = []
    for strategy in strategies:
        for case in cases:
            problem = problem_from_bakeoff_case(case)
            fallback = None if strategy.strategy_id == bounded.strategy_id else bounded
            execution = execute_strategy(strategy, problem, timeout_ms=2000, fallback=fallback)
            replay = execute_strategy(strategy, problem, timeout_ms=2000, fallback=fallback)
            postcheck = (
                _full_postcheck(problem, execution.effective)
                if execution.effective.status is StrategyStatus.SUCCESS
                else {"status": "NOT_RUN", "reason": execution.effective.failure_reason}
            )
            records.append({
                "case_id": case["case_id"],
                "case_hash": case["case_hash"],
                "city": case["city"],
                "scenario": case["scenario"],
                "strategy_id": strategy.strategy_id,
                "oracle_outcome": case["oracle"]["expected_outcome"],
                "oracle_pass": _oracle_pass(case, strategy.strategy_id, execution.primary.status),
                "primary": execution.primary.model_dump(mode="json"),
                "effective": execution.effective.model_dump(mode="json"),
                "receipt": execution.receipt.model_dump(mode="json"),
                "replay_match": execution.receipt.replay_hash == replay.receipt.replay_hash,
                "postcheck": postcheck,
            })
    metrics: dict[str, Any] = {}
    solved_classes: dict[str, set[str]] = {}
    for strategy in strategies:
        selected = [row for row in records if row["strategy_id"] == strategy.strategy_id]
        primary_success = [row for row in selected if row["primary"]["status"] == "SUCCESS"]
        effective_success = [row for row in selected if row["effective"]["status"] == "SUCCESS"]
        dense = [row["receipt"]["duration_ms"] for row in selected if row["scenario"] == "dense_25_stop"]
        solved_classes[strategy.strategy_id] = {row["scenario"] for row in primary_success}
        metrics[strategy.strategy_id] = {
            "case_count": len(selected),
            "primary_success_count": len(primary_success),
            "primary_success_rate": len(primary_success) / len(selected),
            "effective_success_count": len(effective_success),
            "status_counts": dict(sorted(Counter(row["primary"]["status"] for row in selected).items())),
            "failure_classes": sorted({row["scenario"] for row in selected if row["primary"]["status"] != "SUCCESS"}),
            "p95_ms": _percentile([row["receipt"]["duration_ms"] for row in selected], 0.95),
            "dense_25_stop_p95_ms": _percentile(dense, 0.95),
            "mean_edit_cost": (
                sum(row["primary"]["edit_cost"] for row in primary_success) / len(primary_success)
                if primary_success else None
            ),
            "mean_route_cost": (
                sum(row["primary"]["route_cost"] for row in primary_success) / len(primary_success)
                if primary_success else None
            ),
            "timeout_count": sum(row["primary"]["status"] == "TIMEOUT" for row in selected),
            "fallback_count": sum(row["receipt"]["fallback_strategy_id"] is not None for row in selected),
            "oracle_pass_count": sum(row["oracle_pass"] for row in selected),
            "replay_match_count": sum(row["replay_match"] for row in selected),
            "postcheck_pass_count": sum(row["postcheck"]["status"] == "PASS" for row in selected),
        }
    bounded_metrics = metrics[bounded.strategy_id]
    cp_metrics = metrics[CpSatRepairStrategy.strategy_id]
    improvement_points = (
        cp_metrics["primary_success_rate"] - bounded_metrics["primary_success_rate"]
    ) * 100
    extra_classes = sorted(
        solved_classes[CpSatRepairStrategy.strategy_id] - solved_classes[bounded.strategy_id]
    )
    safety_pass = all(
        row["postcheck"]["status"] in {"PASS", "NOT_RUN"}
        for row in records
    )
    all_success_postchecked = all(
        row["postcheck"]["status"] == "PASS"
        for row in records
        if row["effective"]["status"] == "SUCCESS"
    )
    replay_pass = all(row["replay_match"] for row in records)
    performance_pass = (cp_metrics["dense_25_stop_p95_ms"] or float("inf")) <= 2000
    effect_pass = improvement_points >= 10 or len(extra_classes) >= 3
    admitted = safety_pass and all_success_postchecked and replay_pass and performance_pass and effect_pass
    manifest: dict[str, Any] = {
        "schema_version": "trip-check-p4-solver-bakeoff-result-v1",
        "subject_commit": commit_sha,
        "dataset_path": DATASET.relative_to(BACKEND_ROOT).as_posix(),
        "dataset_hash": sha256_canonical(cases),
        "run_spec": cases[0]["run_spec"],
        "case_count": len(cases),
        "record_count": len(records),
        "metrics": metrics,
        "admission": {
            "status": "PASS" if admitted else "REJECT",
            "cp_sat_primary_improvement_percentage_points": improvement_points,
            "cp_sat_extra_solved_classes": extra_classes,
            "safety_pass": safety_pass,
            "authoritative_postcheck_pass": all_success_postchecked,
            "replay_pass": replay_pass,
            "performance_pass": performance_pass,
            "effect_threshold_pass": effect_pass,
            "default_strategy": "cp_sat_v1" if admitted else "bounded_repair_v1",
        },
        "evidence_boundary": {
            "execution_mode": "controlled_fixture",
            "live_provider": "NOT_RUN",
            "public_e2e": "NOT_RUN",
            "human_evidence": "NOT_RUN",
            "frozen_blind": "NOT_RUN",
        },
    }
    manifest["records_hash"] = sha256_canonical(records)
    manifest["manifest_hash"] = sha256_canonical(manifest)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "strategy_receipts.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in records),
        encoding="utf-8",
        newline="\n",
    )
    (output_dir / "solver_bakeoff_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_bakeoff(commit_sha=args.commit_sha, output_dir=args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
