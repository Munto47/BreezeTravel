"""Build the formal P5 v5 Evaluation Gate from sealed, replayed artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.gate_v5 import build_p5_gate_manifest_v5  # noqa: E402


REPO_ROOT = BACKEND_ROOT.parent
P5_ROOT = REPO_ROOT / "backend" / "evals" / "trip_check_v1" / "p5"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=P5_ROOT / "dataset_v5.manifest.json")
    parser.add_argument("--active-contract", type=Path, default=P5_ROOT / "active_contract.json")
    parser.add_argument(
        "--blind-seal",
        type=Path,
        default=P5_ROOT / "sealed" / "frozen_blind.v5.seal.json",
    )
    parser.add_argument("--run-spec", type=Path, default=P5_ROOT / "run_spec_template_v5.json")
    parser.add_argument("--rubric", type=Path, default=P5_ROOT / "judge_rubric_v2.json")
    parser.add_argument("--nonblind-run-manifest", type=Path, required=True)
    parser.add_argument("--nonblind-score", type=Path, required=True)
    parser.add_argument("--blind-run-manifest", type=Path, required=True)
    parser.add_argument("--blind-score", type=Path, required=True)
    parser.add_argument("--judge-panel", type=Path, required=True)
    parser.add_argument("--formal-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--gate-schema", type=Path, default=P5_ROOT / "gate_v5.schema.json"
    )
    args = parser.parse_args()
    manifest = build_p5_gate_manifest_v5(
        repo_root=REPO_ROOT,
        dataset_manifest_path=args.dataset,
        active_contract_path=args.active_contract,
        blind_seal_path=args.blind_seal,
        run_spec_path=args.run_spec,
        rubric_path=args.rubric,
        nonblind_run_manifest_path=args.nonblind_run_manifest,
        nonblind_score_path=args.nonblind_score,
        blind_run_manifest_path=args.blind_run_manifest,
        blind_score_path=args.blind_score,
        judge_panel_path=args.judge_panel,
        formal_receipt_path=args.formal_receipt,
        output_path=args.output,
        gate_schema_path=args.gate_schema,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
