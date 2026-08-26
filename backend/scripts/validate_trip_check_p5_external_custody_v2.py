"""Validate external P5 v2 custodian and reviewer artifacts without generating labels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_check_v1.p5.blind_external_contract_v2 import (
    expected_blind_dataset_binding_v2,
    validate_external_blind_bundle_v2,
    validate_external_blind_review_receipt_v2,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Consumer-only validation for artifacts produced by repository-external independent "
            "custodian and reviewer agents. This command cannot generate blind labels."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--external-bundle", type=Path, required=True)
    parser.add_argument("--external-bundle-sha256", required=True)
    parser.add_argument("--external-review-receipt", type=Path, required=True)
    parser.add_argument("--review-receipt-sha256", required=True)
    parser.add_argument("--labels-canonical-sha256", required=True)
    parser.add_argument("--candidate-subject-commit", required=True)
    args = parser.parse_args()

    binding, case_ids = expected_blind_dataset_binding_v2(args.repo_root)
    bundle = validate_external_blind_bundle_v2(
        repo_root=args.repo_root,
        bundle_path=args.external_bundle,
        expected_bundle_sha256=args.external_bundle_sha256,
        expected_labels_canonical_sha256=args.labels_canonical_sha256,
        expected_dataset_binding=binding,
        expected_case_ids=case_ids,
    )
    review = validate_external_blind_review_receipt_v2(
        repo_root=args.repo_root,
        receipt_path=args.external_review_receipt,
        expected_receipt_sha256=args.review_receipt_sha256,
        expected_candidate_subject_commit=args.candidate_subject_commit,
        expected_bundle_sha256=args.external_bundle_sha256,
        expected_bundle_canonical_sha256=bundle["bundle_canonical_sha256"],
        expected_labels_canonical_sha256=args.labels_canonical_sha256,
        expected_dataset_binding=binding,
    )
    print(
        json.dumps(
            {
                "schema_version": "trip-check-p5-external-custody-validation-v2",
                "status": "PASS",
                "case_count": 90,
                "bundle_byte_sha256": bundle["bundle_byte_sha256"],
                "labels_canonical_sha256": bundle["labels_canonical_sha256"],
                "review_receipt_sha256": review["review_receipt_sha256"],
                "candidate_subject_commit": review["candidate_subject_commit"],
                "human_evidence": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
