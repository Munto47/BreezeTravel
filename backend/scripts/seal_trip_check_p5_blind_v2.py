"""Seal and activate the frozen P5 v2 blind dataset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.seal_v2 import SealPathsV2, seal_and_activate_v2  # noqa: E402
from scripts.validate_trip_check_p5_dataset_v2 import validate as validate_dataset_v2  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the strict P5 v2 blind seal and activate the v2 formal contract."
    )
    parser.add_argument("--labels-canonical-sha256", required=True)
    parser.add_argument("--external-bundle-sha256", required=True)
    parser.add_argument("--review-receipt-sha256", required=True)
    parser.add_argument("--candidate-freeze-commit", required=True)
    args = parser.parse_args()
    result = seal_and_activate_v2(
        paths=SealPathsV2.for_repo(REPO_ROOT),
        labels_canonical_sha256=args.labels_canonical_sha256,
        external_bundle_sha256=args.external_bundle_sha256,
        review_receipt_sha256=args.review_receipt_sha256,
        candidate_freeze_commit=args.candidate_freeze_commit,
        dataset_validator=lambda: validate_dataset_v2(formal=True),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
