"""Build and verify the P6 G6 immutable release and Candidate Gate artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p6.contracts_v1 import P6ContractError  # noqa: E402
from evals.trip_check_v1.p6.release_runner import (  # noqa: E402
    build_candidate_gate_decision,
    build_pre_gate_release,
    capture_final_disclosure,
    capture_pre_gate_readback,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase",
        choices=("build", "pre-readback", "decide", "final-readback"),
    )
    parser.add_argument("--candidate-run-spec", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=BACKEND_ROOT.parent)
    args = parser.parse_args()
    try:
        if args.phase == "build":
            manifest, _ = build_pre_gate_release(
                candidate_run_spec_path=args.candidate_run_spec,
                repo_root=args.repo_root,
            )
            artifact_hash = manifest["manifest_hash"]
        elif args.phase == "pre-readback":
            artifact_hash = capture_pre_gate_readback(
                candidate_run_spec_path=args.candidate_run_spec,
                repo_root=args.repo_root,
            )["receipt_hash"]
        elif args.phase == "decide":
            receipt, _ = build_candidate_gate_decision(
                candidate_run_spec_path=args.candidate_run_spec,
                repo_root=args.repo_root,
            )
            artifact_hash = receipt["receipt_hash"]
        else:
            artifact_hash = capture_final_disclosure(
                candidate_run_spec_path=args.candidate_run_spec,
                repo_root=args.repo_root,
            )["receipt_hash"]
    except P6ContractError as exc:
        print(json.dumps({"status": "REJECT", "reason_code": exc.reason_code}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"status": "REJECT", "reason_code": "P6_G6_INTERNAL_ERROR"}))
        return 1
    print(json.dumps({"status": "PASS", "artifact_hash": artifact_hash}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
