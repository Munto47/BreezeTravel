from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_nlu_v2.validator import validate_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the public Trip NLU v2 dataset contract")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "eval_data" / "trip_nlu_v2",
    )
    parser.add_argument("--external-blind-labels", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            validate_dataset(
                args.data_root,
                external_blind_labels=args.external_blind_labels,
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
