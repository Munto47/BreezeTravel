"""Score a complete P5 non-blind run group using the deterministic oracle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_check_v1.p5.scorer import score_run_group
from evals.trip_check_v1.p5.active_contract import require_v2_formal_ready


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = BACKEND_ROOT / "evals" / "trip_check_v1" / "p5" / "cases_nonblind_v1.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--allow-partial-development-run", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.allow_partial_development_run:
        require_v2_formal_ready()
    report = score_run_group(
        run_dir=args.run_dir.resolve(),
        cases_path=args.cases.resolve(),
        require_full_nonblind=not args.allow_partial_development_run,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8", newline="\n")
    print(payload, end="")


if __name__ == "__main__":
    main()
