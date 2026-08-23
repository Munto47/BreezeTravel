"""Build the sole P5 v2 case/materialization JSONL path."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.data_contract_v2 import build_dataset_v2, write_dataset_v2  # noqa: E402


async def _run(*, ocr_mode: str, write: bool, work_root: Path) -> dict:
    nonblind_cases, blind_cases, nonblind_materializations, blind_materializations = await build_dataset_v2(
        ocr_mode=ocr_mode,  # type: ignore[arg-type]
        work_root=work_root,
    )
    result = {
        "schema_version": "trip-check-p5-build-result-v2",
        "write": write,
        "ocr_mode": ocr_mode,
        "counts": {
            "nonblind_cases": len(nonblind_cases),
            "blind_cases": len(blind_cases),
            "nonblind_materializations": len(nonblind_materializations),
            "blind_materializations": len(blind_materializations),
            "screenshots": sum(
                row["input_kind"] == "SYNTHETIC_SCREENSHOT" for row in [*nonblind_cases, *blind_cases]
            ),
        },
        "formal_validation_eligible": ocr_mode == "actual",
    }
    if write:
        manifest = write_dataset_v2(
            nonblind_cases=nonblind_cases,
            blind_cases=blind_cases,
            nonblind_materializations=nonblind_materializations,
            blind_materializations=blind_materializations,
            ocr_mode=ocr_mode,  # type: ignore[arg-type]
        )
        result["manifest_hash"] = manifest["manifest_hash"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ocr-mode", choices=("development", "actual"), required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--work-root", type=Path)
    args = parser.parse_args()
    if args.work_root is not None:
        args.work_root.mkdir(parents=True, exist_ok=True)
        result = asyncio.run(_run(ocr_mode=args.ocr_mode, write=args.write, work_root=args.work_root))
    else:
        with tempfile.TemporaryDirectory(prefix="p5-v2-materialization-") as directory:
            result = asyncio.run(
                _run(ocr_mode=args.ocr_mode, write=args.write, work_root=Path(directory))
            )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
