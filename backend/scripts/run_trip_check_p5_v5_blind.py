"""Execute the sealed P5 v5 frozen-blind lane exactly once."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.runner_v5 import (  # noqa: E402
    BlindDatasetPathsV5,
    P5BlindRunnerErrorV5,
    run_blind_once_v5,
)


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=REPO_ROOT, text=True, encoding="utf-8"
    ).strip()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--output-root", type=Path, required=True)
    value.add_argument("--consumption-dir", type=Path, required=True)
    value.add_argument("--nonce-file", type=Path, required=True)
    value.add_argument("--run-id", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    # Delayed because the dataset/seal v5 slice is integrated independently.
    from evals.trip_check_v1.p5.data_contract_v2 import JUDGE_RUBRIC_PATH_V2
    from evals.trip_check_v1.p5.data_contract_v3 import (
        ACTIVE_CONTRACT_PATH,
    )
    from evals.trip_check_v1.p5.data_contract_v5 import (
        BLIND_INPUT_PATH_V5,
        BLIND_MATERIALIZATIONS_PATH_V5,
        BLIND_SEAL_PATH_V5,
        MANIFEST_PATH_V5,
        RUN_SPEC_TEMPLATE_PATH_V5,
    )

    subject = _git("rev-parse", "HEAD")
    upstream_ref = _git("rev-parse", "--abbrev-ref", "@{upstream}")
    upstream_commit = _git("rev-parse", "@{upstream}")
    dirty = bool(_git("status", "--short"))
    try:
        receipt = asyncio.run(
            run_blind_once_v5(
                repo_root=REPO_ROOT,
                dataset_paths=BlindDatasetPathsV5(
                    inputs=BLIND_INPUT_PATH_V5,
                    materializations=BLIND_MATERIALIZATIONS_PATH_V5,
                    manifest=MANIFEST_PATH_V5,
                    seal=BLIND_SEAL_PATH_V5,
                    run_spec_template=RUN_SPEC_TEMPLATE_PATH_V5,
                    rubric=JUDGE_RUBRIC_PATH_V2,
                    active_contract=ACTIVE_CONTRACT_PATH,
                ),
                output_root=args.output_root,
                consumption_dir=args.consumption_dir,
                nonce_file=args.nonce_file,
                run_id=args.run_id,
                subject_commit=subject,
                upstream_ref=upstream_ref,
                upstream_commit=upstream_commit,
                dirty_tree=dirty,
            )
        )
    except P5BlindRunnerErrorV5 as exc:
        print(
            json.dumps(
                {
                    "schema_version": "trip-check-p5-blind-run-error-v5",
                    "status": "INVALID_EVIDENCE",
                    "reason_code": exc.reason_code,
                    "blind_labels_read": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "run_dir": receipt["run_dir"],
                "manifest_hash": receipt["manifest_hash"],
                "blind_labels_read": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
