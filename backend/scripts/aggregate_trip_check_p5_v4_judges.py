"""Aggregate exactly three independent P5 v4 automated Judge rounds."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.judge_v4 import (  # noqa: E402
    aggregate_judge_rounds_v4,
)


REPO_ROOT = BACKEND_ROOT.parent
PANEL_SCHEMA = (
    REPO_ROOT
    / "backend"
    / "evals"
    / "trip_check_v1"
    / "p5"
    / "judge_panel_v4.schema.json"
)


def _validate_panel(report: dict[str, object]) -> None:
    schema = json.loads(PANEL_SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(report),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        raise RuntimeError(f"P5_V4_JUDGE_PANEL_SCHEMA_INVALID: {errors[0].message}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--mapping-sha256", required=True)
    parser.add_argument("--round", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.round) != 3:
        parser.error("--round must be supplied exactly three times")
    report = aggregate_judge_rounds_v4(
        repo_root=REPO_ROOT,
        mapping_path=args.mapping,
        mapping_sha256=args.mapping_sha256,
        round_paths=args.round,
    )
    _validate_panel(report)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8", newline="\n")
    readback = json.loads(args.output.read_text(encoding="utf-8"))
    _validate_panel(readback)
    if readback != report:
        raise RuntimeError("P5_V4_JUDGE_PANEL_READBACK_FAILED")
    print(payload, end="")


if __name__ == "__main__":
    main()
