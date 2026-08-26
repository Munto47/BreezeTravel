"""Seal the frozen P5 v3 blind dataset through isolated v2 truth custody."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.seal_v3 import SealPathsV3, seal_and_freeze_v3  # noqa: E402
from scripts.validate_trip_check_p5_dataset_v3 import validate as validate_dataset_v3  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the strict P5 v3 blind seal and freeze the v3 manifest."
    )
    parser.add_argument("--external-bundle", required=True, type=Path)
    parser.add_argument("--external-review-receipt", required=True, type=Path)
    parser.add_argument("--candidate-freeze-commit", required=True)
    parser.add_argument("--candidate-manifest-hash", required=True)
    args = parser.parse_args()
    result = seal_and_freeze_v3(
        paths=SealPathsV3.for_repo(REPO_ROOT),
        external_bundle_path=args.external_bundle,
        external_review_receipt_path=args.external_review_receipt,
        candidate_freeze_commit=args.candidate_freeze_commit,
        candidate_manifest_hash=args.candidate_manifest_hash,
        nonformal_validator=lambda: validate_dataset_v3(formal=False),
        formal_validator=lambda: validate_dataset_v3(formal=True),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
