"""Score an exact three-variant P5 v2 non-blind run group."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.active_contract import require_v2_formal_ready  # noqa: E402
from evals.trip_check_v1.p5.scorer_v2 import score_run_group_v2  # noqa: E402


P5_ROOT = BACKEND_ROOT / "evals" / "trip_check_v1" / "p5"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--dataset-manifest",
        type=Path,
        default=P5_ROOT / "dataset_v2.manifest.json",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=P5_ROOT / "cases_nonblind_v2.jsonl",
    )
    parser.add_argument(
        "--materializations",
        type=Path,
        default=P5_ROOT / "materializations_nonblind_v2.jsonl",
    )
    parser.add_argument("--allow-partial-development-run", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    formal = not args.allow_partial_development_run
    if formal:
        require_v2_formal_ready()
    report = score_run_group_v2(
        run_dir=args.run_dir.resolve(),
        cases_path=args.cases.resolve(),
        materializations_path=args.materializations.resolve(),
        dataset_manifest_path=args.dataset_manifest.resolve(),
        require_formal=formal,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8", newline="\n")
    print(payload, end="")


if __name__ == "__main__":
    main()
