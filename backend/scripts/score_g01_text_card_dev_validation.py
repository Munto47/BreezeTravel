from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_text_cards_v1.annotations import verify_adjudication
from evals.trip_text_cards_v1.scorer import load_predictions, score_predictions
from evals.trip_text_cards_v1.validator import load_cases, validate_dataset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", required=True, choices=("dev", "validation"))
    parser.add_argument("--annotation-a", required=True, type=Path)
    parser.add_argument("--annotation-b", required=True, type=Path)
    parser.add_argument("--adjudication", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--data-root", type=Path, default=Path("eval_data/trip_text_cards_v1"))
    args = parser.parse_args()

    data_root = args.data_root.resolve(strict=True)
    repository_root = data_root.parents[2]
    validate_dataset(data_root)
    source_cases = load_cases(data_root)[args.split]
    adjudication, annotation_receipt = verify_adjudication(
        split=args.split,
        source_cases=source_cases,
        first_path=args.annotation_a,
        second_path=args.annotation_b,
        adjudication_path=args.adjudication,
        repository_root=repository_root,
    )
    score = score_predictions(
        source_cases=source_cases,
        gold_cases=adjudication.gold_cases,
        predictions=load_predictions(args.predictions),
    )
    receipt = {
        "schema_version": "g01-text-card-human-scored-receipt-v1",
        "split": args.split,
        "annotation": annotation_receipt,
        "score": score,
        "blind_labels_read": 0,
        "gate_claim": "NOT_RUN" if args.split == "dev" else "VALIDATION_ONLY",
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        handle.write("\n")
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
