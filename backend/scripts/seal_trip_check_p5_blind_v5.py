"""Seal and activate the exact clean/pushed P5 v5 custody candidate."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.seal_v5 import (  # noqa: E402
    SealPathsV5,
    seal_and_freeze_v5,
)


REPO_ROOT = BACKEND_ROOT.parent


def _validate(*, formal: bool) -> dict:
    command = [
        sys.executable,
        str(BACKEND_ROOT / "scripts" / "validate_trip_check_p5_dataset_v5.py"),
    ]
    if formal:
        command.append("--formal")
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return json.loads(completed.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--external-bundle", type=Path, required=True)
    parser.add_argument("--external-correction-receipt", type=Path, required=True)
    parser.add_argument("--external-review-receipt", type=Path, required=True)
    parser.add_argument("--external-bundle-sha256", required=True)
    parser.add_argument("--labels-canonical-sha256", required=True)
    parser.add_argument("--correction-receipt-sha256", required=True)
    parser.add_argument("--review-receipt-sha256", required=True)
    parser.add_argument("--candidate-freeze-commit", required=True)
    parser.add_argument("--candidate-manifest-hash", required=True)
    args = parser.parse_args()
    result = seal_and_freeze_v5(
        paths=SealPathsV5.for_repo(REPO_ROOT),
        external_bundle_path=args.external_bundle.resolve(),
        external_correction_receipt_path=args.external_correction_receipt.resolve(),
        external_review_receipt_path=args.external_review_receipt.resolve(),
        expected_bundle_sha256=args.external_bundle_sha256,
        expected_labels_canonical_sha256=args.labels_canonical_sha256,
        expected_correction_receipt_sha256=args.correction_receipt_sha256,
        expected_review_receipt_sha256=args.review_receipt_sha256,
        candidate_freeze_commit=args.candidate_freeze_commit,
        candidate_manifest_hash=args.candidate_manifest_hash,
        nonformal_validator=lambda: _validate(formal=False),
        formal_validator=lambda: _validate(formal=True),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
