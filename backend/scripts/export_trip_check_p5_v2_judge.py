"""Export three repository-external anonymous P5 v2 Judge bundles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_check_v1.p5.active_contract import require_v2_formal_ready
from evals.trip_check_v1.p5.judge_v2 import export_judge_bundles_v2


REPO_ROOT = Path(__file__).resolve().parents[2]
P5_ROOT = REPO_ROOT / "backend" / "evals" / "trip_check_v1" / "p5"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=P5_ROOT / "frozen_blind.v2.inputs.jsonl")
    parser.add_argument(
        "--materializations",
        type=Path,
        default=P5_ROOT / "frozen_blind.v2.materializations.jsonl",
    )
    parser.add_argument("--dataset", type=Path, default=P5_ROOT / "dataset_v2.manifest.json")
    parser.add_argument("--rubric", type=Path, default=P5_ROOT / "judge_rubric_v2.json")
    args = parser.parse_args()
    require_v2_formal_ready()
    receipt = export_judge_bundles_v2(
        repo_root=REPO_ROOT,
        run_dir=args.run_dir,
        cases_path=args.cases,
        materializations_path=args.materializations,
        dataset_manifest_path=args.dataset,
        output_dir=args.output_dir,
        rubric_path=args.rubric,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
