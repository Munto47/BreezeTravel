from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("RUNTIME_PROFILE", "test")
os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("PLACE_META_LOOKUP_ENABLED", "false")

from evals.faults import inject_fault
from evals.runner import load_cases
from evals.schema import EvalSplit


BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent


async def main() -> None:
    npm = shutil.which("npm.cmd") or shutil.which("npm") or "npm"
    yjs = subprocess.run(
        [npm, "test"], cwd=ROOT / "y-websocket", capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    os.environ["YJS_RESTART_TEST_PASSED"] = str(yjs.returncode == 0).lower()
    results = []
    for split in EvalSplit:
        cases = load_cases(BACKEND / "eval_data" / "fault" / f"{split.value}.jsonl", purpose="final_evaluation", split=split)
        for case in cases:
            started = time.perf_counter()
            try:
                actual = await inject_fault(case.fault_profile or "")
                passed = bool(actual.get("passed")) and actual.get("behavior") == case.expected["behavior"]
                error = None
            except Exception as exc:
                actual = {"behavior": "injector_error", "passed": False, "actual": str(exc)}
                passed = False
                error = type(exc).__name__
            results.append({
                "case_id": case.id, "split": split.value, "fault_profile": case.fault_profile,
                "injection": f"controlled:{case.fault_profile}", "expected": case.expected,
                "actual": actual, "passed": passed,
                "latency_ms": (time.perf_counter() - started) * 1000,
                "trace_id": f"fault-{case.id}", "error_category": error,
                "conclusion": "matched_expected_boundary" if passed else "unresolved_or_mismatch",
            })
    passed_count = sum(item["passed"] for item in results)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": "local-controlled-fault-injection",
        "sample_size": len(results), "passed": passed_count,
        "pass_rate": passed_count / len(results) if results else 0.0,
        "unresolved": [item["case_id"] for item in results if not item["passed"]],
        "yjs_restart_command": "npm test",
        "yjs_restart_exit_code": yjs.returncode,
        "results": results,
        "production_claim_not_made": True,
    }
    target = BACKEND / "evidence" / "fault_injection" / "summary.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    print(json.dumps({key: summary[key] for key in ("sample_size", "passed", "pass_rate", "unresolved")}, ensure_ascii=False))
    if summary["pass_rate"] < 0.90:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
