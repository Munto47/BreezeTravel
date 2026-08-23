"""Export three anonymous, balanced, no-oracle P5 Judge bundles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_check_v1.p5.judge import export_judge_bundles


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUBRIC = REPO_ROOT / "backend" / "evals" / "trip_check_v1" / "p5" / "judge_rubric_v1.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rubric", type=Path, default=DEFAULT_RUBRIC)
    args = parser.parse_args()
    receipt = export_judge_bundles(
        repo_root=REPO_ROOT,
        run_dir=args.run_dir,
        cases_path=args.cases,
        output_dir=args.output_dir,
        rubric_path=args.rubric,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
