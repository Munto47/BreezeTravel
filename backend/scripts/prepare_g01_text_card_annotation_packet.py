from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_text_cards_v1.annotations import build_blank_work_packet
from evals.trip_text_cards_v1.validator import load_cases, validate_dataset


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", required=True, choices=("dev", "validation", "frozen_blind"))
    parser.add_argument("--assignment-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--data-root", type=Path, default=Path("eval_data/trip_text_cards_v1"))
    args = parser.parse_args()

    data_root = args.data_root.resolve(strict=True)
    repository_root = data_root.parents[2]
    output = args.output.resolve()
    if _is_within(output, repository_root):
        raise SystemExit("annotation work packets must be written outside the repository")
    validate_dataset(data_root)
    packet = build_blank_work_packet(
        split=args.split,
        assignment_id=args.assignment_id,
        source_cases=load_cases(data_root)[args.split],
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True))
        handle.write("\n")
    print(json.dumps({"output": str(output), "case_count": len(packet["cases"]), "labels": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
