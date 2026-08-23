from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from pathlib import Path

from evals.trip_check_v1.reliability_runner import (
    DEFAULT_OUTPUT,
    run_reliability_matrix,
    run_termination_worker,
)


def _head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        encoding="utf-8",
    ).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Trip Check P2 PostgreSQL reliability matrix")
    parser.add_argument("--commit-sha", default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--admin-dsn", default=None)
    parser.add_argument("--worker-dsn", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-run-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-ready-file", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-otel-path", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    worker_values = (
        args.worker_dsn,
        args.worker_run_id,
        args.worker_ready_file,
        args.worker_otel_path,
    )
    if any(worker_values):
        if not all(worker_values):
            parser.error("all hidden worker arguments are required together")
        asyncio.run(
            run_termination_worker(
                database_dsn=args.worker_dsn,
                run_id=args.worker_run_id,
                ready_file=args.worker_ready_file,
                otel_path=args.worker_otel_path,
            )
        )
        return 0
    manifest = asyncio.run(
        run_reliability_matrix(
            commit_sha=args.commit_sha or _head(),
            output=args.output,
            admin_dsn=args.admin_dsn,
        )
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "canonical_cases_passed": manifest["canonical_cases_passed"],
                "canonical_case_count": manifest["canonical_case_count"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
