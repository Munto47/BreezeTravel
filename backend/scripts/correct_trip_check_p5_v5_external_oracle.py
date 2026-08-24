"""Isolated custodian: correct only the authorized P5 v5 oracle field."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.oracle_correction_v5 import (  # noqa: E402
    correct_external_oracle_v5,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--corrected-bundle", type=Path, required=True)
    parser.add_argument("--correction-receipt", type=Path, required=True)
    parser.add_argument("--candidate-subject-commit", required=True)
    args = parser.parse_args()
    result = correct_external_oracle_v5(
        repo_root=BACKEND_ROOT.parent,
        source_bundle_path=args.source_bundle,
        corrected_bundle_path=args.corrected_bundle,
        correction_receipt_path=args.correction_receipt,
        candidate_subject_commit=args.candidate_subject_commit,
        entrypoint_path=Path(__file__).resolve(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
