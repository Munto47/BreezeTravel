"""Generate the P5 v2 blind label bundle in external custody storage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_check_v1.p5.blind_custody_v2 import build_blind_label_bundle_v2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--external-output", type=Path, required=True)
    args = parser.parse_args()
    result = build_blind_label_bundle_v2(
        repo_root=args.repo_root,
        external_output_path=args.external_output,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
