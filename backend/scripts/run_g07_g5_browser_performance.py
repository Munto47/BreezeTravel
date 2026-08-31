"""Run exact-subject G07 browser or 50-chain live performance evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from evals.g07_candidate.browser_performance import (
    run_browser_evidence,
    run_live_performance_evidence,
)
from evals.trip_check_v1.p6.contracts_v1 import P6ContractError


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    browser = subparsers.add_parser("browser")
    browser.add_argument("--output-root", required=True, type=Path)
    browser.add_argument("--log-root", required=True, type=Path)
    browser.add_argument("--repo-root", type=Path, default=BACKEND_ROOT.parent)
    browser.add_argument(
        "--database-admin-url",
        default="postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )
    browser.add_argument("--redis-url", default="redis://127.0.0.1:6379")
    performance = subparsers.add_parser("performance")
    performance.add_argument("--output-root", required=True, type=Path)
    performance.add_argument("--repo-root", type=Path, default=BACKEND_ROOT.parent)
    performance.add_argument(
        "--database-admin-url",
        default="postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )
    args = parser.parse_args()
    try:
        if args.mode == "browser":
            receipt = run_browser_evidence(
                output_root=args.output_root,
                log_root=args.log_root,
                repo_root=args.repo_root,
                database_admin_url=args.database_admin_url,
                redis_url=args.redis_url,
            )
        else:
            receipt = asyncio.run(
                run_live_performance_evidence(
                    output_root=args.output_root,
                    repo_root=args.repo_root,
                    database_admin_url=args.database_admin_url,
                )
            )
    except P6ContractError as exc:
        print(
            json.dumps(
                {"status": "REJECT", "reason_code": exc.reason_code},
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "gate": receipt["gate"],
                "subject_commit": receipt["subject_commit"],
                "receipt_hash": receipt["receipt_hash"],
            },
            sort_keys=True,
        )
    )
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
