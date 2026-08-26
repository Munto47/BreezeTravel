"""Score a fail-closed P5 v3 non-blind run group."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.scorer_v3 import (  # noqa: E402
    P5V3ScoringError,
    score_run_group_v3,
)


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--run-dir", required=True)
    value.add_argument("--output", required=True)
    value.add_argument(
        "--development",
        action="store_true",
        help="diagnostic-only validation; the report is always REJECT",
    )
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        report = score_run_group_v3(
            run_dir=Path(args.run_dir),
            repo_root=REPO_ROOT,
            require_formal=not args.development,
        )
    except P5V3ScoringError as exc:
        print(
            json.dumps(
                {
                    "schema_version": "trip-check-p5-score-error-v3",
                    "status": "INVALID_EVIDENCE",
                    "reason_code": exc.reason_code,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    _write_json_atomic(Path(args.output), report)
    print(
        json.dumps(
            {
                "output": str(Path(args.output).resolve()),
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
