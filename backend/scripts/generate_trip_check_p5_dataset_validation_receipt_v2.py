"""Generate the formal P5 v2 dataset validation receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_check_v1.p5.formal_validation_receipt_v2 import (
    DEFAULT_RECEIPT_PATH,
    generate_formal_validation_receipt,
)
from scripts.validate_trip_check_p5_dataset_v2 import validate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject-commit")
    parser.add_argument("--output", type=Path, default=DEFAULT_RECEIPT_PATH)
    args = parser.parse_args()
    receipt = generate_formal_validation_receipt(
        subject_commit=args.subject_commit,
        output_path=args.output,
        validator=validate,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
