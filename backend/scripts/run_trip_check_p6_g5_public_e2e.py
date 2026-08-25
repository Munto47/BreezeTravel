"""Run the fail-closed P6 G5 public health and controlled-snapshot full chain."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p6.contracts_v1 import P6ContractError  # noqa: E402
from evals.trip_check_v1.p6.public_e2e_runner import run_public_e2e  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-run-spec", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=BACKEND_ROOT.parent)
    args = parser.parse_args()
    try:
        health, e2e = run_public_e2e(
            candidate_run_spec_path=args.candidate_run_spec,
            output_root=args.output_root,
            repo_root=args.repo_root,
            credential_file=args.credential_file,
        )
    except P6ContractError as exc:
        print(json.dumps({"status": "REJECT", "reason_code": exc.reason_code}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"status": "REJECT", "reason_code": "P6_G5_PUBLIC_INTERNAL_ERROR"}))
        return 1
    print(json.dumps({
        "status": "PASS",
        "health_receipt_hash": health["receipt_hash"],
        "e2e_receipt_hash": e2e["receipt_hash"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
