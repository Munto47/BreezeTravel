from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_text_cards_v1.runner import write_baseline
from evals.trip_text_cards_v1.validator import load_cases, validate_dataset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject-commit", required=True)
    parser.add_argument("--data-root", type=Path, default=Path("eval_data/trip_text_cards_v1"))
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    data_root = args.data_root.resolve(strict=True)
    backend_root = data_root.parents[1]
    validate_dataset(data_root)
    cases = load_cases(data_root)
    receipt = write_baseline(
        split_cases={"dev": cases["dev"], "validation": cases["validation"]},
        output_root=args.output_root.resolve(),
        backend_root=backend_root,
        subject_commit=args.subject_commit,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
