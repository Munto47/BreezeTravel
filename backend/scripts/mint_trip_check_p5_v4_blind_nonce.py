"""Mint one repository-external, label-free P5 v4 blind-run nonce."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.formal_receipts_v4 import (  # noqa: E402
    mint_blind_nonce_v4,
)


REPO_ROOT = BACKEND_ROOT.parent
P5_ROOT = REPO_ROOT / "backend" / "evals" / "trip_check_v1" / "p5"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = mint_blind_nonce_v4(
        repo_root=REPO_ROOT,
        output_path=args.output,
        active_contract_path=P5_ROOT / "active_contract.json",
        dataset_manifest_path=P5_ROOT / "dataset_v4.manifest.json",
        seal_path=P5_ROOT / "sealed" / "frozen_blind.v4.seal.json",
        nonce_schema_path=P5_ROOT / "blind_run_nonce_v4.schema.json",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
