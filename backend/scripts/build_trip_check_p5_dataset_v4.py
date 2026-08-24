"""Build the label-free P5 v4 route-evidence supersession."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.data_contract_v4 import (  # noqa: E402
    build_dataset_v4,
    write_dataset_v4,
)


def run(*, write: bool) -> dict:
    nonblind, blind, nonblind_materializations, blind_materializations = (
        build_dataset_v4()
    )
    result = {
        "schema_version": "trip-check-p5-build-result-v4",
        "status": "PASS",
        "write": write,
        "source_mode": "SEALED_V3_ROUTE_EVIDENCE_REBIND",
        "blind_labels_read": False,
        "external_bundle_read": False,
        "fresh_actual_ocr_execution": "NOT_RUN",
        "route_evidence_repairs": [
            "p5.pilot.bj.004",
            "p5.pilot.sh.001",
        ],
        "counts": {
            "nonblind_cases": len(nonblind),
            "blind_cases": len(blind),
            "nonblind_materializations": len(nonblind_materializations),
            "blind_materializations": len(blind_materializations),
        },
    }
    if write:
        manifest = write_dataset_v4(
            nonblind_cases=nonblind,
            blind_cases=blind,
            nonblind_materializations=nonblind_materializations,
            blind_materializations=blind_materializations,
        )
        result["manifest_hash"] = manifest["manifest_hash"]
        result["seal_status"] = manifest["seal_status"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(write=args.write), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
