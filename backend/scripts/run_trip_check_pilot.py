from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from pathlib import Path

from evals.trip_check_v1.pilot_runner import BACKEND_ROOT, DEFAULT_PILOT_PATH, run_pilot


def _current_commit() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=BACKEND_ROOT.parent,
        text=True,
    ).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute the controlled P1 TripCheck pilot")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_PILOT_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--commit-sha", default=None)
    args = parser.parse_args()
    result = asyncio.run(
        run_pilot(
            commit_sha=args.commit_sha or _current_commit(),
            dataset_path=args.dataset.resolve(),
            output_dir=args.output.resolve() if args.output else None,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
