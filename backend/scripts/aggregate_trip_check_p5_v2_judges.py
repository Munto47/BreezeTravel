"""Validate and aggregate exactly three independent no-tool P5 v2 Judge rounds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_check_v1.p5.active_contract import require_v2_formal_ready
from evals.trip_check_v1.p5.judge_v2 import aggregate_judge_rounds_v2


REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--mapping-sha256", required=True)
    parser.add_argument("--round", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require_v2_formal_ready()
    report = aggregate_judge_rounds_v2(
        repo_root=REPO_ROOT,
        mapping_path=args.mapping,
        mapping_sha256=args.mapping_sha256,
        round_paths=args.round,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8", newline="\n")
    if json.loads(args.output.read_text(encoding="utf-8")) != report:
        raise RuntimeError("P5 v2 Judge panel readback failed")
    print(payload, end="")


if __name__ == "__main__":
    main()
