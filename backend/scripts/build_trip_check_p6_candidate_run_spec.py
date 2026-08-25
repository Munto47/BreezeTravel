"""Build the immutable P6 CandidateRunSpec input bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p6.candidate_spec_builder import build_candidate_run_spec  # noqa: E402
from evals.trip_check_v1.p6.contracts_v1 import P6ContractError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ocr-dataset-manifest", type=Path, required=True)
    parser.add_argument("--p5-gate-manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=BACKEND_ROOT.parent)
    args = parser.parse_args()
    try:
        spec, path = build_candidate_run_spec(
            ocr_dataset_manifest_path=args.ocr_dataset_manifest,
            p5_gate_manifest_path=args.p5_gate_manifest,
            output_root=args.output_root,
            repo_root=args.repo_root,
        )
    except P6ContractError as exc:
        print(json.dumps({"status": "REJECT", "reason_code": exc.reason_code}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"status": "REJECT", "reason_code": "P6_CANDIDATE_INPUT_INTERNAL_ERROR"}))
        return 1
    print(json.dumps({
        "status": "PASS",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "candidate_run_spec_path": str(path.resolve()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
