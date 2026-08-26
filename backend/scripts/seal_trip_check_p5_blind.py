"""Permanently disabled P5 v1 blind sealing entrypoint."""

from __future__ import annotations

from evals.trip_check_v1.p5.active_contract import P5ContractNotReadyError, reject_v1_formal


def seal(
    *, labels_canonical_sha256: str, external_bundle_sha256: str, review_receipt_sha256: str
) -> dict:
    del labels_canonical_sha256, external_bundle_sha256, review_receipt_sha256
    reject_v1_formal()
    raise P5ContractNotReadyError("P5_V1_FORMAL_SEAL_PERMANENTLY_DISABLED")


def main() -> None:
    # This must precede argument parsing and every v1 dataset/seal read.
    reject_v1_formal()
    raise P5ContractNotReadyError("P5_V1_FORMAL_SEAL_PERMANENTLY_DISABLED")


if __name__ == "__main__":
    main()
