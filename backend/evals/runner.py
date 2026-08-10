from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from evals.adjudication import bad_case_registry
from evals.metrics import bootstrap_ci, bucketed
from evals.schema import EvalCase, EvalKind, EvalSplit, EvaluationRun


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_cases(path: Path, *, purpose: str, split: EvalSplit) -> list[EvalCase]:
    if split == EvalSplit.BLIND and purpose not in {"final_evaluation", "release_gate"}:
        raise PermissionError("blind labels are unavailable to training, tuning and experiments")
    cases = [EvalCase.model_validate(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [case for case in cases if case.split == split]


class EvaluationRunner:
    def __init__(self, *, seed: int = 42, environment: str = "local-controlled"):
        self.seed = seed
        self.environment = environment

    async def run(
        self,
        *,
        kind: EvalKind,
        cases: Iterable[EvalCase],
        adapter: Callable[[EvalCase], Any],
        dataset_hash: str,
        config: dict[str, Any],
        split: EvalSplit,
        corpus_hash: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> EvaluationRun:
        results = []
        for case in cases:
            try:
                actual = adapter(case)
                if inspect.isawaitable(actual):
                    actual = await actual
                passed = bool(actual.get("passed"))
                results.append({
                    "case_id": case.id,
                    "city": case.city,
                    "tags": case.tags,
                    "expected": case.expected,
                    "actual": actual,
                    "passed": passed,
                    "latency_ms": actual.get("latency_ms", 0),
                    "error_category": actual.get("error_category"),
                    "reproduce": actual.get("reproduce"),
                })
            except Exception as exc:
                results.append({
                    "case_id": case.id, "city": case.city, "tags": case.tags,
                    "expected": case.expected, "actual": None, "passed": False,
                    "latency_ms": 0, "error_category": type(exc).__name__,
                    "reproduce": f"case:{case.id}",
                })
        scores = [float(row["passed"]) for row in results]
        commit = _git_commit()
        return EvaluationRun(
            run_id=str(uuid4()),
            kind=kind,
            split=split,
            commit_sha=commit,
            dataset_hash=dataset_hash,
            corpus_hash=corpus_hash,
            config_hash=canonical_hash(config),
            model=model,
            prompt_version=prompt_version,
            environment=self.environment,
            seed=self.seed,
            metrics={
                "pass_rate": bootstrap_ci(scores, seed=self.seed),
                "sample_size": len(results),
            },
            buckets={
                "city": bucketed(results, "city", lambda row: float(row["passed"])),
                "error_category": bucketed(results, "error_category", lambda row: float(row["passed"])),
            },
            raw_results=results,
            bad_cases=bad_case_registry(results),
        )

    @staticmethod
    def write_atomic(run: EvaluationRun, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(run.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(output)


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unavailable"
