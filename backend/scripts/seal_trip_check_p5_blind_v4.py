"""Seal/activate P5 v4 through the unchanged isolated v2 truth custody."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.seal_v4 import SealPathsV4, seal_and_freeze_v4  # noqa: E402
from scripts.validate_trip_check_p5_dataset_v4 import validate as validate_v4  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the strict P5 v4 blind seal and activate v4."
    )
    parser.add_argument("--external-bundle", type=Path, required=True)
    parser.add_argument("--external-review-receipt", type=Path, required=True)
    parser.add_argument("--candidate-freeze-commit", required=True)
    parser.add_argument("--candidate-manifest-hash", required=True)
    args = parser.parse_args()
    result = seal_and_freeze_v4(
        paths=SealPathsV4.for_repo(REPO_ROOT),
        external_bundle_path=args.external_bundle.resolve(),
        external_review_receipt_path=args.external_review_receipt.resolve(),
        candidate_freeze_commit=args.candidate_freeze_commit,
        candidate_manifest_hash=args.candidate_manifest_hash,
        nonformal_validator=lambda: validate_v4(formal=False),
        formal_validator=lambda: validate_v4(formal=True),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
