"""Aggregate three slot-bound P5 Judge v3 sealed-holdout rounds."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.judge_holdout_v2 import (  # noqa: E402
    aggregate_judge_holdout_rounds_v2,
)


REPO_ROOT = BACKEND_ROOT.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--key-sha256", required=True)
    parser.add_argument("--round", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.round) != 3:
        parser.error("--round must be supplied exactly three times")
    report = aggregate_judge_holdout_rounds_v2(
        repo_root=REPO_ROOT,
        key_path=args.key,
        key_sha256=args.key_sha256,
        round_paths=args.round,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8", newline="\n")
    if json.loads(args.output.read_text(encoding="utf-8")) != report:
        raise RuntimeError("P5_JUDGE_V3_HOLDOUT_PANEL_READBACK_FAILED")
    print(payload, end="")


if __name__ == "__main__":
    main()
