from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.trip_understanding.screenshot_vl import (
    G04VlReceiptError,
    evaluate_vl_candidate,
)


def _read_json(path: Path | None) -> Any:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate frozen G04 VL metrics without making provider calls"
    )
    parser.add_argument("--binding", type=Path)
    parser.add_argument("--paddle-receipt", type=Path)
    parser.add_argument("--vl-receipt", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        binding = _read_json(args.binding)
        if binding is None:
            decision = evaluate_vl_candidate()
        else:
            decision = evaluate_vl_candidate(
                _read_json(args.paddle_receipt),
                _read_json(args.vl_receipt),
                exact_binding=binding,
            )
        payload = json.dumps(
            decision.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        if args.output is not None:
            args.output.write_text(f"{payload}\n", encoding="utf-8", newline="\n")
    except (OSError, json.JSONDecodeError, G04VlReceiptError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": "g04-vl-decision-error-v1",
                    "status": "INVALID_INPUT",
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
