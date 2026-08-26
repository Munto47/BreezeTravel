"""Validate and score a P5 v5 non-blind run group."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.data_contract import canonical_bytes  # noqa: E402
from evals.trip_check_v1.p5.nonblind_scorer_v5 import (  # noqa: E402
    P5NonblindScoringErrorV5,
    score_nonblind_run_group_v5,
)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(canonical_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--run-dir", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument(
        "--development",
        action="store_true",
        help="Diagnostic validation; report status is always REJECT.",
    )
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        report = score_nonblind_run_group_v5(
            run_dir=args.run_dir.resolve(),
            repo_root=REPO_ROOT,
            require_formal=not args.development,
        )
    except P5NonblindScoringErrorV5 as exc:
        print(
            json.dumps(
                {
                    "schema_version": "trip-check-p5-nonblind-score-error-v5",
                    "status": "INVALID_EVIDENCE",
                    "reason_code": exc.reason_code,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    _write_json_atomic(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "report_hash": report["report_hash"],
                "status": report["status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
