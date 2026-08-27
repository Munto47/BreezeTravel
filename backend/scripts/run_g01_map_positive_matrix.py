from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_text_cards_v1.map_positive import run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("eval_data/g01_map_positive_v1"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    receipt = run(args.data_root)
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
    print(encoded, end="")
    return 0 if receipt["fixture_subgate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
