"""Custodian-only CLI for aggregate scoring of one P5 v4 blind run."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.data_contract import canonical_bytes  # noqa: E402
from evals.trip_check_v1.p5.final_blind_scorer_v4 import (  # noqa: E402
    ExternalCustodianBundleReaderV4,
    P5BlindScoringErrorV4,
    score_isolated_blind_v4,
)


def _write_external(path: Path, value: dict) -> None:
    absolute = path.absolute()
    try:
        absolute.relative_to(REPO_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise P5BlindScoringErrorV4("BLIND_SCORE_OUTPUT_INSIDE_REPOSITORY")
    absolute.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=absolute.parent, prefix=f".{absolute.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(canonical_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, absolute)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--run-dir", type=Path, required=True)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--bundle-sha256", required=True)
    value.add_argument("--custodian-authorization", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        reader = ExternalCustodianBundleReaderV4.from_external_files(
            bundle_path=args.bundle,
            authorization_path=args.custodian_authorization,
            repo_root=REPO_ROOT,
        )
        receipt = score_isolated_blind_v4(
            repo_root=REPO_ROOT,
            run_dir=args.run_dir,
            expected_bundle_sha256=args.bundle_sha256,
            custodian_reader=reader,
        )
        _write_external(args.output, receipt)
    except P5BlindScoringErrorV4 as exc:
        print(
            json.dumps(
                {
                    "schema_version": "trip-check-p5-isolated-blind-score-error-v4",
                    "status": "INVALID_EVIDENCE",
                    "reason_code": exc.reason_code,
                    "human_evidence": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
