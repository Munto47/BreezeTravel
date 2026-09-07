"""Run the fail-closed G07 fixed 18-call map and weather live gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.g07_candidate.live_provider_runner import (  # noqa: E402
    run_g07_live_provider_gate,
)
from evals.trip_check_v1.p6.contracts_v1 import P6ContractError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-run-spec", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--live-env-file", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=BACKEND_ROOT.parent)
    args = parser.parse_args()
    try:
        receipt = asyncio.run(
            run_g07_live_provider_gate(
                candidate_run_spec_path=args.candidate_run_spec,
                output_root=args.output_root,
                repo_root=args.repo_root,
                live_env_file=args.live_env_file,
            )
        )
    except P6ContractError as exc:
        print(json.dumps({"status": "REJECT", "reason_code": exc.reason_code}))
        return 1
    except Exception:
        print(json.dumps({"status": "REJECT", "reason_code": "G07_G4_INTERNAL_ERROR"}))
        return 1
    print(
        json.dumps(
            {"status": "PASS", "receipt_hash": receipt["receipt_hash"]},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
