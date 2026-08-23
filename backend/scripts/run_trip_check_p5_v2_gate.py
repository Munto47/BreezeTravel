"""Build the P5 v2 Evaluation Gate from sealed run, score, and Judge artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_check_v1.p5.active_contract import require_v2_formal_ready
from evals.trip_check_v1.p5.gate_v2 import build_p5_gate_manifest_v2


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "backend"
    / "evidence"
    / "trip_check_v1"
    / "p5"
    / "p5_gate_manifest_v2.json"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nonblind-run-dir", type=Path, required=True)
    parser.add_argument("--nonblind-score", type=Path, required=True)
    parser.add_argument("--blind-run-dir", type=Path, required=True)
    parser.add_argument("--blind-score", type=Path, required=True)
    parser.add_argument("--judge-panel", type=Path, required=True)
    parser.add_argument("--formal-validation-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    require_v2_formal_ready()
    manifest = build_p5_gate_manifest_v2(
        repo_root=REPO_ROOT,
        nonblind_run_dir=args.nonblind_run_dir,
        nonblind_score_path=args.nonblind_score,
        blind_run_dir=args.blind_run_dir,
        blind_score_path=args.blind_score,
        judge_panel_path=args.judge_panel,
        formal_validation_receipt_path=args.formal_validation_receipt,
        output_path=args.output,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
