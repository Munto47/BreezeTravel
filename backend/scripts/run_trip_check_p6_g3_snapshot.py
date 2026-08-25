"""Run the fail-closed P6 G3 controlled-snapshot gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p6.contracts_v1 import P6ContractError  # noqa: E402
from evals.trip_check_v1.p6.snapshot_runner import run_snapshot_gate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-run-spec", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=BACKEND_ROOT.parent)
    args = parser.parse_args()
    try:
        receipt = asyncio.run(
            run_snapshot_gate(
                candidate_run_spec_path=args.candidate_run_spec,
                output_root=args.output_root,
                repo_root=args.repo_root,
            )
        )
    except P6ContractError as exc:
        print(json.dumps({"status": "REJECT", "reason_code": exc.reason_code}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"status": "REJECT", "reason_code": "P6_G3_INTERNAL_ERROR"}))
        return 1
    print(json.dumps({"status": "PASS", "receipt_hash": receipt["receipt_hash"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
