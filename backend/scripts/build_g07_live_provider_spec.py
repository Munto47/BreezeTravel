"""Build the repository-external G07 adapter spec for the fixed live matrix."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.g07_candidate.live_spec_builder import (  # noqa: E402
    build_g07_live_provider_spec,
)
from evals.trip_check_v1.p6.contracts_v1 import P6ContractError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=BACKEND_ROOT.parent)
    args = parser.parse_args()
    try:
        spec, path = build_g07_live_provider_spec(
            output_root=args.output_root,
            repo_root=args.repo_root,
        )
    except P6ContractError as exc:
        print(json.dumps({"status": "REJECT", "reason_code": exc.reason_code}))
        return 1
    except Exception:
        print(
            json.dumps(
                {"status": "REJECT", "reason_code": "G07_LIVE_SPEC_INTERNAL_ERROR"}
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": "PASS",
                "subject_commit": spec["subject_commit"],
                "run_spec_hash": spec["run_spec_hash"],
                "candidate_run_spec_path": str(path.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
