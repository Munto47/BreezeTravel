"""Run the fail-closed P6 G1 real-authorized OCR gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p6.contracts_v1 import P6ContractError  # noqa: E402
from evals.trip_check_v1.p6.real_ocr_runner import run_sync  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-run-spec", type=Path, required=True)
    parser.add_argument("--dataset-manifest", type=Path, required=True)
    parser.add_argument("--private-manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--log-root", type=Path, action="append", required=True)
    parser.add_argument("--repo-root", type=Path, default=BACKEND_ROOT.parent)
    args = parser.parse_args()
    try:
        receipt = run_sync(
            candidate_run_spec_path=args.candidate_run_spec,
            dataset_manifest_path=args.dataset_manifest,
            private_manifest_path=args.private_manifest,
            output_root=args.output_root,
            work_root=args.work_root,
            repo_root=args.repo_root,
            formal=True,
            database_url=os.environ.get("DATABASE_URL"),
            log_roots=args.log_root,
        )
    except P6ContractError as exc:
        print(json.dumps({"status": "REJECT", "reason_code": exc.reason_code}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"status": "REJECT", "reason_code": "P6_REAL_OCR_INTERNAL_ERROR"}))
        return 1
    print(json.dumps({"status": "PASS", "receipt_hash": receipt["receipt_hash"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
