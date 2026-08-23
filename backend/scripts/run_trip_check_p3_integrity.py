from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from pathlib import Path

from evals.trip_check_v1.input_provider_integrity_runner import (
    DEFAULT_OUTPUT,
    run_live_matrix,
    run_snapshot_matrix,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
    ).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Trip Check P3 Provider integrity matrices")
    parser.add_argument("--commit-sha", default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    commit_sha = args.commit_sha or _head()
    manifest = asyncio.run(
        run_live_matrix(commit_sha=commit_sha, output=args.output)
        if args.live
        else run_snapshot_matrix(commit_sha=commit_sha, output=args.output)
    )
    print(json.dumps({"status": manifest["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if manifest["status"] in {"PASS", "NOT_RUN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
