"""Export three separately-custodied anonymous P5 v4 Judge inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_check_v1.p5.judge_v4 import export_judge_bundles_v4


REPO_ROOT = Path(__file__).resolve().parents[2]
P5_ROOT = REPO_ROOT / "backend" / "evals" / "trip_check_v1" / "p5"


def _require_active_v4() -> None:
    payload = json.loads((P5_ROOT / "active_contract.json").read_text(encoding="utf-8"))
    if (
        payload.get("active_contract") != "trip-check-p5-v4"
        or payload.get("formal_evidence_status") != "READY"
    ):
        raise RuntimeError("P5_V4_FORMAL_CONTRACT_NOT_READY")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--round-output-dir",
        type=Path,
        action="append",
        required=True,
        help="Repeat exactly three times; each directory goes to one Judge only.",
    )
    parser.add_argument("--custody-output-dir", type=Path, required=True)
    parser.add_argument("--rubric", type=Path, default=P5_ROOT / "judge_rubric_v2.json")
    args = parser.parse_args()
    if len(args.round_output_dir) != 3:
        parser.error("--round-output-dir must be supplied exactly three times")
    _require_active_v4()
    receipt = export_judge_bundles_v4(
        repo_root=REPO_ROOT,
        run_dir=args.run_dir,
        round_output_dirs=args.round_output_dir,
        custody_output_dir=args.custody_output_dir,
        rubric_path=args.rubric,
    )
    output = args.custody_output_dir / "judge_export_receipt.v4.json"
    payload = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.write_text(payload, encoding="utf-8", newline="\n")
    if json.loads(output.read_text(encoding="utf-8")) != receipt:
        raise RuntimeError("P5_V4_JUDGE_EXPORT_RECEIPT_READBACK_FAILED")
    print(payload, end="")


if __name__ == "__main__":
    main()
