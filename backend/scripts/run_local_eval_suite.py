from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

os.environ.setdefault("RUNTIME_PROFILE", "test")
os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("PLACE_META_LOOKUP_ENABLED", "false")

from evals.adapters import end_to_end_adapter, router_adapter, task_parse_adapter, verifier_adapter
from evals.runner import EvaluationRunner, load_cases
from evals.schema import EvalKind, EvalSplit


ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {
    EvalKind.ROUTER: router_adapter,
    EvalKind.TASK_PARSE: task_parse_adapter,
    EvalKind.VERIFIER: verifier_adapter,
    EvalKind.END_TO_END: end_to_end_adapter,
}


async def main(case_filter: str | None = None):
    output = ROOT / "evidence" / "local_eval"
    manifest = json.loads((ROOT / "eval_data" / "manifest.json").read_text(encoding="utf-8"))
    summary = {"runs": [], "production_claim_not_made": True}
    for kind, adapter in ADAPTERS.items():
        for split in EvalSplit:
            path = ROOT / "eval_data" / kind.value / f"{split.value}.jsonl"
            cases = load_cases(path, purpose="final_evaluation", split=split)
            if case_filter:
                cases = [case for case in cases if case.id == case_filter]
                if not cases:
                    continue
            run = await EvaluationRunner(seed=42, environment="local-controlled-mock-providers").run(
                kind=kind, cases=cases, adapter=adapter, dataset_hash=manifest["datasets"][kind.value]["hash"],
                config={"adapter": adapter.__name__, "providers": "mock_or_deterministic"}, split=split,
                corpus_hash=manifest["datasets"].get("rag_claim", {}).get("hash"), prompt_version="task-parser-v1",
            )
            target = output / kind.value / f"{split.value}.json"
            EvaluationRunner.write_atomic(run, target)
            summary["runs"].append({"kind": kind.value, "split": split.value, "samples": len(run.raw_results), "pass_rate": run.metrics["pass_rate"], "artifact": str(target.relative_to(ROOT))})
    summary["total_samples"] = sum(item["samples"] for item in summary["runs"])
    (output / "summary.json").parent.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case")
    args = parser.parse_args()
    asyncio.run(main(args.case))
