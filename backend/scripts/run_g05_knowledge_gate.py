from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from evals.g05_knowledge import evaluate_admission_manifest, load_admission_manifest
from evals.g05_knowledge.ablation import evaluate_knowledge_ablation, load_ablation_oracle


ROOT = Path(__file__).parents[1]
DEFAULT_MANIFEST = ROOT / "eval_data/g05_knowledge/admission_v1.json"
DEFAULT_ORACLE = ROOT / "eval_data/g05_knowledge/ablation_oracle_v1.json"
DEFAULT_AS_OF = datetime.fromisoformat("2026-08-31T09:00:00+08:00")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the G05 source admission and ablation gate")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--oracle", type=Path, default=DEFAULT_ORACLE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples", type=int, default=300)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()

    manifest = load_admission_manifest(args.manifest)
    admission = evaluate_admission_manifest(manifest, as_of=DEFAULT_AS_OF)
    ablation = evaluate_knowledge_ablation(
        manifest,
        load_ablation_oracle(args.oracle),
        as_of=DEFAULT_AS_OF,
        samples=args.samples,
        rounds=args.rounds,
    )
    payload = {
        "schema_version": "g05-knowledge-gate-result-v1",
        "dataset_id": manifest["dataset_id"],
        "as_of": DEFAULT_AS_OF.isoformat(),
        "admission": {
            **admission.__dict__,
            "errors": list(admission.errors),
            "passed": admission.passed,
        },
        "ablation": ablation.to_dict(),
        "gate_result": "PASS" if admission.passed and ablation.passed else "FAIL",
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0 if payload["gate_result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
