"""Build a bound P5 v5 Judge round from a Judge-owned score payload."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.contracts_v2 import canonical_bytes  # noqa: E402
from evals.trip_check_v1.p5.judge_v5 import (  # noqa: E402
    build_judge_round_report_v5,
    require_external_judge_artifact_path_v5,
)


REPO_ROOT = BACKEND_ROOT.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = require_external_judge_artifact_path_v5(
        repo_root=REPO_ROOT, path=args.output
    )
    if output.exists():
        raise RuntimeError("P5_V5_JUDGE_ROUND_OUTPUT_ALREADY_EXISTS")
    report = build_judge_round_report_v5(
        repo_root=REPO_ROOT,
        bundle_path=args.bundle,
        score_payload_path=args.scores,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_bytes(report) + b"\n")
    readback = json.loads(output.read_text(encoding="utf-8"))
    if readback != report:
        raise RuntimeError("P5_V5_JUDGE_ROUND_READBACK_FAILED")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
