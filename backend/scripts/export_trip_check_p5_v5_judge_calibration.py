"""Export three separately-custodied non-blind P5 Judge calibration inputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.judge_calibration_v1 import (  # noqa: E402
    export_judge_calibration_bundles_v1,
)


REPO_ROOT = BACKEND_ROOT.parent
P5_ROOT = REPO_ROOT / "backend" / "evals" / "trip_check_v1" / "p5"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--round-output-dir", type=Path, action="append", required=True
    )
    parser.add_argument("--custody-output-dir", type=Path, required=True)
    parser.add_argument("--rubric", type=Path, default=P5_ROOT / "judge_rubric_v2.json")
    parser.add_argument(
        "--protocol", type=Path, default=P5_ROOT / "judge_protocol_v1.json"
    )
    parser.add_argument(
        "--calibration-set",
        type=Path,
        default=P5_ROOT / "judge_calibration_v1.json",
    )
    args = parser.parse_args()
    if len(args.round_output_dir) != 3:
        parser.error("--round-output-dir must be supplied exactly three times")
    receipt = export_judge_calibration_bundles_v1(
        repo_root=REPO_ROOT,
        round_output_dirs=args.round_output_dir,
        custody_output_dir=args.custody_output_dir,
        rubric_path=args.rubric,
        protocol_path=args.protocol,
        calibration_set_path=args.calibration_set,
    )
    output = args.custody_output_dir / "judge_calibration_export_receipt.v1.json"
    payload = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.write_text(payload, encoding="utf-8", newline="\n")
    if json.loads(output.read_text(encoding="utf-8")) != receipt:
        raise RuntimeError("P5_JUDGE_CALIBRATION_EXPORT_READBACK_FAILED")
    print(payload, end="")


if __name__ == "__main__":
    main()
