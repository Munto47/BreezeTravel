from __future__ import annotations

import argparse
from pathlib import Path

from evals.trip_text_cards_v1.contracts import canonical_sha256


def tranche_commitment_sha256(
    *,
    input_bundle_sha256: str,
    case_set_commitment_sha256: str,
    truth_bundle_commitment: dict[str, str],
) -> str:
    """Frozen public formula used before a one-shot tranche is minted."""

    return canonical_sha256(
        {
            "input_bundle_sha256": input_bundle_sha256,
            "case_set_commitment_sha256": case_set_commitment_sha256,
            "truth_bundle_commitment": truth_bundle_commitment,
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "G01 sealed scoring remains unavailable until the repository-external "
            "custodian scorer and signer IPC are activation-bound."
        )
    )
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--inference-outputs", required=True, type=Path)
    parser.add_argument("--prediction-envelope", required=True, type=Path)
    parser.add_argument("--inference-receipts", required=True, type=Path)
    parser.add_argument("--provider-receipt-index", required=True, type=Path)
    parser.add_argument("--provider-runtime-receipts", required=True, type=Path)
    parser.add_argument("--mint-receipt", required=True, type=Path)
    parser.add_argument("--score-input-output", required=True, type=Path)
    parser.add_argument("--score-receipt-output", required=True, type=Path)
    parser.add_argument("--truth-hmac-key-id", required=True)
    parser.parse_args()
    parser.error(
        "formal sealed scoring is NOT_RUN during BOOTSTRAP; the activated "
        "repository-external custodian scorer and signer IPC are required"
    )


if __name__ == "__main__":
    raise SystemExit(main())
