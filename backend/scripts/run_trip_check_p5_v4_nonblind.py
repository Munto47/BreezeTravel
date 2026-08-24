"""Execute the exact formal P5 v4 non-blind run outside the repository."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.nonblind_runner_v4 import (  # noqa: E402
    run_nonblind_v4,
)


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=REPO_ROOT, text=True, encoding="utf-8"
    ).strip()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="Existing repository-external parent directory for formal artifacts.",
    )
    value.add_argument("--run-id")
    value.add_argument(
        "--upstream-ref",
        help="Exact remote-tracking ref; defaults to the current branch upstream.",
    )
    return value


async def execute(args: argparse.Namespace) -> dict[str, object]:
    subject_commit = _git("rev-parse", "HEAD")
    upstream_ref = args.upstream_ref or _git(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    upstream_commit = _git("rev-parse", upstream_ref)
    dirty_tree = bool(_git("status", "--short"))
    run_id = args.run_id or datetime.now(timezone.utc).strftime(
        "p5-v4-nonblind-%Y%m%dT%H%M%SZ"
    )
    return await run_nonblind_v4(
        repo_root=REPO_ROOT,
        output_root=args.output_root.resolve(),
        run_id=run_id,
        subject_commit=subject_commit,
        upstream_ref=upstream_ref,
        upstream_commit=upstream_commit,
        dirty_tree=dirty_tree,
        require_formal=True,
    )


def main() -> None:
    result = asyncio.run(execute(parser().parse_args()))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
